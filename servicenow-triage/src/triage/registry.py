"""Stage 7: versioned model storage.

    models/
      v20261012-143000/model.joblib
      v20261012-143000/manifest.json   (sha256, data range, honest scores)
      PRODUCTION                        (the version being served)
      HISTORY.jsonl                     (every promotion, for rollback)

joblib files can run code when loaded: only load files from this folder, keep
it writable by the training job only, and the sha256 check below refuses a
file that was changed after it was saved.

    python -m triage.registry list
    python -m triage.registry promote v20261012-143000
    python -m triage.registry rollback
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import sklearn

from triage.config import load_config, resolve
from triage.model import TriageModel


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_version() -> str:
    return datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%S")


def save(model: TriageModel, models_dir: Path, version: str, extra: dict[str, Any] | None = None) -> Path:
    d = models_dir / version
    d.mkdir(parents=True, exist_ok=False)
    model.meta["version"] = version
    path = d / "model.joblib"
    joblib.dump(model, path, compress=3)
    manifest = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": _sha256(path),
        "sklearn_version": sklearn.__version__,
        "trained_from": model.meta.get("trained_from"),
        "trained_to": model.meta.get("trained_to"),
        "n_train": model.meta.get("n_train"),
        **(extra or {}),
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return d


def manifest(models_dir: Path, version: str) -> dict[str, Any]:
    return json.loads((models_dir / version / "manifest.json").read_text())


def load(models_dir: Path, version: str | None = None) -> TriageModel:
    """Load a version (default: production) after checking its sha256."""
    version = version or production_version(models_dir)
    if not version:
        raise FileNotFoundError(f"no production model in {models_dir}")
    man = manifest(models_dir, version)
    path = models_dir / version / "model.joblib"
    if _sha256(path) != man["sha256"]:
        raise RuntimeError(f"checksum mismatch for {path}: refusing to load")
    if man["sklearn_version"] != sklearn.__version__:
        raise RuntimeError(f"{version} was saved with scikit-learn {man['sklearn_version']}, running {sklearn.__version__}")
    model = joblib.load(path)
    if not isinstance(model, TriageModel):
        raise TypeError(f"{path} is not a TriageModel")
    return model


def production_version(models_dir: Path) -> str | None:
    p = models_dir / "PRODUCTION"
    return p.read_text().strip() or None if p.exists() else None


def promote(models_dir: Path, version: str, reason: str = "") -> None:
    if not (models_dir / version / "manifest.json").exists():
        raise FileNotFoundError(version)
    previous = production_version(models_dir)
    if previous == version:
        return  # already in production; a history entry would break rollback
    tmp = models_dir / "PRODUCTION.tmp"
    tmp.write_text(version + "\n")
    tmp.replace(models_dir / "PRODUCTION")  # atomic swap
    with open(models_dir / "HISTORY.jsonl", "a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "version": version,
                            "previous": previous, "reason": reason}) + "\n")


def rollback(models_dir: Path) -> str:
    current = production_version(models_dir)
    hist = [json.loads(l) for l in (models_dir / "HISTORY.jsonl").read_text().splitlines() if l.strip()]
    entry = next((h for h in reversed(hist) if h["version"] == current), None)
    if not entry or not entry.get("previous"):
        raise RuntimeError("no previous production version to roll back to")
    promote(models_dir, entry["previous"], reason=f"rollback from {current}")
    return entry["previous"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("promote")
    p.add_argument("version")
    sub.add_parser("rollback")
    a = ap.parse_args()
    cfg = load_config(a.config)
    models_dir = resolve(cfg, cfg["paths"]["models_dir"])
    if a.cmd == "list":
        prod = production_version(models_dir)
        for d in sorted(p for p in models_dir.glob("v*") if p.is_dir()):
            m = manifest(models_dir, d.name)
            honest = m.get("holdout_metrics", {}).get("fields", {})
            accs = ", ".join(f"{t} {f['accuracy']}%" for t, f in honest.items())
            print(f"{'*' if d.name == prod else ' '} {d.name}  data to {m.get('trained_to')}  {accs}")
    elif a.cmd == "promote":
        promote(models_dir, a.version, reason="manual")
        print(f"production -> {a.version}")
    else:
        print(f"production -> {rollback(models_dir)}")


if __name__ == "__main__":
    main()

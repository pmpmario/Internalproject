"""Load config.yaml and resolve paths relative to the project root."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_root"] = str(path.resolve().parent)
    return cfg


def resolve(cfg: dict[str, Any], rel: str | Path) -> Path:
    """Resolve a path from the config against the config file's folder."""
    p = Path(rel)
    return p if p.is_absolute() else Path(cfg["_root"]) / p


def enabled_targets(cfg: dict[str, Any]) -> list[str]:
    return [t for t, spec in cfg["targets"].items() if spec.get("enabled", True)]

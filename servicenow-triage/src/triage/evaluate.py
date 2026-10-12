"""Stage 6: score predictions against what really happened.

Used by train.py (holdout test) and monitor.py (live tickets), so the numbers
in both reports are computed the same way.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from triage.labels import urgent_labels
from triage.model import TriageModel

THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]


def _pct(x: float) -> float:
    return round(100 * float(x), 1)


def _p(x: float | None) -> str:
    return "-" if x is None else f"{x}%"


def score(model: TriageModel, actual: pd.DataFrame, pred: pd.DataFrame) -> dict[str, Any]:
    """`actual` holds the true field values, `pred` the output of model.predict
    (same index). Returns JSON-serialisable metrics."""
    cfg = model.cfg
    res: dict[str, Any] = {"n_tickets": int(len(actual)), "fields": {}}
    for target, lm in model.label_maps.items():
        if target not in actual.columns:
            continue
        raw = actual[target].astype("string").str.strip().replace(lm.merge)
        y = lm.transform(actual[target])
        rows = y.notna()
        if not rows.any():
            continue
        yt, yp = y[rows].astype(str), pred.loc[rows, f"pred_{target}"].astype(str)
        conf = pred.loc[rows, f"conf_{target}"].astype(float)
        correct = (yt == yp).to_numpy()
        majority = model.meta["majority"][target]
        f: dict[str, Any] = {
            "n": int(rows.sum()),
            "accuracy": _pct(correct.mean()),
            "baseline": _pct((yt == majority).mean()),
            "unseen_class_share": _pct((~raw[rows].isin(lm.known)).mean()),
            "mean_confidence": round(float(conf.mean()), 4),
        }
        f["gain"] = round(f["accuracy"] - f["baseline"], 1)
        table = []
        for t in THRESHOLDS:
            sel = (conf >= t).to_numpy()
            table.append({"threshold": t, "coverage": _pct(sel.mean()), "accuracy": _pct(correct[sel].mean()) if sel.any() else None})
        f["confidence_table"] = table
        cut = cfg["cutoffs"].get(target)
        if cut is not None:
            sel = (conf >= cut).to_numpy()
            f["at_cutoff"] = {"cutoff": cut, "coverage": _pct(sel.mean()), "accuracy": _pct(correct[sel].mean()) if sel.any() else None}
        train_counts = model.meta["train_counts"].get(target, {})
        per_class = []
        for cls, grp in yt.groupby(yt):
            per_class.append(
                {
                    "class": cls,
                    "train_n": train_counts.get(cls, 0),
                    "test_n": int(len(grp)),
                    "recall": _pct((yp[grp.index] == cls).mean()),
                    "precision": _pct((yt[yp == cls] == cls).mean()) if (yp == cls).any() else None,
                }
            )
        f["per_class"] = sorted(per_class, key=lambda r: -r["test_n"])
        wrong = pd.DataFrame({"actual": yt[~correct], "predicted": yp[~correct]})
        f["top_confusions"] = (
            wrong.value_counts().head(10).rename("n").reset_index().to_dict("records") if len(wrong) else []
        )
        res["fields"][target] = f

    if "auto_assignment_group" in pred.columns and "assignment_group" in model.label_maps:
        y = model.label_maps["assignment_group"].transform(actual["assignment_group"])
        auto = pred["auto_assignment_group"] & y.notna()
        hit = (pred.loc[auto, "pred_assignment_group"] == y[auto].astype(str)).to_numpy()
        res["auto_route"] = {"coverage": _pct(auto.mean()), "accuracy": _pct(hit.mean()) if auto.any() else None}

    pf = cfg["urgency"]["priority_field"]
    if "urgent_alert" in pred.columns and pf in actual.columns:
        u = urgent_labels(actual, cfg)
        rows = u.notna()
        truth, alert = u[rows].astype(bool).to_numpy(), pred.loc[rows, "urgent_alert"].astype(bool).to_numpy()
        res["urgent"] = {
            "n_urgent": int(truth.sum()),
            "recall": _pct(alert[truth].mean()) if truth.any() else None,
            "precision": _pct(truth[alert].mean()) if alert.any() else None,
            "alerts_per_urgent": round(float(alert.sum() / truth.sum()), 1) if truth.any() else None,
        }
    return res


def write_report(metrics: dict[str, Any], out_dir: Path, title: str, extra: dict[str, Any] | None = None) -> Path:
    """Write metrics.json and a readable report.md. Returns the markdown path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {**metrics, **(extra or {})}
    (out_dir / "metrics.json").write_text(json.dumps(payload, indent=2, default=str))
    lines = [f"# {title}", ""]
    for k, v in (extra or {}).items():
        if not isinstance(v, (dict, list)):
            lines.append(f"- **{k}:** {v}")
    lines += ["", f"Tickets scored: {metrics['n_tickets']:,}", "", "## Accuracy by field", "",
              "| Field | Accuracy | Baseline | Gain | At cut-off: coverage / accuracy | Unseen classes |",
              "|---|---|---|---|---|---|"]
    for t, f in metrics["fields"].items():
        ac = f.get("at_cutoff") or {}
        cut = f"{_p(ac.get('coverage'))} / {_p(ac.get('accuracy'))} (≥{ac.get('cutoff')})" if ac else "-"
        lines.append(f"| {t} | {f['accuracy']}% | {f['baseline']}% | {f['gain']:+} | {cut} | {f['unseen_class_share']}% |")
    if "auto_route" in metrics:
        a = metrics["auto_route"]
        lines += ["", f"**Auto-routing:** {_p(a['coverage'])} of tickets, {_p(a['accuracy'])} correct."]
    if "urgent" in metrics:
        u = metrics["urgent"]
        lines += ["", f"**Urgent alert:** {u['n_urgent']} real P1/P2; recall {_p(u['recall'])}, precision {_p(u['precision'])}, "
                      f"{u['alerts_per_urgent']} alerts per real urgent ticket."]
    for t, f in metrics["fields"].items():
        lines += ["", f"## {t}", "", "Confidence table:", "", "| Threshold | Coverage | Accuracy |", "|---|---|---|"]
        lines += [f"| ≥{r['threshold']} | {_p(r['coverage'])} | {_p(r['accuracy'])} |" for r in f["confidence_table"]]
        weak = [r for r in f["per_class"] if r["recall"] < 50]
        if weak:
            lines += ["", "Classes with recall under 50%:", "", "| Class | Train n | Test n | Recall |", "|---|---|---|---|"]
            lines += [f"| {r['class']} | {r['train_n']} | {r['test_n']} | {r['recall']}% |" for r in weak[:25]]
        if f["top_confusions"]:
            lines += ["", "Most common mistakes:", "", "| Actual | Predicted | n |", "|---|---|---|"]
            lines += [f"| {r['actual']} | {r['predicted']} | {r['n']} |" for r in f["top_confusions"]]
    md = out_dir / "report.md"
    md.write_text("\n".join(lines) + "\n")
    return md


def mean_confidences(metrics: dict[str, Any]) -> dict[str, float]:
    return {t: f["mean_confidence"] for t, f in metrics["fields"].items()}


def accuracy_of(metrics: dict[str, Any], field: str) -> float:
    return metrics["fields"].get(field, {}).get("accuracy", np.nan)

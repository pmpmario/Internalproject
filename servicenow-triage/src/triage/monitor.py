"""Stage 10: is the live model still good? Run weekly (cron / scheduler).

Needs a fresh export of incidents that have since been RESOLVED/CLOSED, so the
true CI, team and priority are known.

    # score what the API actually predicted (from logs/predictions.jsonl):
    python -m triage.monitor --actuals data/closed_last_week.csv

    # or re-score tickets the production model has never seen
    # (e.g. before go-live, or to test new exports):
    python -m triage.monitor --actuals data/new_tickets.csv --rescore

Writes reports/monitor-<date>/report.md and exits with code 1 when a
threshold in config.yaml (monitoring:) is breached, so the scheduler can alert
and/or trigger `python -m triage.train --promote auto`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from triage import registry
from triage.clean import clean_rows, load_raw
from triage.config import load_config, resolve
from triage.evaluate import score, write_report
from triage.model import TriageModel


def predictions_from_log(log_path: Path, numbers: pd.Series, id_col: str) -> pd.DataFrame:
    """Latest logged prediction per ticket number."""
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    log = pd.DataFrame(rows)
    if log.empty:
        return log
    log = log[log[id_col].isin(set(numbers))].drop_duplicates(subset=[id_col], keep="last")
    return log


def check(metrics: dict[str, Any], model: TriageModel, cfg: dict[str, Any]) -> list[str]:
    mon, alerts = cfg["monitoring"], []
    for field, min_acc in (mon.get("min_accuracy") or {}).items():
        f = metrics["fields"].get(field)
        if f and f["accuracy"] < 100 * min_acc:
            alerts.append(f"{field} accuracy {f['accuracy']}% < {100 * min_acc:.0f}%")
    ar = metrics.get("auto_route", {})
    if ar.get("accuracy") is not None and ar["accuracy"] < 100 * mon["min_auto_route_accuracy"]:
        alerts.append(f"auto-route accuracy {ar['accuracy']}% < {100 * mon['min_auto_route_accuracy']:.0f}%")
    for field, f in metrics["fields"].items():
        if f["unseen_class_share"] > 100 * mon["max_unseen_class_share"]:
            alerts.append(f"{field}: {f['unseen_class_share']}% of tickets belong to classes the model does not know (new CI/team?)")
        base = model.meta.get("holdout_mean_confidence", {}).get(field)
        if base is not None and f["mean_confidence"] < base - mon["max_confidence_drop"]:
            alerts.append(f"{field}: mean confidence {f['mean_confidence']} vs {base} at training (drift)")
    return alerts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--actuals", required=True, help="CSV export of resolved/closed incidents")
    ap.add_argument("--rescore", action="store_true", help="predict with the production model instead of reading the log")
    ap.add_argument("--version", help="model version (default: production)")
    a = ap.parse_args()

    cfg = load_config(a.config)
    models_dir = resolve(cfg, cfg["paths"]["models_dir"])
    model = registry.load(models_dir, a.version)
    id_col, date_col = cfg["data"]["id_col"], cfg["data"]["date_col"]
    actual, _ = clean_rows(load_raw(cfg, a.actuals), cfg)

    if a.rescore:
        unseen = actual[actual[date_col] > pd.Timestamp(model.meta["trained_to"])]
        if len(unseen) < len(actual):
            print(f"note: {len(actual) - len(unseen):,} tickets were in the model's training data and are skipped")
        actual = unseen.reset_index(drop=True)
        pred = model.predict(actual)
        source = f"re-scored with {model.meta['version']}"
    else:
        log = predictions_from_log(resolve(cfg, cfg["paths"]["prediction_log"]), actual[id_col], id_col)
        if log.empty:
            sys.exit("no logged predictions match these tickets; use --rescore")
        actual = actual.merge(log[[id_col]], on=id_col).reset_index(drop=True)
        pred = actual[[id_col]].merge(log, on=id_col, how="left")
        source = f"logged predictions ({', '.join(sorted(log['model_version'].unique()))})"

    if actual.empty:
        sys.exit("no tickets to score")
    metrics = score(model, actual, pred)
    alerts = check(metrics, model, cfg)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = resolve(cfg, cfg["paths"]["reports_dir"]) / f"monitor-{stamp}"
    md = write_report(metrics, out, title=f"Monitoring report {stamp}", extra={
        "source": source, "period": f"{actual[date_col].min()} to {actual[date_col].max()}",
        "alerts": "; ".join(alerts) or "none",
    })
    print(f"report {md}")
    for t, f in metrics["fields"].items():
        print(f"  {t:<18} {f['accuracy']:>5}%")
    if alerts:
        print("ALERTS:\n  " + "\n  ".join(alerts))
        sys.exit(1)


if __name__ == "__main__":
    main()

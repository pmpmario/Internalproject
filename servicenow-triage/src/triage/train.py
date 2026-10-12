"""Stages 4-7: split by time, train, score honestly, save, and (optionally) promote.

    python -m triage.train --config config.yaml                 # train + save, no promotion
    python -m triage.train --config config.yaml --promote auto  # promote only if it beats production
    python -m triage.train --config config.yaml --promote yes   # first deployment / forced

What it does:
1. Clean the export (clean.py).
2. Split by opened_at: earliest 80% train, latest 20% test.
3. Train on the 80% and score on the 20% -> the HONEST numbers (reports/<version>/).
4. Retrain the same recipe on 100% of the tickets -> the model that is deployed.
   Its manifest carries the honest numbers from step 3; quote those, never
   scores of a model on tickets it has learned.
5. If a production model exists, compare it with the new one on test tickets
   that NEITHER has seen (opened after production's training data ended).
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import Any

import pandas as pd

from triage import registry
from triage.clean import clean_rows, load_raw
from triage.config import load_config, resolve
from triage.evaluate import accuracy_of, score, write_report
from triage.model import TriageModel

log = logging.getLogger("triage.train")


def time_split(df: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values(cfg["data"]["date_col"]).reset_index(drop=True)
    cut = int(round(len(df) * (1 - cfg["split"]["test_fraction"])))
    return df.iloc[:cut], df.iloc[cut:]


def compare_with_production(candidate: TriageModel, test: pd.DataFrame, cfg: dict[str, Any]) -> dict[str, Any]:
    """Score production and the candidate's holdout model on the same unseen tickets."""
    models_dir = resolve(cfg, cfg["paths"]["models_dir"])
    prod_version = registry.production_version(models_dir)
    if not prod_version:
        return {"decision": "promote", "why": "no production model yet"}
    prod = registry.load(models_dir, prod_version)
    date_col = cfg["data"]["date_col"]
    fresh = test[test[date_col] > pd.Timestamp(prod.meta["trained_to"])]
    p = cfg["promotion"]
    if len(fresh) < p["min_compare_tickets"]:
        return {"decision": "keep", "production": prod_version,
                "why": f"only {len(fresh)} tickets newer than production's training data (need {p['min_compare_tickets']})"}
    m_prod = score(prod, fresh, prod.predict(fresh))
    m_new = score(candidate, fresh, candidate.predict(fresh))
    rows, worse = [], []
    for f in p["key_fields"]:
        a, b = accuracy_of(m_prod, f), accuracy_of(m_new, f)
        rows.append({"field": f, "production": a, "candidate": b})
        if b < a - p["max_drop_points"]:
            worse.append(f)
    return {
        "decision": "keep" if worse else "promote",
        "production": prod_version,
        "n_compared": int(len(fresh)),
        "fields": rows,
        "why": f"candidate worse on {worse}" if worse else "candidate not worse on key fields",
    }


def run(cfg: dict[str, Any], data: str | None = None, promote: str = "no") -> dict[str, Any]:
    t0 = time.time()
    df, removed = clean_rows(load_raw(cfg, data), cfg)
    train, test = time_split(df, cfg)
    date_col = cfg["data"]["date_col"]
    log.info("clean tickets %d (removed %s)", len(df), removed)
    log.info("train %d (%s to %s), test %d (%s to %s)", len(train), train[date_col].min(), train[date_col].max(),
             len(test), test[date_col].min(), test[date_col].max())

    log.info("training holdout model on the earliest %d tickets", len(train))
    holdout = TriageModel.fit(train, cfg)
    honest = score(holdout, test, holdout.predict(test))
    comparison = compare_with_production(holdout, test, cfg)

    log.info("training final model on all %d tickets", len(df))
    final = TriageModel.fit(df, cfg)
    final.meta["holdout_mean_confidence"] = {t: f["mean_confidence"] for t, f in honest["fields"].items()}

    version = registry.new_version()
    models_dir = resolve(cfg, cfg["paths"]["models_dir"])
    split_info = {
        "train_period": f"{train[date_col].min()} to {train[date_col].max()}",
        "test_period": f"{test[date_col].min()} to {test[date_col].max()}",
        "n_train": len(train), "n_test": len(test), "rows_removed": removed,
    }
    registry.save(final, models_dir, version, extra={"holdout_metrics": honest, "split": split_info, "comparison": comparison})

    report = write_report(
        honest, resolve(cfg, cfg["paths"]["reports_dir"]) / version,
        title=f"Holdout report {version} (honest: trained on earliest 80%, scored on latest 20%)",
        extra={"version": version, **{k: v for k, v in split_info.items() if k != "rows_removed"},
               "promotion decision": f"{comparison['decision']} ({comparison['why']})",
               "comparison": comparison, "rows_removed": removed},
    )

    promoted = promote == "yes" or (promote == "auto" and comparison["decision"] == "promote")
    if promoted:
        registry.promote(models_dir, version, reason=f"train --promote {promote}: {comparison['why']}")
    log.info("saved %s in %.0fs; report %s; promoted=%s", version, time.time() - t0, report, promoted)
    return {"version": version, "promoted": promoted, "report": str(report), "metrics": honest, "comparison": comparison}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--data", help="CSV export (default: data.path in config)")
    ap.add_argument("--promote", choices=["no", "auto", "yes"], default="no")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    r = run(load_config(a.config), a.data, a.promote)
    print(f"\nversion {r['version']}  promoted={r['promoted']}\nreport  {r['report']}")
    for t, f in r["metrics"]["fields"].items():
        print(f"  {t:<18} {f['accuracy']:>5}%  (baseline {f['baseline']}%)")


if __name__ == "__main__":
    main()

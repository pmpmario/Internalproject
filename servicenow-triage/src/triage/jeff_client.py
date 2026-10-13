"""Ask a running jeff-serve (with the adapter trained on jeff_rows.py's files) about tickets.

predict() returns the same pred_<field> / conf_<field> keys as /predict, so the two
models can be compared, or Jeff used in the same decision rules. conf_<field> is the
probability of the chosen option, like sklearn's, so the config cut-offs mean the same
thing (Jeff's own "confidence" field is rescaled for the number of options). Only the standard
library is used; the jeff package (and torch) are needed on the server, not here.

Score a held-out file (accuracy, and accuracy and coverage at the config cut-offs):
    python -m triage.jeff_client --rows data/jeff/test.jsonl --url http://localhost:8765 --model servicenow

Predict for tickets in the my_tickets format:
    python -m triage.jeff_client --tickets my_tickets.json --options data/jeff/options.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from triage.config import load_config
from triage.jeff_rows import ticket_state


def decide(url: str, model: str, state: Any, questions: dict[str, Any], api_key: str | None = None,
           retries: int = 5) -> dict[str, Any]:
    """One POST /v1/systemone: several questions about one ticket in one request."""
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(f"{url.rstrip('/')}/v1/systemone", body, headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)["answers"]
        except urllib.error.HTTPError as e:
            if e.code != 529 or attempt == retries - 1:  # 529: the model is busy with another request
                raise
            time.sleep(1)
    raise RuntimeError("unreachable")


def predict(ticket: dict[str, Any], options: dict[str, Any], cfg: dict[str, Any], url: str, model: str,
            api_key: str | None = None) -> dict[str, Any]:
    answers = decide(url, model, ticket_state(ticket, cfg), options, api_key)
    out: dict[str, Any] = {}
    for field, answer in answers.items():
        out[f"pred_{field}"] = answer["choice"]
        out[f"conf_{field}"] = answer["probabilities"][answer["choice"]]
    return out


def score_rows(rows: list[dict[str, Any]], cfg: dict[str, Any], url: str, model: str,
               api_key: str | None = None) -> dict[str, dict[str, float]]:
    """Accuracy per field on labelled rows, plus coverage and accuracy at the config cut-off."""
    tickets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tickets[row["source"]["ticket"]].append(row)
    hits: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    for group in tickets.values():
        questions = {r["source"]["field"]: r["question"] for r in group}
        answers = decide(url, model, group[0]["state"], questions, api_key)
        for r in group:
            a = answers[r["source"]["field"]]
            hits[r["source"]["field"]].append((a["choice"] == r["label"], a["probabilities"][a["choice"]]))
    report = {}
    for field, h in hits.items():
        cut = cfg.get("cutoffs", {}).get(field, 0.9)
        sure = [ok for ok, conf in h if conf >= cut]
        report[field] = {"rows": len(h), "accuracy": 100 * sum(ok for ok, _ in h) / len(h), "cutoff": cut,
                         "coverage_at_cutoff": 100 * len(sure) / len(h),
                         "accuracy_at_cutoff": 100 * sum(sure) / len(sure) if sure else float("nan")}
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--url", default="http://localhost:8765")
    ap.add_argument("--model", default="servicenow", help="The adapter's folder name under JEFF_ADAPTERS")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--rows", type=Path, help="Labelled rows from jeff_rows.py: print accuracy")
    src.add_argument("--tickets", type=Path, help="JSON list of tickets: print predictions")
    ap.add_argument("--options", type=Path, default=Path("data/jeff/options.json"))
    a = ap.parse_args()
    cfg, key = load_config(a.config), os.getenv("JEFF_API_KEY")
    if a.rows:
        rows = [json.loads(line) for line in a.rows.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"{'field':<18}{'rows':>7}{'accuracy':>10}{'cut-off':>9}{'covered':>9}{'acc. there':>12}")
        for field, r in score_rows(rows, cfg, a.url, a.model, key).items():
            print(f"{field:<18}{r['rows']:>7}{r['accuracy']:>9.1f}%{r['cutoff']:>9}"
                  f"{r['coverage_at_cutoff']:>8.1f}%{r['accuracy_at_cutoff']:>11.1f}%")
    else:
        options = json.loads(a.options.read_text(encoding="utf-8"))
        for ticket in json.loads(a.tickets.read_text(encoding="utf-8")):
            print(json.dumps({"short_description": ticket.get("short_description"),
                              **predict(ticket, options, cfg, a.url, a.model, key)}))


if __name__ == "__main__":
    main()

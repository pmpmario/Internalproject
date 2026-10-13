"""Turn incidents into training rows for a Jeff adapter (https://github.com/firelex/jeff).

Jeff is a small decision model: you give it a ticket (the "state") and a question with
a list of options, and it returns a calibrated probability for each option. An adapter
(LoRA) teaches it one job. Here the job is five questions per ticket: CI, assignment
group, category, subcategory and priority. Each (ticket, question) pair is one row.

Input, either:
- the ServiceNow export (CSV, the same columns as training: the label of each field is
  its own column on the closed ticket), or
- a JSON / JSONL list of dicts in the "my_tickets" format, where the labels are the
  optional expected_<field> keys and cmdb_ci is the CI the caller picked (ignored, as
  in the sklearn model).

Output (in --out):
- train / development / calibration / test .jsonl: split by time, whole ISO weeks per
  split, the latest weeks last (the same idea as the sklearn holdout). One week is one
  Jeff "family", so the kit's leak check can confirm no week is in two files.
- options.json: the questions exactly as trained. Serving must ask the same questions
  (jeff_triage.client reads this file).

    python -m jeff_triage.rows --data data/raw_incident_reassign.csv --out data/jeff
    python -m jeff_triage.rows --data my_tickets.json --out data/jeff --min-count 1

The text cleaning, label folding and config come from ../servicenow-triage (the triage
package), so Jeff sees tickets cleaned exactly like the sklearn model does.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from triage.clean import clean_rows, clean_text, load_raw
from triage.config import load_config
from triage.labels import fit_label_map

FIELDS = ("cmdb_ci", "assignment_group", "category", "subcategory", "priority")
INSTRUCTIONS = {
    "cmdb_ci": "Which configuration item (application or service) is this incident about?",
    "assignment_group": "Which support team should own and resolve this incident?",
    "category": "Which category does this incident belong to?",
    "subcategory": "Which subcategory does this incident belong to?",
    "priority": "Which priority should this incident get?",
}
# Fields whose options get an automatic description from the training tickets when
# jeff_options.yaml has none. Priority levels are described in the YAML instead.
AUTO_DESCRIBE = ("cmdb_ci", "assignment_group", "category", "subcategory")
MAX_OPTIONS = 254          # Jeff answers at most 255 options; one is kept for ROUTE_TO_HUMAN
SPLITS = (("train", 0.7), ("development", 0.1), ("calibration", 0.1), ("test", 0.1))
SUITE = "servicenow-triage"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BARE_NUMBER = re.compile(r"^\d+$")  # Jeff refuses option keys that are bare numbers


def option_key(field: str, value: str) -> str:
    value = str(value).strip()
    if _BARE_NUMBER.match(value):
        return f"P{value}" if field == "priority" else f"#{value}"
    return value


def iso_dates(values: pd.Series) -> pd.Series:
    """Dates as ISO text. Read day-first ("05-09-2026" is 5 September); ISO dates parse the
    same either way. Unreadable dates become empty, and cleaning drops those tickets."""
    return pd.to_datetime(values, dayfirst=True, format="mixed", errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")


def tickets_frame(tickets: list[dict[str, Any]]) -> pd.DataFrame:
    """my_tickets dicts -> a frame shaped like the export: expected_<field> becomes <field>."""
    df = pd.DataFrame(tickets).drop(columns=["cmdb_ci"], errors="ignore")
    df = df.rename(columns={f"expected_{f}": f for f in FIELDS})
    if "number" not in df.columns:
        df["number"] = [f"T{i:07d}" for i in range(len(df))]
    df["opened_at"] = iso_dates(df["opened_at"])
    return df.replace({"": pd.NA})


def read_tickets(path: Path, cfg: dict[str, Any]) -> pd.DataFrame:
    """The ServiceNow export (CSV), or tickets in the my_tickets format (JSON, JSONL, or a CSV
    with expected_<field> columns). A relative path is relative to the current folder."""
    path = path.resolve()  # load_raw would resolve it against servicenow-triage's folder
    if path.suffix.lower() == ".csv":
        df = load_raw(cfg, str(path))
        df.columns = [c.lstrip("\ufeff").strip() for c in df.columns]  # Excel's "CSV UTF-8" starts with a BOM
        if any(c.startswith("expected_") for c in df.columns):
            return tickets_frame(df.to_dict("records"))
        df["opened_at"] = iso_dates(df["opened_at"])
        return df
    text = path.read_text(encoding="utf-8-sig")
    tickets = [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix == ".jsonl" else json.loads(text)
    return tickets_frame(tickets)


def ticket_state(ticket: dict[str, Any] | pd.Series, cfg: dict[str, Any]) -> dict[str, str]:
    """What Jeff reads about one ticket, built the same way in training and serving.
    The free text comes last, so the fields before it stay in Jeff's prompt cache."""
    opened = pd.to_datetime(ticket.get("opened_at"), dayfirst=True, format="mixed")
    return {
        "contact type": str(ticket.get("contact_type") or "unknown"),
        "impact": str(ticket.get("impact") or "unknown"),
        "opened": opened.strftime("%A %H:%M") if pd.notna(opened) else "unknown",
        "summary": clean_text(ticket.get("short_description"), cfg) or "(none)",
        "details": clean_text(ticket.get("description"), cfg) or "(none)",
    }


def _hints(train: pd.DataFrame, field: str, keys: list[str], n_words: int = 6) -> dict[str, str]:
    """For each class, the words its training tickets use far more than the others."""
    from sklearn.feature_extraction.text import CountVectorizer

    labels = train[field].map(lambda v: option_key(field, v))
    vec = CountVectorizer(min_df=2, stop_words="english", token_pattern=r"(?u)\b[a-z][a-z0-9-]{2,}\b")
    try:
        counts = vec.fit_transform(train["text"])
    except ValueError:  # too few tickets to have any word twice
        return {}
    words = vec.get_feature_names_out()
    total = counts.sum(axis=0).A1 + 1.0
    hints = {}
    for key in keys:
        mask = (labels == key).to_numpy()
        if not mask.any():
            continue
        own = counts[mask].sum(axis=0).A1
        score = own * (own / total)  # frequent in this class and mostly in this class
        top = [words[i] for i in score.argsort()[::-1][:n_words] if own[i] > 0]
        if top:
            hints[key] = "Tickets usually mention: " + ", ".join(top)
    return hints


def fit_options(train: pd.DataFrame, cfg: dict[str, Any], descriptions: dict[str, dict[str, str]],
                min_count: int | None = None) -> dict[str, dict[str, Any]]:
    """The question for each field: its instructions and its options, learned from the
    training tickets only. Rare classes become ROUTE_TO_HUMAN, as in the sklearn model."""
    other = cfg["labels"]["other_label"]
    questions: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        if field not in train.columns or train[field].dropna().empty:
            continue
        local = cfg if min_count is None else {**cfg, "labels": {**cfg["labels"], "min_class_count": {field: min_count}}}
        label_map = fit_label_map(train, field, local)
        counts = train[field].astype("string").str.strip().replace(label_map.merge).value_counts()
        # the most common classes if there are too many, then in alphabetical order, so an
        # option's position says nothing about how likely it is
        known = sorted([v for v in counts.index if v in label_map.known][:MAX_OPTIONS])
        keys = [option_key(field, v) for v in known]
        given = descriptions.get(field, {}) or {}
        auto = _hints(train.dropna(subset=[field]), field, keys) if field in AUTO_DESCRIBE else {}
        criteria = {k: given.get(k) or auto.get(k) or k for k in keys}
        criteria[other] = given.get(other) or "None of the other options: a rarely seen or unclear case; leave it for a person to route"
        questions[field] = {"type": "choice", "instructions": INSTRUCTIONS[field], "criteria": criteria,
                            "_values": dict(zip(keys, known, strict=True)), "_merge": label_map.merge}
    return questions


def label_for(question: dict[str, Any], field: str, value: Any, other: str) -> str:
    value = str(value).strip()
    value = question["_merge"].get(value, value)
    key = option_key(field, value)
    return key if key in question["_values"] else other


def to_rows(df: pd.DataFrame, questions: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    other = cfg["labels"]["other_label"]
    rows = []
    for _, ticket in df.iterrows():
        state = ticket_state(ticket, cfg)
        week = "%d-W%02d" % ticket["opened_at"].isocalendar()[:2]
        for field, q in questions.items():
            value = ticket.get(field)
            if value is None or pd.isna(value) or not str(value).strip():
                continue  # no known answer for this field
            label = label_for(q, field, value, other)
            rows.append({
                "id": f"{ticket['number']}-{field}", "suite": SUITE, "family": week, "state": state,
                "question": {k: v for k, v in q.items() if not k.startswith("_")},
                "label": label, "target": label,
                "source": {"dataset": "servicenow-export", "field": field, "ticket": str(ticket["number"])},
            })
    return rows


def time_split(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Whole ISO weeks per split, oldest first: train 70%, then development, calibration
    and test 10% each (by tickets, as closely as whole weeks allow)."""
    week = df["opened_at"].dt.to_period("W")
    weeks = week.value_counts().sort_index()
    if len(weeks) < len(SPLITS):
        raise SystemExit(f"Only {len(weeks)} week(s) of tickets: a time split needs at least {len(SPLITS)}. "
                         "Use --no-split, or export more history.")
    before = ((weeks.cumsum() - weeks) / weeks.sum()).tolist()  # share of tickets older than each week
    bounds = pd.Series([frac for _, frac in SPLITS]).cumsum().tolist()
    idx: list[int] = []
    for i, s in enumerate(before):
        j = next(k for k, b in enumerate(bounds) if s < b - 1e-9)
        j = max(j, len(SPLITS) - (len(weeks) - i))         # leave a week for every later split
        idx.append(0 if not idx else min(j, idx[-1] + 1))  # and skip none
    names = [SPLITS[j][0] for j in idx]
    assign = week.map(dict(zip(weeks.index, names, strict=True)))
    return {name: df[assign == name] for name, _ in SPLITS}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build(df: pd.DataFrame, cfg: dict[str, Any], out: Path, descriptions: dict[str, dict[str, str]] | None = None,
          min_count: int | None = None, split: bool = True) -> dict[str, int]:
    """Clean, split, fit the options on train, write the row files and options.json."""
    df, removed = clean_rows(df, cfg)
    parts = time_split(df) if split else {"rows": df}
    first = parts["train"] if split else df
    questions = fit_options(first, cfg, descriptions or {}, min_count)
    out.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name, part in parts.items():
        rows = to_rows(part, questions, cfg)
        write_jsonl(out / f"{name}.jsonl", rows)
        sizes[name] = len(rows)
    saved = {f: {"type": q["type"], "instructions": q["instructions"], "criteria": q["criteria"]} for f, q in questions.items()}
    (out / "options.json").write_text(json.dumps(saved, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"tickets": len(df), "removed": removed["total_removed"], **sizes,
            **{f"options_{f}": len(q["criteria"]) for f, q in questions.items()}}


def drop_leaks(out: Path, report: Path) -> tuple[int, int]:
    """Remove from train.jsonl every ticket that jeff-kit leak-check matched to an evaluation
    row (--report file). Real queues repeat themselves ("outlook not opening"), so some
    training tickets copy later ones; the kit's rule is to drop those, never to edit the test."""
    flagged = {item["train_id"] for items in json.loads(report.read_text(encoding="utf-8")).values() for item in items}
    rows = [json.loads(line) for line in (out / "train.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    tickets = {r["source"]["ticket"] for r in rows if r["id"] in flagged}
    kept = [r for r in rows if r["source"]["ticket"] not in tickets]
    write_jsonl(out / "train.jsonl", kept)
    return len(tickets), len(rows) - len(kept)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="servicenow-triage's config.yaml (default: the installed one)")
    ap.add_argument("--data", help="CSV export, or JSON/JSONL in the my_tickets format")
    ap.add_argument("--out", type=Path, default=Path("data/jeff"))
    ap.add_argument("--options", type=Path, default=PROJECT_ROOT / "jeff_options.yaml",
                    help="Descriptions of the options (optional)")
    ap.add_argument("--min-count", type=int, help="Override labels.min_class_count for every field")
    ap.add_argument("--no-split", action="store_true", help="Write one rows.jsonl (for jeff-kit split) instead")
    ap.add_argument("--drop-leaks", type=Path, help="A jeff-kit leak-check --report file: drop those training tickets")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.drop_leaks:
        tickets, rows = drop_leaks(a.out, a.drop_leaks)
        print(f"  dropped {tickets:,} training tickets ({rows:,} rows) that copy an evaluation ticket")
        return
    if not a.data:
        ap.error("--data is required")
    descriptions = (yaml.safe_load(a.options.read_text(encoding="utf-8")) or {}) if a.options.exists() else {}
    sizes = build(read_tickets(Path(a.data), cfg), cfg, a.out, descriptions, a.min_count, not a.no_split)
    for k, v in sizes.items():
        print(f"  {k}: {v:,}")


if __name__ == "__main__":
    main()

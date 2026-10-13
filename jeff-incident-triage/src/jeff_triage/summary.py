"""What the row files hold, per field: before training, check the bar Jeff has to beat.

    python -m jeff_triage.summary data/jeff

For each field: rows in train and test, number of options, the share labelled
ROUTE_TO_HUMAN (rare or new classes), the score of always answering the most common
label on test (the baseline), and how many options still lack a hand-written description
in jeff_options.yaml.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

OTHER = "ROUTE_TO_HUMAN"
AUTO = "Tickets usually mention:"


def counts(path: Path) -> dict[str, Counter[str]]:
    by_field: dict[str, Counter[str]] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            by_field.setdefault(row["source"]["field"], Counter())[row["label"]] += 1
    return by_field


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, nargs="?", default=Path("data/jeff"))
    ap.add_argument("--top", type=int, default=5, help="Most common labels to list per field")
    a = ap.parse_args()
    train, test = counts(a.folder / "train.jsonl"), counts(a.folder / "test.jsonl")
    options = json.loads((a.folder / "options.json").read_text(encoding="utf-8"))
    print(f"{'field':<18}{'train':>8}{'test':>7}{'options':>9}{'routed train':>14}{'routed test':>13}"
          f"{'baseline':>10}{'undescribed':>13}")
    for field, q in options.items():
        tr, te = train.get(field, Counter()), test.get(field, Counter())
        n_tr, n_te = sum(tr.values()), sum(te.values())
        common = tr.most_common(1)[0][0] if tr else None
        baseline = 100 * te[common] / n_te if n_te else float("nan")
        bare = sum(1 for k, v in q["criteria"].items() if k != OTHER and (v == k or str(v).startswith(AUTO)))
        print(f"{field:<18}{n_tr:>8,}{n_te:>7,}{len(q['criteria']):>9}"
              f"{100 * tr[OTHER] / max(n_tr, 1):>13.1f}%{100 * te[OTHER] / max(n_te, 1):>12.1f}%"
              f"{baseline:>9.1f}%{bare:>13}")
    print("\nbaseline: always answering the most common training label, scored on test. Jeff must beat it clearly.")
    print("routed: tickets labelled ROUTE_TO_HUMAN. A high share means many small or new teams/CIs.")
    print("undescribed: options with no description in jeff_options.yaml (they get an automatic hint).\n")
    for field in options:
        tr = train.get(field, Counter())
        n = max(sum(tr.values()), 1)
        top = ", ".join(f"{k} {100 * v / n:.0f}%" for k, v in tr.most_common(a.top))
        print(f"{field}: {top}")


if __name__ == "__main__":
    main()

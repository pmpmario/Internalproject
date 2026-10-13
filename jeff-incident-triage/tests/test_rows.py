"""Jeff rows: the my_tickets format, the time split, and the row rules jeff-kit check-rows enforces."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).parents[2] / "servicenow-triage" / "tests"))
from synthetic import make  # noqa: E402  (servicenow-triage's fake incidents)

from triage.config import load_config
from jeff_triage.rows import build, read_tickets, tickets_frame

EXAMPLE = Path(__file__).parents[1] / "examples" / "my_tickets.json"


def check_row(row: dict) -> None:
    """The checks of jeff-kit check-rows (jeff/kit/rows.py) that these rows could break."""
    assert {"id", "suite", "family", "state", "question", "label", "target", "source"} <= set(row)
    criteria = row["question"]["criteria"]
    assert 2 <= len(criteria) <= 255
    assert not any(re.fullmatch(r"\d+", k) for k in criteria)
    assert all(v for v in criteria.values())
    assert row["label"] in criteria and row["target"] == row["label"]
    assert not re.search(r"\{\{[^{}]*\}\}", json.dumps(row))


def test_my_tickets_format():
    cfg = load_config()
    df = read_tickets(EXAMPLE, cfg)
    assert df.loc[0, "opened_at"] == "2026-09-15 10:15:00"  # day-first
    assert df.loc[0, "cmdb_ci"] == "S.A.MS-OUTLOOK"         # the expected CI, not the caller's empty one
    assert tickets_frame([{"opened_at": "2026-01-02 08:00:00", "short_description": "x"}]).loc[0, "number"]


def test_single_ticket_without_split(tmp_path):
    cfg = load_config()
    sizes = build(read_tickets(EXAMPLE, cfg), cfg, tmp_path, min_count=1, split=False)
    rows = [json.loads(line) for line in (tmp_path / "rows.jsonl").read_text().splitlines()]
    assert sizes["rows"] == len(rows) == 5
    by_field = {r["source"]["field"]: r for r in rows}
    assert by_field["assignment_group"]["label"] == "IMG-SMC-TCC-ITSC"
    assert by_field["priority"]["label"] == "P3"
    assert by_field["cmdb_ci"]["state"]["summary"] == "unable to open outlook mailbox full"
    for row in rows:
        check_row(row)


def test_time_split_on_export(tmp_path):
    cfg = load_config()
    descriptions = yaml.safe_load((Path(__file__).parents[1] / "jeff_options.yaml").read_text())
    build(make(3000, seed=2), cfg, tmp_path, descriptions)
    parts = {n: [json.loads(x) for x in (tmp_path / f"{n}.jsonl").read_text().splitlines()]
             for n in ("train", "development", "calibration", "test")}
    weeks = {n: {r["family"] for r in rows} for n, rows in parts.items()}
    assert all(weeks.values())
    names = list(weeks)
    for i, a in enumerate(names):  # no week in two splits, and the splits follow each other in time
        for b in names[i + 1:]:
            assert not weeks[a] & weeks[b]
            assert max(weeks[a]) < min(weeks[b])
    n_train = len({r["source"]["ticket"] for r in parts["train"]})
    n_all = sum(len({r["source"]["ticket"] for r in rows}) for rows in parts.values())
    assert 0.6 < n_train / n_all < 0.8
    options = json.loads((tmp_path / "options.json").read_text())
    assert "ROUTE_TO_HUMAN" in options["assignment_group"]["criteria"]
    assert not any(k.startswith("Tiny-Team") for k in options["assignment_group"]["criteria"])  # folded, < 25 tickets
    assert options["cmdb_ci"]["criteria"]["S.A.MS-OUTLOOK"].startswith("Microsoft Outlook")  # from jeff_options.yaml
    assert options["cmdb_ci"]["criteria"]["S.A.RESCONNECT"].startswith("Tickets usually mention:")  # automatic
    for rows in parts.values():
        for row in rows:
            check_row(row)
            assert row["question"] == options[row["source"]["field"]]
    assert pd.Series([r["label"] for r in parts["test"] if r["source"]["field"] == "assignment_group"]).nunique() > 1

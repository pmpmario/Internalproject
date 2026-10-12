"""Stage 2: label hygiene.

- Apply merges agreed with the teams (labels.merge_map).
- Fold classes with too few training tickets into one "ROUTE_TO_HUMAN" class,
  so the model says "I don't know this one" instead of guessing a tiny team.
- Mark urgent tickets (P1/P2) for the urgency gate.

The mapping is learned from the TRAINING tickets only and stored in the model,
so test and live tickets are mapped exactly the same way.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class LabelMap:
    target: str
    merge: dict[str, str]
    known: set[str]
    other_label: str
    folded: dict[str, int] = field(default_factory=dict)  # class -> train count, for the report

    def transform(self, s: pd.Series) -> pd.Series:
        s = s.astype("string").str.strip().replace(self.merge)
        return s.where(s.isin(self.known) | s.isna(), self.other_label)


def fit_label_map(train: pd.DataFrame, target: str, cfg: dict[str, Any]) -> LabelMap:
    lab = cfg["labels"]
    merge = (lab.get("merge_map") or {}).get(target) or {}
    s = train[target].astype("string").str.strip().replace(merge).dropna()
    counts = s.value_counts()
    min_n = (lab.get("min_class_count") or {}).get(target, 1)
    known = set(counts[counts >= min_n].index)
    folded = counts[counts < min_n].to_dict()
    return LabelMap(target, dict(merge), known, lab["other_label"], folded)


_LEVEL = re.compile(r"(\d)")


def priority_level(p: Any) -> float:
    """'2 - High' / 'P2' / '2' -> 2. Unknown -> NaN."""
    if p is None or (isinstance(p, float) and pd.isna(p)):
        return float("nan")
    m = _LEVEL.search(str(p))
    return float(m.group(1)) if m else float("nan")


def urgent_labels(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.Series:
    u = cfg["urgency"]
    lvl = df[u["priority_field"]].map(priority_level)
    return lvl.isin(u["urgent_levels"]).astype(int).where(lvl.notna())

"""Stage 1: clean ServiceNow incidents.

Two levels:
- clean_text(): one ticket's text. Used in training AND in serving, so the
  model always sees text prepared the same way.
- clean_rows(): the training export (duplicates, test tickets, cancelled...).

Run on its own to inspect the result:
    python -m triage.clean --config config.yaml --out data/clean.csv
"""
from __future__ import annotations

import argparse
import html
import re
from functools import lru_cache
from typing import Any

import pandas as pd

from triage.config import load_config, resolve

# Personal / identifying data is replaced by a token before anything else.
# Order matters: the specific patterns run before the generic number one.
_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), " __email__ "),
    (re.compile(r"https?://\S+|www\.\S+"), " __url__ "),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), " __ip__ "),
    (re.compile(r"\b(?:INC|RITM|REQ|CHG|PRB|TASK|SCTASK)\d{5,}\b", re.I), " __ticket__ "),
    (re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"), " __phone__ "),
    # PNR / booking reference: 6 upper-case letters+digits with at least one digit
    (re.compile(r"\b(?=[A-Z0-9]{6}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6}\b"), " __ref__ "),
    (re.compile(r"\b[A-Z]{0,2}\d{5,}\b", re.I), " __num__ "),
]
_TAGS = re.compile(r"<[^<>]{1,200}>")
_NON_TEXT = re.compile(r"[^\w\s.-]+")
_SPACES = re.compile(r"\s+")


@lru_cache(maxsize=8)
def _compiled(cut_markers: tuple[str, ...], synonyms: tuple[tuple[str, str], ...]):
    cuts = [re.compile(p, re.I | re.M) for p in cut_markers]
    syns = [(re.compile(rf"\b{re.escape(a)}\b"), b) for a, b in synonyms]
    return cuts, syns


def _cleaning_rules(cfg: dict[str, Any]):
    c = cfg.get("cleaning", {})
    return _compiled(tuple(c.get("cut_markers", [])), tuple(sorted(c.get("synonyms", {}).items())))


def clean_text(text: Any, cfg: dict[str, Any]) -> str:
    """Strip HTML, reply chains and footers, mask personal data, normalise."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    s = html.unescape(str(text))
    s = _TAGS.sub(" ", s)
    cuts, syns = _cleaning_rules(cfg)
    for cut in cuts:
        m = cut.search(s)
        if m and m.start() > 0:  # never cut the whole ticket away
            s = s[: m.start()]
    for pattern, token in _MASKS:
        s = pattern.sub(token, s)
    s = s.lower()
    s = _NON_TEXT.sub(" ", s)
    for pattern, repl in syns:
        s = pattern.sub(repl, s)
    return _SPACES.sub(" ", s).strip()


def build_text(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.Series:
    """The single text field the model reads: short description + description."""
    d = cfg["data"]
    short = df.get(d["short_description_col"], pd.Series("", index=df.index))
    desc = df.get(d["description_col"], pd.Series("", index=df.index))
    return (short.map(lambda t: clean_text(t, cfg)) + " " + desc.map(lambda t: clean_text(t, cfg))).str.strip()


def load_raw(cfg: dict[str, Any], path: str | None = None) -> pd.DataFrame:
    p = resolve(cfg, path or cfg["data"]["path"])
    df = pd.read_csv(p, dtype=str, keep_default_na=False, na_values=[""], encoding_errors="replace")
    df.columns = [c.strip() for c in df.columns]
    return df


def clean_rows(df: pd.DataFrame, cfg: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, int]]:
    """Clean the export. Returns the cleaned frame (with a `text` column) and a
    count of rows removed per reason, for the training report."""
    d = cfg["data"]
    removed: dict[str, int] = {}
    n0 = len(df)
    df = df.copy()

    def note(reason: str, before: int) -> None:
        removed[reason] = before - len(df)

    df[d["date_col"]] = pd.to_datetime(df[d["date_col"]], errors="coerce")
    before = len(df)
    df = df[df[d["date_col"]].notna()]
    note("bad_or_missing_opened_at", before)

    before = len(df)
    df = df.drop_duplicates(subset=[d["id_col"]], keep="last")
    note("duplicate_number", before)

    if d.get("state_col") in df.columns and d.get("drop_states"):
        before = len(df)
        df = df[~df[d["state_col"]].isin(d["drop_states"])]
        note("dropped_state", before)

    df["text"] = build_text(df, cfg)
    before = len(df)
    df = df[df["text"].str.len() > 0]
    note("empty_text", before)

    if d.get("drop_text_patterns"):
        before = len(df)
        rx = re.compile("|".join(f"(?:{p})" for p in d["drop_text_patterns"]), re.I)
        short = df[d["short_description_col"]].fillna("").astype(str).str.strip()
        df = df[~short.map(lambda s: bool(rx.search(s)))]
        note("test_ticket", before)

    # Near-duplicates: the same caller raising the same ticket again within minutes,
    # or alert storms. Keep the first one.
    keys = [k for k in d.get("duplicate_key_cols", []) if k in df.columns]
    window = d.get("duplicate_window_minutes")
    if keys and window:
        before = len(df)
        df = df.sort_values(d["date_col"])
        gap = df.groupby(keys, dropna=False)[d["date_col"]].diff()
        df = df[~(gap.notna() & (gap <= pd.Timedelta(minutes=window)))]
        note("near_duplicate", before)

    df = df.sort_values(d["date_col"]).reset_index(drop=True)
    removed["total_removed"] = n0 - len(df)
    return df, removed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--data", help="CSV export (default: data.path in config)")
    ap.add_argument("--out", default="data/clean.csv")
    a = ap.parse_args()
    cfg = load_config(a.config)
    df, removed = clean_rows(load_raw(cfg, a.data), cfg)
    out = resolve(cfg, a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out) if out.suffix == ".parquet" else df.to_csv(out, index=False)
    print(f"{len(df):,} clean tickets -> {out}")
    for k, v in removed.items():
        print(f"  {k}: {v:,}")


if __name__ == "__main__":
    main()

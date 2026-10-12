"""Stage 3: features. One sklearn Pipeline per field, so the exact transform
used in training is saved inside the model and reused when serving.

- text: TF-IDF on the cleaned short description + description (words and pairs)
- optional creation-time categoricals (contact type, caller, impact...)
- optional hour and weekday of opened_at
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

TIME_COLS = ["opened_hour", "opened_weekday"]


def add_time_features(df: pd.DataFrame, cfg: dict[str, Any]) -> pd.DataFrame:
    opened = pd.to_datetime(df[cfg["data"]["date_col"]], errors="coerce")
    df = df.copy()
    df["opened_hour"] = opened.dt.hour.astype("string").fillna("na")
    df["opened_weekday"] = opened.dt.dayofweek.astype("string").fillna("na")
    return df


def input_columns(target: str, cfg: dict[str, Any]) -> list[str]:
    spec = cfg["targets"][target]
    return list(spec.get("categorical", [])) + (TIME_COLS if spec.get("time_features") else [])


def make_pipeline(target: str, cfg: dict[str, Any], class_weight: str | None = None) -> Pipeline:
    m = cfg["model"]
    parts: list[tuple[str, Any, Any]] = [
        (
            "text",
            TfidfVectorizer(
                ngram_range=tuple(m["ngram_range"]),
                max_features=m["max_features"],
                sublinear_tf=True,
                min_df=2,
                dtype=np.float32,
            ),
            "text",
        )
    ]
    cats = input_columns(target, cfg)
    if cats:
        parts.append(
            (
                "cat",
                OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=m["min_category_frequency"]),
                cats,
            )
        )
    return Pipeline(
        [
            ("features", ColumnTransformer(parts, sparse_threshold=1.0)),
            (
                "clf",
                LogisticRegression(
                    C=m["C"], max_iter=m["max_iter"], class_weight=class_weight, random_state=m["random_state"]
                ),
            ),
        ]
    )


def model_frame(df: pd.DataFrame, target: str, cfg: dict[str, Any]) -> pd.DataFrame:
    """The columns a target's pipeline reads, with missing values filled."""
    cols = input_columns(target, cfg)
    if any(c in TIME_COLS for c in cols) and "opened_hour" not in df.columns:
        df = add_time_features(df, cfg)
    out = pd.DataFrame({"text": df["text"].fillna("").astype(object)}, index=df.index)
    for c in cols:
        col = df[c].astype("string") if c in df.columns else pd.Series(pd.NA, index=df.index, dtype="string")
        out[c] = col.fillna("na").astype(object)
    return out

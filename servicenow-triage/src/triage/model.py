"""The triage model: one logistic regression per field, an urgency gate, and the
decision rules (cut-offs, fallback, never-auto lists) that turn predictions
into actions. Everything needed to predict lives in this one object, so the
saved file is self-contained.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import Pipeline

from triage.clean import build_text
from triage.config import enabled_targets
from triage.features import make_pipeline, model_frame
from triage.labels import LabelMap, fit_label_map, priority_level, urgent_labels

log = logging.getLogger(__name__)

# Config sections the model needs at prediction time (copied into the model so
# a saved model keeps behaving the same even if config.yaml changes later).
_RUNTIME_SECTIONS = ["data", "cleaning", "targets", "labels", "cutoffs", "auto_fields", "never_auto", "urgency", "routing"]
_GATE = "_urgency_gate"


def _gate_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """The urgency gate reads the text only."""
    return {**cfg, "targets": {_GATE: {}}}


@dataclass
class TriageModel:
    cfg: dict[str, Any]
    pipelines: dict[str, Pipeline] = field(default_factory=dict)
    label_maps: dict[str, LabelMap] = field(default_factory=dict)
    urgency: Pipeline | None = None
    urgency_threshold: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ fit
    @classmethod
    def fit(cls, train: pd.DataFrame, cfg: dict[str, Any]) -> "TriageModel":
        """`train` is the output of clean.clean_rows (has a `text` column)."""
        runtime_cfg = {k: cfg.get(k) for k in _RUNTIME_SECTIONS}
        runtime_cfg["model"] = cfg["model"]
        m = cls(cfg=runtime_cfg)
        date_col = cfg["data"]["date_col"]
        m.meta.update(
            trained_from=str(train[date_col].min()),
            trained_to=str(train[date_col].max()),
            n_train=int(len(train)),
            majority={},
            train_counts={},
        )
        for target in enabled_targets(cfg):
            if target not in train.columns:
                log.warning("target %s not in data, skipped", target)
                continue
            lm = fit_label_map(train, target, cfg)
            y = lm.transform(train[target])
            rows = y.notna()
            if y[rows].nunique() < 2:
                log.warning("target %s has fewer than 2 classes after cleaning, skipped", target)
                continue
            log.info("fitting %s on %d tickets, %d classes", target, rows.sum(), y[rows].nunique())
            pipe = make_pipeline(target, cfg)
            pipe.fit(model_frame(train[rows], target, cfg), y[rows].astype(str).to_numpy())
            m.pipelines[target] = pipe
            m.label_maps[target] = lm
            m.meta["majority"][target] = str(y[rows].mode().iloc[0])
            m.meta["train_counts"][target] = {str(k): int(v) for k, v in y[rows].value_counts().items()}
        m._fit_urgency(train, cfg)
        return m

    def _fit_urgency(self, train: pd.DataFrame, cfg: dict[str, Any]) -> None:
        """Text-only binary model for P1/P2. Its threshold is chosen from
        out-of-fold predictions on the training tickets (never the test ones)
        so it catches `target_recall` of urgent tickets."""
        if cfg["urgency"]["priority_field"] not in train.columns:
            return
        y = urgent_labels(train, cfg)
        rows = y.notna()
        n_urgent = int(y[rows].sum())
        if n_urgent < 10:
            log.warning("only %d urgent tickets: urgency gate disabled", n_urgent)
            return
        X, yy = model_frame(train[rows], _GATE, _gate_cfg(cfg)), y[rows].astype(int)
        pipe = make_pipeline(_GATE, _gate_cfg(cfg), class_weight="balanced")
        folds = StratifiedKFold(n_splits=min(3, n_urgent), shuffle=True, random_state=cfg["model"]["random_state"])
        oof = cross_val_predict(pipe, X, yy.to_numpy(), cv=folds, method="predict_proba")[:, 1]
        urgent_scores = np.sort(oof[yy.to_numpy() == 1])
        self.urgency_threshold = float(np.quantile(urgent_scores, 1 - cfg["urgency"]["target_recall"]))
        self.urgency = pipe.fit(X, yy.to_numpy())
        self.meta["urgency_threshold"] = self.urgency_threshold

    # -------------------------------------------------------------- predict
    def prepare(self, tickets: pd.DataFrame | list[dict[str, Any]]) -> pd.DataFrame:
        df = pd.DataFrame(tickets) if not isinstance(tickets, pd.DataFrame) else tickets.copy()
        if "text" not in df.columns:
            df["text"] = build_text(df, self.cfg)
        return df

    def predict(self, tickets: pd.DataFrame | list[dict[str, Any]]) -> pd.DataFrame:
        """Predictions, confidences and the actions to take, one row per ticket."""
        df = self.prepare(tickets)
        cfg, other = self.cfg, self.cfg["labels"]["other_label"]
        out = pd.DataFrame(index=df.index)
        id_col = cfg["data"]["id_col"]
        if id_col in df.columns:
            out[id_col] = df[id_col]

        for target, pipe in self.pipelines.items():
            proba = pipe.predict_proba(model_frame(df, target, cfg))
            best = proba.argmax(axis=1)
            out[f"pred_{target}"] = pipe.classes_[best]
            out[f"conf_{target}"] = proba[np.arange(len(df)), best].round(4)

        # Automatic actions only above the cut-off, never for the fallback class
        # or for classes listed in never_auto.
        for target in cfg.get("auto_fields") or []:
            if target not in self.pipelines:
                continue
            never = set((cfg.get("never_auto") or {}).get(target) or [])
            out[f"auto_{target}"] = (
                (out[f"conf_{target}"] >= cfg["cutoffs"][target])
                & (out[f"pred_{target}"] != other)
                & ~out[f"pred_{target}"].isin(never)
            )

        r = cfg["routing"]
        if "assignment_group" in self.pipelines:
            auto = out.get("auto_assignment_group", pd.Series(False, index=out.index))
            out["routed_group"] = out["pred_assignment_group"].where(auto, r["fallback_group"])
            g = out["pred_assignment_group"]
            out["reassignment_count"] = np.where(g == r["itsc_group"], 1, np.where(g.str.startswith(r["near_prefix"]), 2, 3))

        urgent = pd.Series(False, index=out.index)
        if "priority" in self.pipelines:
            urgent |= out["pred_priority"].map(priority_level).isin(cfg["urgency"]["urgent_levels"])
        if self.urgency is not None:
            score = self.urgency.predict_proba(model_frame(df, _GATE, _gate_cfg(cfg)))[:, 1]
            out["urgency_score"] = score.round(4)
            urgent |= score >= self.urgency_threshold
        out["urgent_alert"] = urgent

        ci_col = cfg["data"].get("submitted_ci_col")
        if "cmdb_ci" in self.pipelines and ci_col in df.columns:
            submitted = df[ci_col].astype("string").str.strip()
            out["ci_mismatch"] = (
                submitted.notna()
                & (submitted != out["pred_cmdb_ci"])
                & (out["pred_cmdb_ci"] != other)
                & (out["conf_cmdb_ci"] >= cfg["cutoffs"]["ci_mismatch"])
            ).fillna(False).astype(bool)
        return out

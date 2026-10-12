"""Stage 8: prediction API called by ServiceNow when an incident is created.

    export TRIAGE_API_KEY=...            # shared secret, also stored in ServiceNow
    export TRIAGE_CONFIG=/path/config.yaml
    uvicorn triage.serve:app --host 0.0.0.0 --port 8080 --workers 2

    GET  /health        -> model version, no auth (for the load balancer)
    POST /predict       -> predictions for one or more tickets   (X-API-Key)
    POST /admin/reload  -> load the current PRODUCTION model     (X-API-Key)

The prediction log holds ticket numbers, predictions and confidences only:
never the ticket text, never the caller.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from triage import registry
from triage.config import load_config, resolve
from triage.model import TriageModel

log = logging.getLogger("triage.serve")


class Ticket(BaseModel):
    """Fields known when the incident is created. Extra fields are accepted."""
    model_config = ConfigDict(extra="allow")
    number: str | None = None
    opened_at: str | None = None
    short_description: str = ""
    description: str = ""
    contact_type: str | None = None
    caller_id: str | None = None
    sys_created_by: str | None = None
    impact: str | None = None
    cmdb_ci: str | None = Field(None, description="CI the requester selected (compared, never used as a feature)")


class PredictRequest(BaseModel):
    tickets: list[Ticket] = Field(..., min_length=1, max_length=500)


class State:
    def __init__(self) -> None:
        self.cfg = load_config(os.environ.get("TRIAGE_CONFIG"))
        self.models_dir = resolve(self.cfg, self.cfg["paths"]["models_dir"])
        self.log_path = resolve(self.cfg, self.cfg["paths"]["prediction_log"])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_key = os.environ.get("TRIAGE_API_KEY", "")
        if not self.api_key and os.environ.get("TRIAGE_ALLOW_NO_AUTH") != "1":
            raise RuntimeError("set TRIAGE_API_KEY (or TRIAGE_ALLOW_NO_AUTH=1 for local testing only)")
        self.lock = threading.Lock()
        self.model: TriageModel = registry.load(self.models_dir)

    def reload(self) -> str:
        model = registry.load(self.models_dir)  # verify before swapping
        self.model = model
        return model.meta["version"]


_state: State | None = None


def state() -> State:
    global _state
    if _state is None:
        _state = State()
    return _state


def check_key(x_api_key: str = Header(default="")) -> None:
    s = state()
    if s.api_key and not hmac.compare_digest(x_api_key.encode(), s.api_key.encode()):
        raise HTTPException(status_code=401, detail="invalid API key")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("serving model %s", state().model.meta["version"])  # fail fast if no valid model
    yield


app = FastAPI(title="ServiceNow triage", version="1", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    m = state().model
    return {"status": "ok", "model_version": m.meta["version"], "trained_to": m.meta["trained_to"]}


@app.post("/predict", dependencies=[Depends(check_key)])
def predict(req: PredictRequest) -> dict[str, Any]:
    s = state()
    model = s.model
    tickets = pd.DataFrame([t.model_dump() for t in req.tickets])
    out = model.predict(tickets)
    records = json.loads(out.to_json(orient="records"))
    version = model.meta["version"]
    _log_predictions(s, records, version, tickets.get("opened_at"))
    return {"model_version": version, "results": records}


@app.post("/admin/reload", dependencies=[Depends(check_key)])
def reload_model() -> dict[str, str]:
    try:
        return {"model_version": state().reload()}
    except Exception as e:  # keep serving the old model
        raise HTTPException(status_code=500, detail=f"reload failed, still serving the previous model: {e}") from e


def _log_predictions(s: State, records: list[dict[str, Any]], version: str, opened: pd.Series | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    lines = []
    for i, r in enumerate(records):
        entry = {"logged_at": now, "model_version": version,
                 "opened_at": None if opened is None else opened.iloc[i], **r}
        lines.append(json.dumps(entry, default=str))
    with s.lock, open(s.log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

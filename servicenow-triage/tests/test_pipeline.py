"""End-to-end test on synthetic data: clean -> train -> save -> promote ->
serve -> monitor -> retrain/compare -> rollback."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from synthetic import make  # noqa: E402

from triage import registry
from triage.clean import clean_text
from triage.config import DEFAULT_CONFIG, load_config


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("proj")
    cfg = yaml.safe_load(DEFAULT_CONFIG.read_text())
    cfg["data"]["path"] = "data/incidents.csv"
    cfg["promotion"]["min_compare_tickets"] = 50
    cfg["model"]["max_iter"] = 200
    (root / "config.yaml").write_text(yaml.safe_dump(cfg))
    (root / "data").mkdir()
    make(3000, seed=1).to_csv(root / "data/incidents.csv", index=False)
    return root


def test_clean_text_masks_personal_data_and_cuts_reply_chain():
    cfg = load_config()
    raw = ("<p>Outlook not opening</p> call +971 4 123 4567, mail john.doe@example.com, PNR AB12CD, staff 4512345\n"
           "-----Original Message-----\nFrom: x\nold thread about resconnect")
    out = clean_text(raw, cfg)
    assert "john" not in out and "4512345" not in out and "ab12cd" not in out
    assert "__email__" in out and "__phone__" in out and "__ref__" in out
    assert "resconnect" not in out  # reply chain removed
    assert out.startswith("outlook not opening")
    assert clean_text("My Learning Zone broken", cfg) == "mylearningzone broken"
    assert clean_text(None, cfg) == ""


def test_train_promote_serve_monitor(project: Path, monkeypatch):
    from triage.train import run

    cfg = load_config(project / "config.yaml")
    r = run(cfg, promote="yes")
    assert r["promoted"]
    f = r["metrics"]["fields"]
    assert f["cmdb_ci"]["accuracy"] > 90
    assert f["assignment_group"]["accuracy"] > f["assignment_group"]["baseline"]
    assert "category" not in f  # disabled in config
    assert (project / "reports" / r["version"] / "report.md").exists()

    models_dir = project / "models"
    model = registry.load(models_dir)
    assert model.meta["version"] == r["version"]
    # tiny teams were folded into the human bucket
    assert not any(c.startswith("Tiny-Team") for c in model.label_maps["assignment_group"].known)

    # ---- API
    from fastapi.testclient import TestClient
    import triage.serve as serve

    monkeypatch.setenv("TRIAGE_CONFIG", str(project / "config.yaml"))
    monkeypatch.setenv("TRIAGE_API_KEY", "secret")
    serve._state = None
    with TestClient(serve.app) as client:
        assert client.get("/health").json()["model_version"] == r["version"]
        body = {"tickets": [
            {"number": "INC9000001", "opened_at": "2026-09-01 10:00:00", "short_description": "resconnect timeout",
             "description": "booking screen frozen for agent", "contact_type": "Phone", "impact": "3 - Low",
             "cmdb_ci": "S.A.MS-OUTLOOK"},
            {"number": "INC9000002", "short_description": "something nobody has seen before"},
        ]}
        assert client.post("/predict", json=body).status_code == 401
        resp = client.post("/predict", json=body, headers={"X-API-Key": "secret"})
        assert resp.status_code == 200, resp.text
        res = resp.json()["results"]
        assert res[0]["pred_cmdb_ci"] == "S.A.RESCONNECT"
        assert res[0]["ci_mismatch"] is True  # requester picked Outlook
        assert res[0]["routed_group"]  # either a team or the fallback
        assert {"urgent_alert", "conf_assignment_group", "auto_assignment_group"} <= res[0].keys()
    logged = (project / "logs/predictions.jsonl").read_text()
    assert "INC9000001" in logged and "booking screen" not in logged  # no ticket text in the log

    # ---- monitoring on newer tickets the model never saw
    newer = make(600, seed=2)
    newer["opened_at"] = (pd.Timestamp(model.meta["trained_to"]) + pd.to_timedelta(range(1, 601), unit="h")).astype(str)
    newer["number"] = [f"INC8{i:06d}" for i in range(600)]
    newer.to_csv(project / "data/new.csv", index=False)
    from triage import monitor

    monkeypatch.setattr(sys, "argv", ["monitor", "--config", str(project / "config.yaml"),
                                      "--actuals", str(project / "data/new.csv"), "--rescore"])
    try:
        monitor.main()
    except SystemExit as e:  # exit 1 only if an alert fired; report must exist either way
        assert e.code in (0, 1, None)
    assert list((project / "reports").glob("monitor-*/report.md"))

    # ---- retrain with more data; comparison with production runs on unseen tickets
    combined = pd.concat([pd.read_csv(project / "data/incidents.csv"), newer])
    combined.to_csv(project / "data/incidents.csv", index=False)
    r2 = run(cfg, promote="auto")
    assert r2["comparison"]["decision"] in ("promote", "keep")
    assert r2["comparison"].get("n_compared", 0) >= 50 or "only" in r2["comparison"]["why"]

    # ---- rollback
    registry.promote(models_dir, r2["version"], reason="test")
    registry.promote(models_dir, r2["version"], reason="promoting twice is a no-op")
    assert registry.rollback(models_dir) == r["version"]
    assert registry.production_version(models_dir) == r["version"]

    # ---- tampered model is refused
    tampered = models_dir / "tampered"
    shutil.copytree(models_dir / r["version"], tampered)
    with open(tampered / "model.joblib", "ab") as fh:
        fh.write(b"x")
    with pytest.raises(RuntimeError, match="checksum"):
        registry.load(models_dir, "tampered")

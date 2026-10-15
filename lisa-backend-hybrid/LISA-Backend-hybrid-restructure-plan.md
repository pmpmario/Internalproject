# LISA-Backend (hybrid) — Restructure Plan for Production

**Goal:** a layout that is easy to maintain and has no clutter, with **nothing removed**. Every
current file has a new home in the mapping (section 4). Non-production material is kept, but moved
out of the paths that are built into images.

The plan is based on the code of `LISA-Backend-playbooks-v2-hybrid`; see
[`LISA-Backend-hybrid-blueprint.md`](LISA-Backend-hybrid-blueprint.md). The production hosting it
prepares for is in [`LISA-Backend-hybrid-hosting-techstack.md`](LISA-Backend-hybrid-hosting-techstack.md).

---

## 1. What makes the current layout hard to maintain

| Problem (found in the project) | Effect |
|---|---|
| **Two deployables in one tree, one of them hidden.** `models/` is a separate service (port 8300), but `.gitignore` excludes `models/`, and it runs from another project's environment (`run_service.bat` → `uv run --project C:/PROD/0_Servicenow_TriagingModel_LogisticRegression`) | Service code is not version-controlled; it cannot be built or deployed on its own |
| **8 Markdown files at the root** (`README`, `Workflow` 69 KB, `HANDOFF` 38 KB, `AGENTIC`, `HYBRID_CLASSIFIER`, `PLAYBOOKS`, `PLAN_reply_understanding`, `TECHSTACK`, …) plus `docs/rules/` (13 files) and two READMEs in `models/` | No single entry point; current docs and session history are mixed |
| **Runtime data inside `notebooks/`.** `context_extraction_agent.py:24` reads `notebooks/cleaned/cmdb_ci_app_clean.json` | Research folders cannot be left out of the image |
| **Duplicated code in `resolution_engine/`.** `ci_predictor/` and `kb_retrieval/` each have their own `ollama_embedding_client.py`, `qwen_reranking_client.py`, `vector_search_store.py`, `keyword_search_store.py`, `settings.py` | Every fix is made twice |
| **A misleading name.** `resolution_engine/ci_predictor/` no longer predicts the CI; it only runs the similar-incident search | Newcomers look in the wrong place |
| **Configuration spread out.** About 55 environment reads (`os.environ` / `getenv`) across 24 modules, plus 2 pydantic `Settings` classes each reading `.env`, plus the service's own variables | No single list of settings or defaults; typos fail silently |
| **Duplicated integration code.** `AzureChatOpenAI` is built in both `workflows/common.py` and `azure_reranking_client.py`; ServiceNow credentials are read in both `servicenow_client.py` and `servicenow_teams_followup.py` (the second without retries) | Inconsistent behaviour |
| **Flat `incident_graph/` root** with about 25 modules of different kinds (integrations, conversation helpers, notes, storage) | Hard to see what belongs together |
| **Tests named after fix batches** (`test_three_fixes.py`, `test_four_fixes.py`, `test_five_fixes.py`, `test_seven_fixes.py`, `test_run_analysis_fixes.py`) next to feature tests | Hard to find the tests for a feature |
| **Dependencies.** `pyproject.toml` lists about 15 libraries the code never imports. scikit-learn is no longer used by the API. `mcp` arrives only through `claude-agent-sdk`. The workspace member `scripts/foundry_hosted_langchain/...` does not exist | Big image, fragile build |
| **Windows-only tooling.** `.bat`, PowerShell, `C:/PROD` paths, CRLF files, `.checkpoints/*/restore.ps1` revert scripts | Does not run in CI or Linux containers |
| **Dead or inactive code next to live code.** `agents/shared_guidance_OLD.py` (imported nowhere); `servicenow_workflow_teams.py` + `routers/teams_webhook.py` (router not registered, but still tested) | Unclear what is live |

---

## 2. Rules for the restructure

1. **Move, don't delete.** Every file goes somewhere (section 4). Retired code goes to `archive/`;
   session notes go to `docs/history/`.
2. **One deployable = one folder under `services/`**, with its own `pyproject.toml`, `Dockerfile`,
   tests and README.
3. **Keep the three Python package names** (`dashboard_backend`, `incident_graph`,
   `resolution_engine`). Imports, test patch targets and pickled artifacts keep working.
4. **Never change these persisted contracts.** Checkpoints, the dashboard database and saved
   incidents depend on them:
   - LangGraph **node names** (`assess_completeness`, `classify_incident`, …)
   - interrupt **`action` strings** (`db.py` maps them to statuses)
   - **state keys** (incl. `ml_classification`)
   - **Qdrant collection names**
   - the module paths inside `.joblib` files (only change them together with an alias in
     `_artifact_compat.py`)
5. **One place per concern.** One settings module, one LLM factory, one ServiceNow client, one
   search toolkit.
6. **Each step leaves the 893 offline tests green**, and each phase is a separate pull request.

---

## 3. Target layout

```
lisa-backend/
├── README.md                    short: what LISA is, quick start, links into docs/
├── CHANGELOG.md                 new: one entry per release (seeded from HYBRID_CLASSIFIER / HANDOFF tables)
├── pyproject.toml               uv workspace root: members services/api, services/triage-ml; shared ruff / pytest settings
├── uv.lock
├── .env.example                 every setting of both services, grouped, placeholders only
├── .gitignore                   fixed: ignore model binaries and data, not the service code
├── .gitattributes               new: LF line endings
├── compose.yaml                 new: local stack (api, triage-ml, tools-mcp, ollama, qdrant, postgres, redis)
├── Makefile                     new: run-api · run-ml · test · lint · build (cross-platform entry points)
│
├── services/
│   ├── api/                                 the LISA backend (deployable 1)
│   │   ├── pyproject.toml                   runtime dependencies only; research libraries in a dependency group
│   │   ├── Dockerfile
│   │   ├── src/
│   │   │   ├── dashboard_backend/           REST API (unchanged package)
│   │   │   ├── incident_graph/              the agent (unchanged package; inner grouping in §5, phase 3)
│   │   │   │   └── playbooks/               rulebook content + loader stay together (non-developers edit the .md files)
│   │   │   └── resolution_engine/           search (unchanged package; shared/ + similar_incidents/ in phase 2)
│   │   ├── reference_data/                  runtime data files the code reads (cmdb_ci_app_clean.json, mock_cmdb_servers.json, …)
│   │   ├── tools/                           maintainer CLIs (replay_playbooks.py)
│   │   └── tests/
│   │       ├── conftest.py
│   │       ├── unit/                        feature tests, one folder per area (assist/, agentic/, questions/, ml_triage/, …)
│   │       ├── regression/                  the fix-batch tests, kept as they are
│   │       └── live/                        tests that need a running service or LLM (markers: llm, live)
│   │
│   └── triage-ml/                           the triage ML service (deployable 2; was models/)
│       ├── pyproject.toml                   from requirements.txt: scikit-learn==1.9.1, xgboost==3.4.1, fastapi, uvicorn
│       ├── Dockerfile
│       ├── app.py · model_a.py · model_b.py · build_model_modules.py · check_service.py
│       ├── README.md · HOWTORUN.md
│       ├── scripts/run_service.bat          kept for Windows development
│       ├── models.lock.json                 new: expected file name + sha256 per model (committed)
│       └── model_files/                     the .joblib files (ignored by git; pulled from the model registry)
│
├── docs/
│   ├── README.md                            index: "to learn X, read Y"
│   ├── architecture/                        how it works (current)
│   ├── operations/                          how to run, configure and host it
│   ├── decisions/                           short ADRs: why it is built this way
│   └── history/                             session hand-offs, plans, the rules migration (kept, read-only)
│
├── research/                                not deployed: notebooks, evaluations, data prep
│   ├── notebooks/
│   └── evaluation/
│
└── archive/                                 retired code, kept for reference, never imported
    └── README.md                            what is here and why
```

**Sibling projects** (Frontend, Chat, tools-mcp, Voice) stay as separate repositories. Optionally,
later, join them as `services/tools-mcp`, `services/voice`, `apps/dashboard` and `apps/chat` in the
same workspace. This plan does not depend on that.

---

## 4. Mapping: every current file and its new home

### 4.1 Root files

| Now | New location | Change |
|---|---|---|
| `README.md` | `docs/architecture/overview.md` (full text) + a new short `README.md` at the root | Content kept in full |
| `Workflow.md` | `docs/architecture/workflow.md` | Move |
| `AGENTIC.md` | `docs/architecture/agent-mode.md` | Move |
| `HYBRID_CLASSIFIER.md` | `docs/architecture/hybrid-classifier.md` | Move |
| `PLAYBOOKS.md` | `docs/architecture/playbooks.md` | Move |
| `TECHSTACK.md` | `docs/architecture/techstack.md` | Move |
| `HANDOFF.md` | `docs/history/HANDOFF.md` | Move |
| `PLAN_reply_understanding.md` | `docs/history/PLAN_reply_understanding.md` | Move |
| `.env.example` | `.env.example` | Merge in the service's settings (`TRIAGE_API_KEY`, `TRIAGE_ML_PORT`, model paths); group by service |
| `.gitignore` | `.gitignore` | Remove the `models/` lines; ignore `services/triage-ml/model_files/*.joblib`, `data/` and `.env` |
| `pyproject.toml` | Root (workspace) + `services/api/pyproject.toml` | Split; unused libraries go to a `research` dependency group (not deleted) |
| `uv.lock` | Root | Regenerated |

### 4.2 `models/` → `services/triage-ml/`

| Now | New location |
|---|---|
| `models/app.py`, `model_a.py`, `model_b.py`, `build_model_modules.py`, `check_service.py` | `services/triage-ml/` (same names) |
| `models/README.md`, `models/HOWTORUN.md` | `services/triage-ml/` (and linked from `docs/operations/`) |
| `models/requirements.txt` | Becomes `services/triage-ml/pyproject.toml` (the file can stay alongside for pip users) |
| `models/run_service.bat` | `services/triage-ml/scripts/run_service.bat` |
| `models/*.joblib` (not in git) | `services/triage-ml/model_files/` locally; the model registry in production; names + sha256 in `models.lock.json` |

### 4.3 `src/` → `services/api/src/` (packages unchanged)

| Now | New location | Change |
|---|---|---|
| `src/dashboard_backend/**` | `services/api/src/dashboard_backend/**` | Move only |
| `src/incident_graph/**` | `services/api/src/incident_graph/**` | Move only (inner grouping optional, §5 phase 3) |
| `src/incident_graph/mock_cmdb_servers.json` | `services/api/reference_data/mock_cmdb_servers.json` | Path from settings (one line in `cmdb_lookup.py`) |
| `src/incident_graph/agents/shared_guidance_OLD.py` | `archive/shared_guidance_OLD.py` | Imported nowhere |
| `src/incident_graph/workflows/servicenow_workflow_teams.py`, `src/dashboard_backend/routers/teams_webhook.py` | **Stay** (tests use them) | Marked "inactive — decision M11" in `docs/architecture/overview.md` |
| `src/incident_graph/playbooks/**` (code, `.md`, `families.yaml`, generated `INDEX.md` / `CODE_RULES.md`) | **Stay together** in the package | Loader default unchanged; `PLAYBOOKS_DIR` still works |
| `src/resolution_engine/**` incl. `README.md` | `services/api/src/resolution_engine/**` | Move only (shared code in phase 2) |

### 4.4 `notebooks/`, `scripts/`, `docs/`, `tests/`

| Now | New location |
|---|---|
| `notebooks/cleaned/cmdb_ci_app_clean.json` | `services/api/reference_data/cmdb_ci_app_clean.json`. **Runtime file**: the path moves to settings (one line in `context_extraction_agent.py`) |
| `notebooks/cleaned/cmdb_ci_app_clean.csv`, `category_ci_map.json`, `category_ci_map_snippet.py` | `research/notebooks/cleaned/` (source / derived data, not read at runtime) |
| `notebooks/clean_cmdb_ci_app.ipynb` | `research/notebooks/` (it produces the reference JSON) |
| `notebooks/L6_IncidentAgent_ByNode.ipynb`, `graph_by_node.py`, `run_by_node_chat_cli.py`, `env_utils.py` | `research/notebooks/` |
| `notebooks/lettucedetect_eval.ipynb`, `lettucedetect_dataset.jsonl`, `lettucedetect_results.jsonl` | `research/evaluation/lettucedetect/` |
| `scripts/replay_playbooks.py` | `services/api/tools/replay_playbooks.py` (a maintainer tool for the playbooks) |
| `scripts/MLZ_Conversation.md`, `scripts/Outlook_Conversation.md` | `docs/history/conversations/` |
| `docs/rules/**` (plans, move maps, inventories, coverage YAML) | `docs/history/rules/**` (unchanged inside) |
| `tests/conftest.py` | `services/api/tests/conftest.py` |
| Feature tests (`test_access_fastpath`, `test_agentic*`, `test_assist_*`, `test_asset_note`, `test_azure_reranking_client`, `test_browser_both`, `test_classify_first`, `test_confirm_retry`, `test_device_split_relevance`, `test_early_ci_saved`, `test_greeting`, `test_important_reask`, `test_inline_steps`, `test_listify_details`, `test_location_contact_questions`, `test_ml_triage`, `test_one_question`, `test_playbooks_v2`, `test_reply_requests`, `test_rerank_pool`, `test_resolution_notice`, `test_staff_base`, `test_tool_catalog`) | `services/api/tests/unit/<area>/` (same file names) |
| `test_three_fixes`, `test_four_fixes`, `test_five_fixes`, `test_seven_fixes`, `test_run_analysis_fixes`, `test_why_replies` | `services/api/tests/regression/` (unchanged) |
| The live-service test inside `test_ml_triage.py` | Stays in the file with a `live` marker; CI runs it against the triage-ml container |
| `.checkpoints/**` revert scripts (not in the zip) | `archive/checkpoints/` if they exist; from now on a git tag per release replaces them |

### 4.5 New `docs/` content (written once from existing text — nothing invented)

| File | Built from |
|---|---|
| `docs/README.md` | Index of all docs below |
| `docs/architecture/blueprint.md` | `LISA-Backend-hybrid-blueprint.md` |
| `docs/operations/hosting.md` | `LISA-Backend-hybrid-hosting-techstack.md` |
| `docs/operations/configuration.md` | The `.env` tables in `README.md` + `HYBRID_CLASSIFIER.md` + the service README (one table, every setting, its default, which service reads it) |
| `docs/operations/runbook.md` | The run sections of `README.md` + `models/HOWTORUN.md` (start order: triage-ml → api → dashboard) |
| `docs/decisions/0001-playbooks-hold-the-rules.md` | `PLAYBOOKS.md` + `docs/rules/PLAN_*` |
| `docs/decisions/0002-triage-ml-as-a-service.md` | The 14 Oct note in `HYBRID_CLASSIFIER.md` |
| `docs/decisions/0003-azure-llm-reranker.md` | `azure_reranking_client.py` docstring + `.env.example` note |
| `docs/decisions/0004-classify-first.md` | `classify_first.py` docstring + `PLAYBOOKS.md` §1 |

---

## 5. Phases

### Phase 0 — Safety net (no code changes)

1. Commit the project to git as it is, **including `models/` code** (fix `.gitignore` first: M14).
   Tag it `poc-hybrid-final`.
2. Add `.gitattributes` (LF), then one normalisation commit.
3. CI pipeline: `uv sync --frozen`, `ruff`, `pytest` (893 offline tests), and a build of both images.
4. Record the sha256 of every artifact: the BM25 `.joblib` files, Qdrant snapshots and both model
   files.

**Exit:** CI green on the untouched code.

### Phase 1 — Move (folders only; no logic changes)

1. `git mv` everything per section 4. Use `git mv` so history follows the files.
2. Only three tiny code edits are needed:
   - the reference-data path in `context_extraction_agent.py`
   - the reference-data path in `cmdb_lookup.py`
   - `triage-ml` reading model files from `model_files/`
   Better still, route all three through settings with the same defaults.
3. Workspace `pyproject.toml`; `services/triage-ml/pyproject.toml` replaces the dependency on
   `C:/PROD/0_Servicenow_TriagingModel_LogisticRegression`.
4. A Dockerfile per service, `compose.yaml`, `Makefile`. Keep the `.bat` files for Windows.
5. `docs/` tree per 4.1 and 4.5; the new short `README.md`.

**Exit:** tests green; both images build; `compose up` runs the stack.

### Phase 2 — Consolidate (no behaviour change; each item has tests)

| Item | From | To |
|---|---|---|
| **One settings module** | ~55 environment reads in 24 modules + 2 `Settings` classes | `incident_graph/config.py`: one pydantic `Settings` (env names and defaults unchanged); a generated `docs/operations/configuration.md` |
| **One LLM factory** | `workflows/common.py` + `azure_reranking_client.py` | `incident_graph/llm.py`: `chat_model()`, `structured(schema)`, `reranker_model()` |
| **One ServiceNow client** | `servicenow_client.py` + the `requests.patch` in `servicenow_teams_followup.py` | `servicenow_client.py` gains `set_followup_fields()`; the Teams module calls it (gets retries) |
| **Shared search toolkit** | Duplicated clients in `ci_predictor/` and `kb_retrieval/` | `resolution_engine/shared/` (embedding client with an optional query prefix, Qwen client, vector-store base, keyword-store base). `data_models.py` stays per domain (the pickles reference them) |
| **Honest naming** | `resolution_engine/ci_predictor/` | `resolution_engine/similar_incidents/`, plus alias entries in `_artifact_compat.py` so the existing `.joblib` files still load |
| **Model-service health** | Not in the API | `ollama_health.py` → `health.py`, which also checks the triage ML `/health` (M13) |
| **Lazy torch** | `import torch` at module top | Import inside `QwenRerankingClient` (S6), so a `RERANKER=azure` image needs no torch |

**Exit:** tests green; the same answers on a replay of saved incidents (`replay_playbooks.py` + the
ML record).

### Phase 3 — Optional inner grouping of `incident_graph/` (one codemod pull request)

```
incident_graph/
├── state.py · config.py · llm.py
├── workflows/        (unchanged: graph + nodes)
├── agentic/          (unchanged)
├── agents/           (unchanged)
├── playbooks/        (unchanged)
├── integrations/     servicenow_client, servicenow_teams_followup, tools_client, ml_triage, cmdb_lookup, claude_agent_sdk_llm
├── assist/           password_reset, browser_fix, access_request
├── conversation/     question_queue, question_dedupe, greeting, flight_reply, reporter_text_checks, priority_recheck, classify_first, ci_rules
├── notes/            asset_note, work_note_summary, resolution_notice
└── storage/          db, token_tracking
```

- **Do it with a codemod.** The tests patch about 15 module paths (for example
  `"incident_graph.servicenow_client.append_work_note"`), so imports and patch strings change in one
  commit.
- **No compatibility shims** — they would be clutter.
- **Skip this phase** if the team prefers stable import paths; phases 1–2 already give most of the
  benefit.

### Phase 4 — Production changes (from the hosting document)

These are M1–M15 and S1–S9: PostgreSQL, Redis, authentication, TLS, the Qdrant server, providers,
tools-server auth, model registry, alerting, async jobs, retention. Do them one pull request each,
in this order:

1. Security (M3, M4, M8)
2. State (M1, M2)
3. Platform (M5, M13, M14, M15)
4. Providers (M6, M12)
5. The S items

### Phase 5 — Retire flags once decided (kept in history, not deleted)

**Kept, not removed:**
- The experiment flags that are all "on unless `=0`": `ASSIST`, `PASSWORD_RESET`,
  `ONE_QUESTION_*`, `RELEVANCE_CHECK`, `ACCESS_REQUEST_FASTPATH`, `INCIDENT_GREETING`,
  `RETRY_AFTER_NO`, `RESOLUTION_NOTICE`.
- The mode switches: `WORKFLOW_MODE`, `RERANKER`.

**How to retire one:** when a decision is final, record it as an ADR. Then make the flag a fixed
default in `config.py` with a comment pointing to the ADR. The code paths stay until a later
release removes them, with the ADR as the record.

---

## 6. Day-to-day maintenance after the restructure

| Task | Where |
|---|---|
| Change a conversation rule or question | `incident_graph/playbooks/*.md` → `make playbooks-check` → `replay_playbooks.py` |
| Change a threshold or setting | `incident_graph/config.py` (+ `.env.example`, regenerated `configuration.md`) |
| Change a prompt | `agents/shared_guidance.py` or the agent module that owns it |
| Add an API route | `dashboard_backend/routers/` + `schemas.py` |
| Add an external system | `incident_graph/integrations/` (phase 3) or next to `servicenow_client.py` |
| Retrain / replace a triage model | Training project → registry → update `models.lock.json` → redeploy `triage-ml` |
| Refresh KB / similar-incident indexes | The index-refresh job; bump the artifact version |
| Record a design decision | `docs/decisions/NNNN-*.md` |
| Release | `CHANGELOG.md` entry + git tag per service (`api-vX.Y.Z`, `triage-ml-vX.Y.Z`) |

**Conventions:**
- **Ownership:** a `CODEOWNERS` file.
- **Pull requests:** one concern per PR.
- **Docs:** update the docs in the same PR.
- **Tests:** every bug fix adds a test in `unit/<area>/`. The fix-batch files stay frozen in
  `regression/`.

---

## 7. Risks and how the plan handles them

| Risk | Mitigation |
|---|---|
| Pickled artifacts reference module paths (`resolution_engine.ci_predictor.data_models`, `servicenow_*` aliases) | Package names unchanged in phase 1; the rename in phase 2 adds aliases to `_artifact_compat.py`; the artifact load is tested in CI |
| Saved checkpoints reference node names and state keys | Rule 4: never renamed |
| Tests patch module paths | Phases 1–2 keep module paths; phase 3 is one codemod with the tests updated in the same commit |
| The triage ML service gives different answers after moving off the training environment | Pin scikit-learn 1.9.1 / XGBoost 3.4.1; `check_service.py` + the 40-ticket comparison in CI; the cleaning-marker check is already in `app.py` |
| Windows developers lose their scripts | `.bat` / PowerShell kept under `scripts/`; `Makefile` / `compose` added next to them |
| Docs drift again | `docs/README.md` index; generated `configuration.md`; the docs-in-same-PR rule |

# Integrating the memory architecture into LISA — step-by-step guide

This guide puts [`LISA-memory-architecture.md`](LISA-memory-architecture.md) into the LISA code
(hybrid / PROD copy). Each step names the exact function it touches and shows the code.

**Rules for every step:**
- It keeps working **without** the new infrastructure: with `DATABASE_URL` / `REDIS_URL` unset, the
  current SQLite / in-memory behaviour stays, so developers and the 893 tests run unchanged.
- It is one pull request with its own tests.
- It does not change graph node names, interrupt texts or state keys.

| Step | Memory type | What changes | Behaviour change for reporters |
|---|---|---|---|
| 1 | Settings | One place for the new settings | None |
| 2 | Short-term (per ticket) | Checkpoints: SQLite → PostgreSQL | None |
| 3 | Secrets (15 min) | Reset secrets: process memory → Redis, encrypted | None (survives restarts, works with 2+ replicas) |
| 4 | Cache + locks | LLM cache, resume lock, ML back-off → Redis | None |
| 5 | User memory | Remember the confirmed / corrected phone number, with source and date; a "forget me" action | Next ticket: "Is your contact number X still correct?" instead of "What is your number?" |
| 6 | Outcome memory | New `outcomes` table, written when a ticket closes; insights endpoint; retraining export | None |
| 7 | Long-term knowledge | KB version in the index, staleness check, refresh job | Edited or retired articles stop being cited |
| 8 | Retention | Purge job for old checkpoints, traces and profiles | None |
| 9 | Relationships | `CMDB_LOOKUP=live` | Server tickets get the right CI |

---

## What the code does today (checked before writing this)

| Memory | Code | Observation |
|---|---|---|
| Checkpoints | `workflows/common.py:93` `make_checkpointer()` → `SqliteSaver` | Called at import by `servicenow_workflow.py`, `servicenow_workflow_teams.py` and `agentic/workflow.py` |
| Reset secrets | `password_reset.py`: `keep_answer`, `get_answer`, `take_answers`, `keep_session`, `session`, `keep_password`, `clear`, `reveal` | Process dict + `threading.Lock`, 15-minute TTL |
| LLM cache | `workflows/common.py`: `make_tracking_cache(InMemoryCache)` | Per process |
| Resume lock | `routers/followup.py:45/55` `_try_claim_resume` / `_release_resume` | Process set |
| ML back-off | `ml_triage.py` `_down_until` | Per process |
| User memory **write** | `context_extraction_agent.extract_incident_context` → `db.upsert_reporter_profile` (ServiceNow profile phone / location, app named) | Profile values only |
| User memory **read** | Same function fills `known_mobile_phone`, `known_location`, `known_applications`, `most_frequent_ci`, `past_issue_summaries` | **`known_mobile_phone` and `known_location` are loaded but never used.** The prompt shows only apps / CI / past issues, as "personalisation only" (`completeness_agent._render_reporter_history`) |
| Reporter's phone / location answers | `apply_contact_reply` updates **this ticket's** `extracted_context` (`contact_number_status`: confirmed / corrected / provided; `reporter_stated_location`) | **Never saved to user memory: lost when the ticket closes** |
| Phone question | `completeness_agent._build_contact_number_block` | Uses the ServiceNow profile phone ("Is your contact number X still correct?"), else asks for a number |
| Location question | `_build_location_question_block` | Always asks (the profile location is often stale) |
| Ticket close | `core_nodes.resolve_incident`, `auto_escalate_incident`, `human_review` (reject path) | Natural place to save memories and outcomes |

---

## Step 1 — Settings

Add to `.env.example` (and later the single settings module from the restructure plan):

```dotenv
# Memory backends (unset = today's SQLite / in-memory behaviour)
DATABASE_URL=postgresql://lisa:***@db-host:5432/lisa
REDIS_URL=rediss://:***@redis-host:6380/0
SECRET_SLOT_KEY=            # Fernet key (32 url-safe base64 bytes) from the vault, encrypts reset secrets in Redis
MEMORY_FACT_MAX_AGE_DAYS=90 # a reporter-given fact older than this is not offered back
RETENTION_CLOSED_DAYS=90    # checkpoints / traces / full state of closed tickets are purged after this
```

New dependencies for `services/api`: `langgraph-checkpoint-postgres`, `psycopg[binary,pool]`,
`redis`, `cryptography`.

Shared connections live in one small module so every step reuses them:

```python
# incident_graph/infra.py  (new)
import os
from functools import lru_cache

@lru_cache
def pg_pool():
    """One PostgreSQL pool per process, or None when DATABASE_URL is unset (dev: SQLite)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    from psycopg_pool import ConnectionPool
    return ConnectionPool(conninfo=url, max_size=10, kwargs={"autocommit": True, "prepare_threshold": 0})

@lru_cache
def redis_client():
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    import redis
    return redis.Redis.from_url(url)
```

---

## Step 2 — Short-term memory: checkpoints to PostgreSQL

`workflows/common.py`:

```python
from incident_graph.infra import pg_pool

def make_checkpointer():
    pool = pg_pool()
    if pool is not None:                                   # production
        from langgraph.checkpoint.postgres import PostgresSaver
        saver = PostgresSaver(pool)
        saver.setup()                                      # creates the checkpoint tables once
        return saver
    os.makedirs(os.path.dirname(CHECKPOINT_DB_PATH), exist_ok=True)   # dev: unchanged
    conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
    memory = SqliteSaver(conn)
    memory.setup()
    return memory
```

**Cut-over:** paused tickets live in `checkpoints.sqlite`. Either let them drain (close or answer
them before switching), or keep a read-only SQLite saver for old thread IDs during a transition
week.

**Test:** run one ticket to a pause (`awaiting_followup_answer`), restart the API, answer it, and
confirm it resumes.

---

## Step 3 — Secret memory: reset secrets to Redis (encrypted, TTL)

Keep **the same functions** in `password_reset.py`, so `assist_nodes.py` does not change. Only the
storage behind `_slot` changes:

```python
import json
from incident_graph.infra import redis_client

_R = redis_client()
_F = None
if _R is not None:
    from cryptography.fernet import Fernet
    _F = Fernet(os.environ["SECRET_SLOT_KEY"])

def _key(thread_id: str) -> str:
    return f"lisa:reset:{thread_id}"

def _load(thread_id: str) -> dict:
    raw = _R.get(_key(thread_id))
    return json.loads(_F.decrypt(raw)) if raw else {"answers": {}, "password": None, "session": None}

def _store(thread_id: str, slot: dict) -> None:
    _R.set(_key(thread_id), _F.encrypt(json.dumps(slot).encode()), ex=TTL_SECONDS)   # TTL restarts on each write, as today

def keep_answer(thread_id: str, field: str, answer: str) -> None:
    if _R is None:                       # dev / tests: today's in-memory code path, unchanged
        with _lock:
            _slot(thread_id)["answers"][field] = answer
        return
    slot = _load(thread_id)
    slot["answers"][field] = answer
    _store(thread_id, slot)

# ...the same pattern for get_answer, take_answers, keep_session, session, keep_password, reveal;
# clear() becomes _R.delete(_key(thread_id)).
```

- **Concurrency:** read-modify-write is safe because one ticket is resumed by one request at a time
  (the resume lock, step 4).
- **What does not change:** secrets still never go into state, checkpoints, work notes or logs.

**Tests:**
- the existing `tests/test_assist_*.py` keep running on the in-memory path;
- one new test runs against a test Redis and checks: TTL set, value encrypted at rest, gone after
  `clear()`.

---

## Step 4 — Cache and locks to Redis

**LLM cache** (`workflows/common.py`):

```python
elif LLM_CACHE_BACKEND == "redis":
    from langchain_community.cache import RedisCache
    from incident_graph.infra import redis_client
    set_llm_cache(make_tracking_cache(RedisCache)(redis_=redis_client(), ttl=3600))
```

The cache-hit tracking mixin keeps working, because it wraps `lookup()`.

**Resume lock** (`routers/followup.py`):

```python
from incident_graph.infra import redis_client
_R = redis_client()

def _try_claim_resume(thread_id: str) -> bool:
    if _R is None:
        ...                                                   # today's in-process set
    return bool(_R.set(f"lisa:resume:{thread_id}", "1", nx=True, ex=600))   # expires if a worker dies mid-turn

def _release_resume(thread_id: str) -> None:
    if _R is None:
        ...
    _R.delete(f"lisa:resume:{thread_id}")
```

Use the same lock in `servicenow_queue.continue_followup` and `voice_intake.continue_voice_incident`.
Today only the Follow-up tab is protected.

**ML back-off** (`ml_triage.py`): `_back_off()` sets `lisa:ml_down` with `ex=BACKOFF_SECONDS`;
`classify()` checks `_R.exists("lisa:ml_down")`. One failing replica then spares the others the
timeout.

---

## Step 5 — User memory: remember what the reporter confirmed, with source and date

### 5.1 Table (`db.py`, PostgreSQL; the same SQL works on SQLite for dev)

```sql
CREATE TABLE IF NOT EXISTS reporter_facts (
    reporter_key TEXT NOT NULL,          -- staff number, else email (same key as reporter_profiles)
    fact         TEXT NOT NULL,          -- 'mobile_phone' (more facts later, one row each)
    value        TEXT NOT NULL,
    source       TEXT NOT NULL,          -- 'servicenow_profile' | 'reporter_confirmed' | 'reporter_corrected' | 'reporter_provided'
    observed_at  REAL NOT NULL,          -- epoch, like the other tables
    incident_id  TEXT,
    PRIMARY KEY (reporter_key, fact, value)
);
```

```python
def remember_fact(reporter_key, fact, value, source, incident_id=None) -> None:
    now = time.time()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO reporter_facts (reporter_key, fact, value, source, observed_at, incident_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (reporter_key, fact, value) DO UPDATE SET source = excluded.source, "
            "observed_at = excluded.observed_at, incident_id = excluded.incident_id",
            (reporter_key, fact, value, source, now, incident_id),
        )

def recall_fact(reporter_key, fact, max_age_days) -> dict | None:
    """The newest value the REPORTER gave (not the profile) within max_age_days, with its source and date."""
    cutoff = time.time() - max_age_days * 86400
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT value, source, observed_at FROM reporter_facts WHERE reporter_key = ? AND fact = ? "
            "AND source LIKE 'reporter_%' AND observed_at >= ? ORDER BY observed_at DESC LIMIT 1",
            (reporter_key, fact, cutoff),
        ).fetchone()
        return dict(row) if row else None
```

(`ON CONFLICT` is valid in both SQLite and PostgreSQL.)

### 5.2 Write: when the ticket closes

New helper, called at the top of `resolve_incident`, `auto_escalate_incident` and the reject path of
`human_review`. A node can re-run on resume, so it is idempotent (upsert):

```python
# incident_graph/memory.py  (new)
from incident_graph import db
from incident_graph.agents.context_extraction_agent import _reporter_key

_PHONE_SOURCE = {"confirmed by the reporter": "reporter_confirmed",
                 "corrected by the reporter": "reporter_corrected",
                 "provided by the reporter": "reporter_provided"}

def remember_from_ticket(state: dict) -> None:
    """Save what the reporter confirmed in this ticket, with where it came from. Never raises."""
    try:
        ctx = state.get("extracted_context") or {}
        key = _reporter_key(state, ctx.get("reporter_staff_no"))
        source = _PHONE_SOURCE.get(ctx.get("contact_number_status") or "")
        if key and source and ctx.get("reporter_mobile_phone"):
            db.remember_fact(key, "mobile_phone", ctx["reporter_mobile_phone"], source, state.get("incident_id"))
    except Exception as exc:  # noqa: BLE001 - memory must never stop a ticket from closing
        print(f"[WARN] {state.get('incident_id')} - could not save reporter memory: {exc}")
```

**Location is not remembered on purpose.** Staff move between gates and buildings daily, and the
code already always asks for it. A stored location would only invite wrong assumptions.

### 5.3 Read: offer the remembered number, still ask to confirm

- **`extract_incident_context`:** replace the unused `known_mobile_phone` with
  `db.recall_fact(key, "mobile_phone", MEMORY_FACT_MAX_AGE_DAYS)`. It keeps value + source + date.
- **`completeness_agent._build_contact_number_block`:**

```python
def _build_contact_number_block(extracted_context: dict | None) -> str:
    context = extracted_context or {}
    phone = context.get("reporter_mobile_phone") or (context.get("known_mobile_phone") or {}).get("value")
    if phone:
        return f"\n\nIs your contact number {phone} still correct? If not, please reply with the correct mobile number, for example 055 123 4567."
    return "\n\nWhat is the best mobile number to reach you on? For example, 055 123 4567."
```

The ServiceNow profile number still wins. The remembered number is used only when ServiceNow has
none, and it is **always confirmed**, never assumed. That is the self-correcting rule.

### 5.4 Forget a reporter

`DELETE /api/reporters/{reporter_key}` (admin role only, once API auth exists) removes their rows
from `reporter_profiles`, `reporter_incident_history` and `reporter_facts`, and writes an audit
line.

**Tests:**
- a ticket where the reporter corrects the number → a `reporter_facts` row with
  `reporter_corrected`
- the next ticket with no ServiceNow phone → the question offers that number
- a fact older than the limit is not offered
- the delete endpoint removes all three

---

## Step 6 — Outcome memory: learn from every closed ticket

### 6.1 Table (no free text, no personal data)

```sql
CREATE TABLE IF NOT EXISTS outcomes (
    thread_id          TEXT PRIMARY KEY,
    incident_id        TEXT,
    channel            TEXT,     -- servicenow | chat | voice
    closed_at          REAL,
    final_status       TEXT,     -- resolved | rejected | security_escalated
    ml_status          TEXT,     -- ok | partial | error
    ml_ci              TEXT,  ml_ci_conf REAL,
    final_ci           TEXT,  ci_source TEXT,      -- hybrid_model | override | server_cmdb | kb_confirmed | low_confidence_unknown | model_error
    ml_priority        TEXT,  final_priority TEXT, policy_reason TEXT,
    major_incident_risk_alert BOOLEAN,
    kb_top             TEXT,  kb_top_relevance REAL,
    reporter_confirmed BOOLEAN,  -- TRUE fixed, FALSE not fixed, NULL not asked
    escalation_reason  TEXT,     -- a short code, not the free-text reason
    assist_tasks       TEXT,     -- e.g. 'password_reset,browser_clear'
    workflow_mode      TEXT,  reranker TEXT, llm_model TEXT, ml_model_files TEXT,
    total_tokens       INTEGER
);
```

### 6.2 Write: same three closing nodes, one helper

```python
# incident_graph/memory.py
from dashboard_backend.kb_format import kb_matches   # move it to incident_graph in the restructure

def record_outcome(state: dict, thread_id: str, final_status: str) -> None:
    try:
        ml = state.get("ml_classification") or {}
        preds = ml.get("predictions") or {}
        cls = state.get("classification") or {}
        kb = kb_matches(state.get("search_results") or [])
        db.save_outcome(thread_id, {
            "incident_id": state.get("incident_id"), "channel": state.get("source"), "final_status": final_status,
            "ml_status": ml.get("status"),
            "ml_ci": (preds.get("cmdb_ci") or {}).get("value"), "ml_ci_conf": (preds.get("cmdb_ci") or {}).get("confidence"),
            "final_ci": cls.get("ci_component"), "ci_source": cls.get("ci_decision_source"),
            "ml_priority": (preds.get("priority") or {}).get("mapped"), "final_priority": cls.get("priority"),
            "policy_reason": cls.get("priority_policy_reason"),
            "major_incident_risk_alert": ml.get("major_incident_risk_alert"),
            "kb_top": kb[0]["kb_number"] if kb else None, "kb_top_relevance": kb[0]["relevance_percentage"] if kb else None,
            "reporter_confirmed": state.get("user_confirmed_resolved"),
            "assist_tasks": ",".join(state.get("assist_done") or []),
            # workflow_mode, reranker, llm_model, ml_model_files, total_tokens: from settings / db.token_usage
        })                                               # INSERT ... ON CONFLICT (thread_id) DO UPDATE: safe on node re-runs
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] {state.get('incident_id')} - could not record outcome: {exc}")
```

| Called in | `final_status` |
|---|---|
| `resolve_incident` | `"resolved"` |
| `auto_escalate_incident` | `"rejected"` |
| `human_review` reject path | `"rejected"` |
| `escalate_to_security` | `"security_escalated"` |

### 6.3 Use it

| Use | How |
|---|---|
| **Dashboard insights** | `GET /api/insights/outcomes`: automation rate, CI agreement (`ml_ci = final_ci`), KB fix rate per article, ML error rate, cost per ticket |
| **KB effectiveness** | `SELECT kb_top, AVG(reporter_confirmed::int), COUNT(*) FROM outcomes WHERE reporter_confirmed IS NOT NULL GROUP BY kb_top`. Articles below a fix rate go to the KB owners |
| **Retraining** (triage ML service) | Export `incident_id, ml_ci, final_ci, ci_source, ml_priority, final_priority` monthly to the training project. Join with the ServiceNow closure values (the true labels) for error analysis and retraining |
| **ML health alert** | Alert when `ml_status = 'error'` exceeds a share per hour (hosting doc M15) |
| **Mode comparison** | Compare `workflow_mode` agent vs graph (turns, tokens, `reporter_confirmed`) before choosing one |

---

## Step 7 — Long-term knowledge: keep the KB fresh and flag stale articles

The index build lives outside this repo (the KB retrieval training project). The integration has
three parts:

1. **At index time,** add `sys_updated_on` and `workflow_state` from ServiceNow `kb_knowledge` to
   each Qdrant point's payload and to the BM25 `KnowledgeBaseArticle`.
2. **A refresh job** (nightly; a Container Apps Job, ECS scheduled task or cron):
   - compare ServiceNow `kb_knowledge` (`sys_updated_on`, `workflow_state`) with the payloads
   - re-embed changed articles (Qdrant upsert by the same deterministic point ID)
   - for retired articles, `set_payload({"stale": True})`
   - rebuild the BM25 `.joblib` and publish it as a new artifact version
3. **At query time** (`kb_retrieval_service.rerank`, or a Qdrant filter in `VectorSearchStore.search`),
   skip points with `stale = true`. The trace says "skipped N stale articles".

This is the "self-correcting memory" pattern applied where it matters most for LISA's answers: an
article that changed is not cited until it has been re-verified (re-indexed).

---

## Step 8 — Retention

A nightly job (same scheduler as step 7):

```python
def purge_closed(days: int) -> None:
    saver = make_checkpointer()
    for row in db.closed_before(days):                       # terminal status and resolved_at older than N days
        saver.delete_thread(row["thread_id"])                # LangGraph checkpoint savers support delete_thread
        db.delete_incident_details(row["thread_id"])         # incident_json, trace_steps, similar / KB match rows
    db.purge_reporter_history(older_than_days=365)           # policy value
```

- **Kept:** `incidents` (the registry row), `token_usage` and `outcomes`, which have no free text.
- **Removed:** the full state, traces and checkpoints, which contain the conversation.

---

## Step 9 — Relationship memory: CMDB live

Set `CMDB_LOOKUP=live` (`cmdb_lookup._live` already queries `cmdb_ci_server`). Next, extend it to
`cmdb_rel_ci` (server → application) so a server named in a ticket can give its application CI. No
new store is needed.

---

## Where the memory layer sits in the graph

```mermaid
flowchart TD
  F[fetch / receive message] --> X[extract_incident_context<br/>READ user memory · recall phone]
  X --> T[triage] --> AC[assess_completeness<br/>offer remembered phone to confirm]
  AC --> CL[classify] --> KB[search_kb<br/>skip stale KB articles]
  KB --> W[write_resolution] --> CR[confirm_resolution]
  CR --> RES[resolve_incident<br/>WRITE user memory + outcome]
  CR --> ESC[auto_escalate_incident<br/>WRITE user memory + outcome]
  CR --> HR[human_review<br/>reject → WRITE user memory + outcome]
  T --> SEC[escalate_to_security<br/>WRITE outcome]
  subgraph Always
    CK[(Checkpoints · PostgreSQL)]
    RS[(Secrets / cache / locks · Redis)]
  end
```

**Agent mode** uses the same closing nodes (`resolve_incident`, `auto_escalate_incident`,
`human_review`) and the same `extract_incident_context`, so steps 5–6 cover it with no extra code.

---

## Rollout order

| Order | Steps | Why this order |
|---|---|---|
| 1 | 1, 2, 3, 4 | Infrastructure first. No behaviour change, and it unlocks running 2+ replicas safely |
| 2 | 6 | Start collecting outcomes early; they are needed to judge every later change |
| 3 | 5 | A small reporter-facing improvement, measurable with outcomes (fewer "what is your number" turns) |
| 4 | 8 | Retention before real users' data piles up |
| 5 | 7, 9 | Depend on ServiceNow / index-build work outside the API |

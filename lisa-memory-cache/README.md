# LISA — Memory and cache on PostgreSQL + Redis Stack + Qdrant (fresh plan)

A clean start, based only on the three services you run in Docker and on LangGraph. It does **not** use earlier scripts or earlier memory / cache plans.
- **Plan:** this README.
- **Building blocks:** `lisa_memory_cache.py`, one module that uses LangGraph's own components wherever they exist.
- **Proof:** `test_lisa_memory_cache.py`, 13 tests, all passing against real PostgreSQL 16, Redis Stack 7.4 and Qdrant 1.15.

## 1. The one-line answer

LangGraph has three slots, `builder.compile(checkpointer=..., store=..., cache=...)`, and you have one service for each job:

| Service | Holds | Why this service |
|---|---|---|
| **PostgreSQL** | **Short-term memory** (checkpointer) and **long-term memory** (store), plus your own tables (outcomes, audit) | Durable, transactional, backed up, queryable with SQL. Nothing here may be lost or evicted |
| **Redis Stack** | **Caches** (LangGraph node cache, LLM cache, API / embedding caches), **locks**, **short-lived secrets** | Fast, with built-in expiry (TTL). Everything here can be rebuilt or is meant to expire |
| **Qdrant** | **Knowledge** (KB articles, past incidents) and **episodic memory** (confirmed fixes of past tickets) | Vector similarity with payload filters |

```python
from lisa_memory_cache import compile_graph      # = builder.compile(checkpointer=PostgresSaver, store=PostgresStore, cache=Redis node cache)
app = compile_graph(builder)
```

---

## 2. Memory — what LISA remembers

| # | Memory | Where | LangGraph piece | Rules |
|---|---|---|---|---|
| M1 | **Short-term**: the whole state of one ticket (answers, interrupts, history) | PostgreSQL | `PostgresSaver` (`make_checkpointer`) | One `thread_id` per ticket. A paused ticket resumes from any worker or after a restart (tested). History serves a trace view. `delete_thread` for retention (tested) |
| M2 | **Long-term, per reporter**: facts such as the confirmed mobile number | PostgreSQL | `PostgresStore` (`make_store`) + `ReporterMemory` | Only facts the **reporter gave** or a **system of record** holds; never LLM-inferred (enforced). Stored with source and date. **Offer, don't assume:** "Is 055 123 4567 still your number?". Too old = not offered (`max_age_days`). `forget()` for privacy requests. Optional auto-expiry (`fact_ttl_days`, tested) |
| M3 | **Episodic**: what fixed similar tickets | Qdrant | `EpisodicMemory` (Qdrant client) | Written **only** when the reporter confirmed the fix. One point per ticket (idempotent). Search **filtered by CI / category**, so a laptop fix is not offered for a phone (tested). No personal data in the text |
| M4 | **Knowledge**: KB articles, historical incidents | Qdrant | your retrieval code | Store each article's source version in the payload; skip stale ones; re-index on change |
| M5 | **Outcomes**: predicted vs final, KB used, fixed or not | PostgreSQL (your own table) | — | For KB effectiveness, retraining and dashboards. No free text, no personal data |
| M6 | **Procedural**: prompts, playbooks, rules | Git | — | Versioned with the code, not in a database |

**Inside a node** the store arrives through `Runtime` (tested):

```python
from langgraph.runtime import Runtime
from lisa_memory_cache import ReporterMemory

def ask_contact(state, runtime: Runtime):
    fact = ReporterMemory(runtime.store).recall(state["reporter_email"], "mobile_phone", max_age_days=90)
    question = (f"Is {fact['value']} still the best number to reach you?" if fact
                else "What is the best mobile number to reach you on?")
    ...
# when the reporter answers:  ReporterMemory(runtime.store).remember(email, "mobile_phone", number, "reporter_confirmed", ticket_id)
```

**Two gotchas, both handled in the module:**
- **No dots in namespaces:** store namespace labels cannot contain `.`, so a raw email can't be a namespace (tested). `ReporterMemory` hashes the reporter id.
- **Connection pool settings:** a hand-made connection pool must use `autocommit=True`, `row_factory=dict_row` and `prepare_threshold=0` for LangGraph's PostgreSQL classes.

**What not to do** (it looks easy in the LangGraph docs, but is wrong for tickets):
- **Don't let the LLM write free-text "memories"** ("User name is Bob") and pull them into prompts by similarity. They can be wrong or stale and are hard to audit or delete, and you'd be storing personal data that nobody checked.
- **Don't trim or summarize message history from the start.** It changes the prompt prefix and defeats provider prompt caching (C1). Shorten bulky tool results instead.

---

## 3. Cache — what LISA avoids recomputing

| # | Cache | Where | Piece | Use for | Never for |
|---|---|---|---|---|---|
| C1 | **Provider prompt cache** | Azure OpenAI / Anthropic side | — (prompt layout) | Every big prompt: put **static** text first (instructions, playbooks), the **ticket** last. Azure caches a repeated prefix automatically (prompts over ~1,024 tokens); measure `usage_metadata["input_token_details"]["cache_read"]` | — |
| C2 | **LangGraph node cache** | Redis | `make_node_cache()` + `CachePolicy(ttl=…)` per node | Pure, expensive nodes: retrieval, rerank, an ML prediction. Keyed on the node's input (tested: a second ticket with the same input skips the node) | Nodes that write, call ServiceNow, ask the user or interrupt |
| C3 | **Exact LLM cache** | Redis | `make_llm_cache()` passed as `cache=` **per model** | Deterministic calls (temperature 0, structured output): classification, extraction | Conversational / creative calls. Avoid the global `set_llm_cache()` (tested: per-model leaves other models uncached) |
| C4 | **API read cache** | Redis | `@ttl_cache(ns, seconds, keep=...)` + `bump(ns)` | ServiceNow / CMDB reads, ML-service results. `keep` drops error results; `bump` after LISA writes | The ticket record at intake, any write |
| C5 | **Embedding cache** | Redis | `cached_embedder(embed, model)` | Same text + same model → one embedding call (float32 bytes) | — |
| C6 | **Semantic cache** | Redis Stack (RedisVL, db 0) | — | **Only** generic, PII-free FAQ answers, scoped by topic + version, with a measured threshold (see the semantic-cache notebook) | **Ticket answers**: similar wording ≠ same answer (other device, CI, reporter) |

**Coordination in the same Redis:**
- **`lock(f"ticket:{id}")`:** one worker per ticket. Only the owner releases it, and it expires if the worker dies (tested).
- **`Secrets`:** identity-check answers and one-time tokens, Fernet-encrypted with a TTL (tested). It refuses to run without a key.

**Safe by design:**
- **Prefixes:** every key starts with `lisa:` (`lisa:node:`, `lisa:llm:`, `lisa:cache:`, `lisa:emb:`, `lisa:lock:`, `lisa:secret:`).
- **No ticket text in keys:** inputs are hashed.
- **Clearing:** changing `LISA_CACHE_VERSION` drops every cache at once.
- **Redis down:** the caches fall back to computing the value (tested).

---

## 4. Your three containers — settings that matter

| Service | Setting | Why |
|---|---|---|
| **PostgreSQL** | A **volume**, and regular **backups** (`pg_dump`) | It holds conversations and memory |
| | Run the LangGraph table setup **once** (`LISA_DB_SETUP=1` on one start, `0` on the rest with several workers) | `setup()` creates / migrates the checkpoint and store tables |
| **Redis Stack** | **db 0**: search indexes (RedisVL semantic cache) only work there. With the `lisa:` prefixes, everything can share db 0 | Simple |
| | Default persistence (snapshots) is enough; **no AOF needed** | Redis holds only caches, locks and expiring secrets. Conversations are in PostgreSQL |
| | One instance on the PC (`noeviction`, no memory limit) is fine | In production use **two instances**: cache with `allkeys-lru`, secrets / locks with `noeviction`. Eviction is set per instance, not per database |
| **Qdrant** | A **volume**; **payload indexes** on the fields you filter (`ci`, `category`; created by `EpisodicMemory`) | Filtered search stays fast |
| | Snapshots for backup | Re-indexing takes long |

`docker-compose.yml` in this folder has the three services with these settings (a reference; you already run them).

---

## 5. Retention and privacy

| Data | How long | How |
|---|---|---|
| Checkpoints (M1) | Closed tickets: e.g. 90 days | A nightly job: `make_checkpointer().delete_thread(thread_id)` for tickets closed before the cut-off |
| Reporter facts (M2) | Offered for 90 days; optional auto-delete after N days | `max_age_days`; `make_store(fact_ttl_days=...)`; `ReporterMemory.forget()` on request |
| Episodic (M3) | Until the fix is no longer valid | `EpisodicMemory.forget_ticket()`; change the collection when the CI catalogue changes |
| Redis | Minutes to days | TTL on every key |

---

## 6. Use it

```powershell
# packages for the run (no change to pyproject.toml)
uv run --with langgraph-checkpoint-postgres --with "psycopg[binary,pool]" --with langchain-redis --with "langchain-core==1.5.3" --with qdrant-client --with cryptography ...

# settings (.env)
LISA_POSTGRES_URL=postgresql://<user>:<password>@localhost:5432/<db>
LISA_REDIS_URL=redis://localhost:6379/0
LISA_QDRANT_URL=http://localhost:6333
LISA_SECRET_KEY=<Fernet key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">

# prove it on your PC (13 tests)
python -m pytest -q test_lisa_memory_cache.py
```

**Tested with:**
- langgraph 1.2.10, langgraph-checkpoint-postgres 3.2.0, langchain-core 1.5.3;
- langchain-redis 0.2.5 (0.2.6 would upgrade langchain-core), qdrant-client 1.19.0, psycopg 3.3.6;
- PostgreSQL 16, Redis Stack 7.4, Qdrant 1.15.4.

## 7. Wiring it into your current scripts

This kit is independent of LISA's code. To plug it in, send the current project. Then each item below can be mapped to the exact node:
- which nodes get a `CachePolicy`;
- where `remember` / `recall` go;
- where `EpisodicMemory.record` runs at ticket close;
- which external reads get `@ttl_cache`;
- which prompts to reorder for C1.

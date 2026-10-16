# LISA — Would `langgraph-redis` / `langchain-redis` make it easier?

**Sources reviewed:**
- LangChain's Redis integrations page (https://docs.langchain.com/oss/python/integrations/providers/redis), read from its source in `langchain-ai/docs`;
- the `redis-developer/langgraph-redis` README (package `langgraph-checkpoint-redis`).

**Also covers** LangGraph's memory guide (`oss/langgraph/add-memory`, pasted in the conversation): §5.

**Checked against** the LISA hybrid PROD copy with the memory & cache update pack, and **tested** against:
- Redis Stack 7.4 (the same image family as your Docker container);
- PostgreSQL 16.

## Short answer

**Partly.** Two pieces are a real simplification for LISA; the rest does not fit.

| Package / feature | What it is | For LISA | Why |
|---|---|---|---|
| **`langgraph-checkpoint-redis` → `RedisSaver`** | LangGraph checkpointer in Redis | **Yes, as an option.** Easiest way to share ticket state across processes on your PC | Tested: LISA's real agent-mode graph passes the same 93 tests as with `MemorySaver`; a paused ticket resumes in another process; `delete_thread` works. **Needs db 0 and AOF** (below) |
| **`langchain-redis` → `RedisCache`** | Exact LLM response cache | **Yes, replace the current one** | It is maintained (`langchain_community` is being retired). It has a key `prefix`, and its `clear()` deletes only its own keys. The current one writes unprefixed keys and `clear()` empties the whole database |
| `langgraph-checkpoint-redis` → `RedisStore` | Cross-thread key-value memory (+ vectors) | No | LISA does not use a LangGraph Store. User memory and outcomes need SQL (dashboard, insights, "forget this reporter") and must be durable; they are already built |
| `langgraph.middleware.redis` (semantic cache, tool cache, conversation memory, semantic router) | Middleware for LangChain's `create_agent` | No | LISA's agent mode is its own `StateGraph`, not `create_agent`. The middleware is async-first, while LISA invokes synchronously. Semantic caching of answers is unsafe for tickets (caching plan §4) |
| `langchain-redis` → `RedisSemanticCache` | Semantic LLM cache | No | Same safety reasons. For FAQ experiments, use the RedisVL notebook |
| `langchain-redis` → `RedisVectorStore` | Vector store / retriever | No | KB and past incidents are in Qdrant + BM25 + reranker, already tuned. A second vector store means two indexes to keep in sync |

**What stays custom, and why:**
- **`incident_graph/cache.py`:** it caches embeddings, reranker scores, ML results, ServiceNow and CMDB reads. None of these are LLM calls, so neither package covers them.
- **The encrypted reset secrets** (`password_reset.py`).
- **The resume lock:** none of these packages provide one.

---

## 1. `RedisSaver` as LISA's checkpointer

### What it would replace

`workflows/common.py:make_checkpointer()` returns `SqliteSaver` (PC) or `PostgresSaver` (`DATABASE_URL`). With `RedisSaver`:
- **every backend process** shares the ticket state (paused tickets, interrupts, history) with no PostgreSQL;
- a ticket paused in one process can be answered through another.

### What was tested

| Test | Result |
|---|---|
| Package compatibility with LISA's versions (langgraph 1.2.10, langgraph-checkpoint 4.1.1, langchain-core 1.5.3) | `langgraph-checkpoint-redis 0.5.2` + `langchain-redis 0.2.5` install **without changing** LISA's versions (pin `langchain-core==1.5.3`; the newest `langchain-redis` 0.2.6 would upgrade it to 1.6.x) |
| LISA's full test suite with `RedisSaver` | Same as SQLite: 915 passed, 2 skipped, 1 known failure (the one Azure-dependent test). **Caveat:** most tests call nodes directly or use `MemorySaver`, so this alone proves little |
| **LISA's real agent-mode graph** (`test_agentic*.py`: interrupts, `Command(resume=…)`, `get_state`) with `RedisSaver` instead of `MemorySaver` | **93 passed, the same 1 known failure — identical to `MemorySaver`** |
| Pause in process A, resume in process B | Works: B finds the interrupt, resumes, and reads the history (`get_state_history`) |
| `delete_thread` (used by `memory purge`) | Works |
| **Database other than 0** | **Fails:** `Cannot create index on db != 0`. Trap: if db 0 already has the index, `setup()` on db 1 *appears* to succeed, but its searches then read db 0's index. **Always use db 0 for checkpoints** |
| Size | About 2 KB per key; one test conversation ≈ 120 KB with full history. Real tickets with KB text will be larger; measure with `INFO memory` |

### What you must get right

| Point | Why | Setting |
|---|---|---|
| **db 0** | Checkpoints need search indexes (RedisJSON + RediSearch), which work only in db 0 | `redis://localhost:6379/0`. LISA's caches stay in db 1; the notebook's keys in db 0 have other prefixes |
| **Write to disk on every change (AOF)** | Your container's default saves a snapshot only every few minutes (`appendonly no`). A crash could lose the last turns of open tickets. SQLite and PostgreSQL don't have this problem | Re-create the container with `-e REDIS_ARGS="--appendonly yes --appendfsync everysec"` (tested: the image applies it) and a volume |
| **Keep the full saver** | The Trace tab reads checkpoint history (`routers/trace.py:203`), so `ShallowRedisSaver` ("latest only") would break it | `RedisSaver` |
| **TTL with care** | `ttl={"default_ttl": minutes, "refresh_on_read": True}` deletes threads after inactivity. A ticket paused longer than the TTL would lose its conversation | No TTL, and keep using `memory purge` for closed tickets; or a TTL much longer than any ticket waits (e.g. 30 days) |
| **No migration** | Paused tickets in `data/checkpoints.sqlite` are not moved | Finish them before switching |
| **RAM** | Everything is in memory | Fine on a PC; size it in production |

### Your container, re-created for checkpoints (one time)

```powershell
docker rm -f redis-stack
docker run -d --name redis-stack --restart unless-stopped -p 6379:6379 -p 8001:8001 -v lisa-redis:/data -e REDIS_ARGS="--appendonly yes --appendfsync everysec" redis/redis-stack:latest
docker exec redis-stack redis-cli CONFIG GET appendonly        # appendonly yes
```

### The code change (small, opt-in)

```python
# workflows/common.py — make_checkpointer(), before the PostgreSQL branch
if os.environ.get("CHECKPOINTER") == "redis":                    # CHECKPOINT_REDIS_URL must be db 0
    from langgraph.checkpoint.redis import RedisSaver
    saver = RedisSaver(redis_url=os.environ.get("CHECKPOINT_REDIS_URL", "redis://localhost:6379/0"))
    saver.setup()                                                 # creates the indexes once
    return saver
```

Run with:

```powershell
uv run --with redis --with langgraph-checkpoint-redis --with "langchain-core==1.5.3" uvicorn dashboard_backend.main:app --port 8000
```

### PC or production?

| | Checkpoints in | Why |
|---|---|---|
| **Your PC, one backend process** | SQLite (as now) | Nothing to gain; the file is durable |
| **Your PC, trying 2+ processes** | **`RedisSaver`** on your Redis Stack (db 0, AOF on) | Easiest: no PostgreSQL to install |
| **Production** | **PostgreSQL** (`PostgresSaver`, already built; **tested the same way**: LISA's 93 agent-mode tests, resume in another process, history, `delete_thread`) | This is also what LangGraph's memory guide recommends for production. Production needs PostgreSQL anyway for the dashboard, user-memory and outcome tables, so checkpoints there add no new service. It has backups, point-in-time recovery and SQL for audit. A Redis checkpointer in production needs Redis with RedisJSON + RediSearch *and* persistence: on Azure Managed Redis the modules must be chosen at creation; AWS compatibility is not verified |

---

## 2. LLM cache: switch to `langchain_redis.RedisCache`

| | `langchain_community` `RedisCache` (now) | `langchain_redis` `RedisCache` |
|---|---|---|
| Maintained | Being retired (deprecation warning) | Yes |
| Key prefix | None (bare hashes) | `prefix=` (e.g. `lisa:llm`) |
| `clear()` | `FLUSHDB`: empties the **whole database** | Deletes only keys with its prefix (tested) |
| Works with LISA's cache-hit tracking (`make_tracking_cache`) | Yes | **Yes** (tested: `lookup` / `update` / `clear`) |

The change, in `workflows/common.py`:

```python
elif LLM_CACHE_BACKEND == "redis":
    from langchain_redis import RedisCache
    ...
    set_llm_cache(make_tracking_cache(RedisCache)(redis_client=redis_client(), ttl=int(os.environ.get("LLM_CACHE_TTL_SECONDS", "86400")), prefix="lisa:llm"))
```

Run with `--with langchain-redis --with "langchain-core==1.5.3"`. With the prefix, the LLM cache could share db 0 safely, but keeping LISA's caches in db 1 is still the tidiest.

---

## 3. Why the rest does not fit

| Feature | LISA's situation |
|---|---|
| **`RedisStore`** (namespaced key-value, optional vectors, TTL) | User memory (`reporter_facts`, profiles, history) and `outcomes` are queried with SQL by the dashboard and `/api/insights/outcomes`, deleted per reporter, and must never expire or be evicted. A Store would add a second copy of personal data without replacing the tables |
| **`SemanticCacheMiddleware`** | Returns a stored LLM answer for a *similar* prompt. Unsafe for tickets: another device, CI or reporter gets the wrong answer, possibly with another person's details (caching plan §4, notebook Part 2) |
| **`ToolResultCacheMiddleware`** | LISA's agent tools are `search_kb` (its embeddings and rerank are already cached underneath), `ask_user` (never cacheable) and password reset / browser clear (side effects, never cacheable). Little left to cache |
| **`ConversationMemoryMiddleware`** | Injects semantically similar *past* messages into the prompt. That risks pulling another ticket's conversation into this one; the checkpoint already holds this ticket's conversation |
| **`SemanticRouterMiddleware`** | LISA routes with triage rules, the ML service and playbooks, which are auditable. A similarity router would be a less explainable second path |
| **All middleware** | Requires `langchain.agents.create_agent` and async calls. LISA's agent mode is a custom `StateGraph` (`agentic/workflow.py`) invoked synchronously; adopting it means rewriting agent mode |
| **`RedisVectorStore`** | Qdrant already holds the KB and past incidents with BM25 and a reranker; moving means re-indexing and re-tuning for no gain |

---

## 4. Details from the LangChain Redis page that matter for LISA

| The page says | For LISA (tested where marked) |
|---|---|
| `set_llm_cache(RedisCache(redis_client))` | **The example fails as written** (tested: `AttributeError: 'Redis' object has no attribute 'startswith'`). In `langchain-redis` 0.2.5 the first argument is `redis_url`. Use `RedisCache(redis_client=client, prefix="lisa:llm")` |
| Run Redis with `docker run ... redis redis-server --save 60 1` | The plain `redis` image has the search and JSON modules only from Redis 8, so checkpoints, the semantic cache and the vector store need Redis 8 or Redis Stack. Your `redis/redis-stack` container has them; keep it. Their `--save 60 1` (a snapshot every minute if anything changed) is better than your container's default, but for checkpoints use AOF (§1) |
| `redis+sentinel://...` connection URLs | **That URL form is a LangChain convention.** Plain redis-py (`Redis.from_url`, used by LISA's `infra.py`) rejects it (tested: "must specify one of redis://, rediss://, unix://"). If production uses Redis Sentinel (the Local VMs column of the hosting plan), `infra.redis_client()` needs Sentinel support (`redis.sentinel.Sentinel(...).master_for(...)`) |
| Redis Cluster is not supported for `redis_url` methods | Use a non-cluster endpoint, or pass a pre-built client. Azure Managed Redis presents one endpoint through its proxy, and the langgraph-redis README says to use a standard (non-cluster) client with it |
| `RedisSemanticCache` with `set_llm_cache(...)` | `set_llm_cache` is **global**: it would apply similarity matching to **every** LLM call in LISA (triage, classification, drafts, reply checks), not just a chosen one. Unsafe (caching plan §4) |

---

## 5. LangGraph's memory guide vs LISA

| The guide | What LISA does | Verdict |
|---|---|---|
| **Short-term memory** = a checkpointer + `thread_id` | Exactly this: one thread per incident, `SqliteSaver` on the PC (`workflows/common.py:make_checkpointer`) | Already in place |
| **Production checkpointer:** `PostgresSaver` (or MongoDB / Redis / Oracle) | `DATABASE_URL` → `PostgresSaver` via a connection pool (update pack). **Now tested** against PostgreSQL 16 (above). Redis also tested (§1). MongoDB / Oracle: not relevant | Use PostgreSQL in production |
| Hand-made connections need `autocommit=True` and `row_factory=dict_row` | `infra.pg_pool()` sets `autocommit` and `prepare_threshold=0`. The installed `PostgresSaver` sets `dict_row` on its own cursors, so it works (tested) | OK. Add `row_factory=dict_row` to the pool kwargs if you upgrade and it breaks |
| **Call `setup()` once; run migrations as a deploy step or at startup** | `setup()` runs at import in each of the three graph modules | Fine on one PC. With several replicas starting together, run it once as a deploy step and skip it in the replicas (a small setting) |
| Subgraphs inherit the parent's checkpointer | LISA's graphs are flat (no subgraphs) | n/a |
| **Long-term memory = a Store** (`InMemoryStore` / `PostgresStore` / `RedisStore`), namespaces per user, `runtime.store` in nodes, optional semantic search | User memory is `reporter_facts` (confirmed phone with source and date) and `outcomes` (analytics), in SQL tables | **Keep the tables.** The guide's example lets the LLM write free-text memories ("User name is Bob") and pulls them into the prompt by similarity. For LISA that means unverified, hard-to-delete personal data. LISA's facts are structured, sourced, offered for confirmation, deletable per reporter, and queried with SQL. A `PostgresStore` could hold them, but it adds nothing over the tables and loses the simple SQL |
| **Manage short-term memory:** trim, delete or summarize messages when history outgrows the context window | The staged graph keeps **structured fields**, not a growing message list. Agent mode's history is capped at **24 steps per round** (`AGENT_MAX_STEPS`); ticket conversations are short | Not needed now. If added later, don't trim from the start of the history: that changes the prompt prefix and defeats the provider's prompt caching (caching plan C1). Shorten bulky tool results (KB text) instead |
| **Manage checkpoints:** `get_state`, `get_state_history`, `delete_thread` | Used: follow-up queues (`get_state`), Trace tab (`get_state_history`), retention purge (`delete_thread`) | Works on SQLite, PostgreSQL and Redis (tested) |

---

## 6. Recommendation

1. **Do now (small):** switch the LLM cache to `langchain_redis.RedisCache` with `prefix="lisa:llm"`.
2. **Do as an opt-in setting:** `CHECKPOINTER=redis` (`RedisSaver`, db 0). Use it on your PC when you want to run more than one backend process, after re-creating the container with AOF. Keep SQLite as the default.
3. **Production:** keep `PostgresSaver` for checkpoints. PostgreSQL is needed anyway for the other tables.
4. **Skip:** `RedisStore` / `PostgresStore` for user memory, the middleware, `RedisSemanticCache` and `RedisVectorStore`.
5. **Before production on Redis Sentinel:** add Sentinel support to `infra.redis_client()`.
6. **With several replicas:** run the checkpointer `setup()` once as a deploy step.

Neither change is in the update pack yet. Both are small: `workflows/common.py`, two settings, and the extra packages with `uv run --with ...`.

# LISA — Caching plan (with Redis Stack on your PC, incl. semantic caching)

This plan is based on the LISA code (hybrid / PROD copy). It covers what is cached, where the same work is repeated, and what is worth caching.
Most caches are **already implemented in the memory & cache update pack** (`MEMORY_AND_CACHE.md`). With your **Redis Stack in Docker**, they are shared by every backend process and survive backend restarts.

**Setup** is in [`LISA-memory-integration-guide.md`](LISA-memory-integration-guide.md), steps 0–1. In short:
- `REDIS_URL=redis://localhost:6379/1`, optionally with `LLM_CACHE_BACKEND=redis`;
- run with `uv run --with redis ...`;
- LISA uses **db 1**, and **db 0** is for vector / semantic-cache experiments (search indexes only work in db 0).

Every cache falls back to process memory when `REDIS_URL` is unset, and to "no cache" if Redis stops. Nothing breaks.

> **Which LISA version?** Written against the **hybrid PROD copy** (`LISA-Backend-playbooks-v2-hybrid-PROD`), where the update pack was built and tested. It applies to `-hybrid` unchanged. For **v2**:
> - C3 (triage ML cache) does not apply: v2 predicts CI / priority in-process.
> - C5 (rerank cache) matters even more, because the CI and priority branches each rerank. v2 has only the Qwen reranker, so only the per-pair cache applies.

---

## 1. Verdict and status

| Cache | Use it? | Where | **Status on your PC** |
|---|---|---|---|
| **C1 Provider prompt caching** (Azure OpenAI / Bedrock) | **Yes — the biggest win** | Every LLM call, above all agent mode | **Not yet.** Measuring is done: `token_usage.cached_input_tokens`. The prompt restructure is still to do |
| **C2 Exact LLM response cache** | Yes | `set_llm_cache` | **Done.** `LLM_CACHE_BACKEND=redis` → Redis Stack db 1, TTL 1 day |
| **C3 Triage ML result cache** | Yes | `ml_triage.classify` | **Done.** Only `ok` results, 10 min. The back-off is shared through Redis |
| **C4 Embedding cache** | Yes | The KB and similar-incident embedding clients | **Done.** 1 day |
| **C5 Rerank score cache** | Yes | `score_candidates` | **Done.** Qwen per pair; Azure listwise only for an identical full batch. 1 day |
| **C6 ServiceNow read cache** | Yes | Queue list, caller profile, journals, CMDB lookup | **Done.** 20 s / 10 min / 60 s / 1 h, cleared by LISA's own writes |
| **C7 Health-check cache** | Yes | `check_model_health` | **Done** for the model check, 10 s per process. `check_qdrant_health` not yet |
| **C8 Dashboard read model** | Yes (a table, not a cache) | `/api/resolution-engine`, follow-up queues | Not yet |
| **C9 Static content** | Yes (light) | Playbooks, tool catalog, frontend assets | Not yet (production only) |
| **Semantic cache of LLM answers to tickets** | **No** | — | Not planned (§4) |
| **Semantic FAQ cache** (generic how-to answers only) | Optional, experimental | RedisVL `SemanticCache` in Redis Stack db 0 | **Notebook only** (`LISA_semantic_cache_redisvl.ipynb`); not wired into LISA (§4.3) |

---

## 2. What is cached now, and where

With `REDIS_URL=redis://localhost:6379/1`:

| Item | Key in Redis (db 1) | TTL | Setting |
|---|---|---|---|
| Exact LLM answers | LangChain's keys (no prefix) | 1 day | `LLM_CACHE_BACKEND=redis`, `LLM_CACHE_TTL_SECONDS` |
| Embeddings | `lisa:cache:v1:embed:<sha256>` | 1 day | `CACHE_TTL_EMBED` |
| Reranker scores (Qwen, per pair) | `lisa:cache:v1:rerank:<sha256>` | 1 day | `CACHE_TTL_RERANK` |
| Reranker scores (Azure, whole batch) | `lisa:cache:v1:rerank_list:<sha256>` | 1 day | `CACHE_TTL_RERANK` |
| Triage ML results | `lisa:cache:v1:ml:<sha256>` | 10 min | `CACHE_TTL_ML` |
| ServiceNow queue / profile / journal | `lisa:cache:v1:sn_queue` / `sn_profile` / `sn_journal:…` | 20 s / 10 min / 60 s | `CACHE_TTL_SN_*` |
| ServiceNow write generations (cache invalidation) | `lisa:cache:v1:sn_gen:<sha256>` | 1 day | — |
| CMDB live lookups | `lisa:cache:v1:cmdb:<sha256>` | 1 hour | `CACHE_TTL_CMDB` |
| ML-service back-off | `lisa:ml_down:<sha256 of URL>` | 30 s | `ML_TRIAGE_BACKOFF_SECONDS` |
| Resume lock (not a cache, same Redis) | `lisa:lock:resume:<thread_id>` | 10 min | `RESUME_LOCK_SECONDS` |
| Reset secrets (not a cache, same Redis, encrypted) | `lisa:reset:<thread_id>` | 15 min | `SECRET_SLOT_KEY` |

**Still in process memory** (one process, by design):

| Item | Why it stays local |
|---|---|
| Model health check, 10 s | A health check should reflect what *this* process can reach (`HEALTH_CACHE_SECONDS`) |
| Qwen reranker weights | Model weights, not results |
| Tools-server catalog, 5 min | Small. Could move to Redis later (C9) |
| CMDB application names | Loaded once at import |

**Switches:**
- `LISA_CACHE=off` turns off every `lisa:cache:*` cache.
- **Changing `CACHE_VERSION`** (for example `v2`) makes LISA ignore every old cached value at once. Do it after re-training the ML models, re-indexing the KB or changing the embedding model. Old keys simply expire.
- **Hit rates:** `GET http://127.0.0.1:8000/api/insights/cache`.

**First, measure where the tokens go** (on `data/dashboard.sqlite`):

```sql
SELECT node_name, COUNT(*) AS calls, SUM(cache_hit) AS exact_cache_hits,
       SUM(input_tokens) AS input_tokens, SUM(cached_input_tokens) AS provider_cached_tokens
FROM token_usage GROUP BY node_name ORDER BY input_tokens DESC;
```

The nodes with the most input tokens and few `provider_cached_tokens` are where C1 pays most.

---

## 3. The caches

### C1 — Provider prompt caching (restructure prompts static-first) — not yet

**The finding:** the large prompts start with the ticket and put the unchanging parts after it, so the provider cannot reuse the prefix.

| Prompt | Starts with | Static part after the ticket text |
|---|---|---|
| `completeness_agent.assess_completeness` | `Incident: {incident_description}` | Category block + playbooks + `FOLLOWUP_GUIDANCE` |
| `core_nodes.write_resolution` | The incident text | KB context + guidelines + playbooks |
| `classification_agent.classify_incident` | `Incident: …` | `SUMMARY_GUIDANCE` (~750 tokens) + `POLICY_SIGNAL_GUIDANCE` (~950 tokens) |
| `targeted_followup_agent` (two prompts) | The incident text / KB | `TARGETED_*_GUIDANCE` + playbooks |
| `triage_agent._build_prompt` | **Static first, incident last** | Already good |
| `agentic/agent.py` `SYSTEM_PROMPT` | Static rules + playbooks; history grows over up to 24 calls per round | Good shape; saves most |

- **Azure OpenAI** reuses a repeated **prompt prefix** automatically on supported models (prompts over ~1,024 tokens), at a discount. Bedrock / Anthropic need explicit cache breakpoints.
- **This has nothing to do with Redis:** the provider caches on its side.

**The change:** send each structured call as a **system message** (static: persona → common playbook → category → family / CI → flows → guidance → output rules) plus a **user message** (this ticket: incident text → answers → known context → history → KB). Do it one node per PR. Replay saved incidents to check that the questions and drafts are unchanged.

**Measure it:** `token_usage.cached_input_tokens` is now recorded from the provider's `usage_metadata.input_token_details.cache_read`.

### C2 — Exact LLM response cache in Redis — done

`workflows/common.py`: `LLM_CACHE_BACKEND=redis` → LangChain `RedisCache` with the cache-hit tracking mixin (`token_usage.cache_hit` still works), TTL `LLM_CACHE_TTL_SECONDS`.
- **Tested against Redis Stack.** If Redis stops, LangChain logs "Redis lookup failed" and calls the model.
- **Use db 1:** the keys have no `lisa:` prefix, and `RedisCache.clear()` would empty the whole database.
- **Invalidation:** the key covers the prompt + model settings, so a changed prompt is a new key. After a playbook change, change `CACHE_VERSION`, or run `docker exec redis-stack redis-cli -n 1 FLUSHDB` on your PC. FLUSHDB also drops in-progress reset secrets, so do it between tests.
- **Privacy:** cached prompts contain ticket text.
  - **On your PC:** this is acceptable. Redis is reachable only from your machine (Docker publishes it on localhost).
  - **In production:** TLS + ACL, a private network, no or encrypted persistence.
- **Follow-up:** `langchain_community` is being retired. Its `RedisCache` has a successor in the `langchain-redis` package; switch when convenient.

### C3 — Triage ML result cache — done

`ml_triage.classify` is called by the early look and again by the real classification.
- **Key:** ML URL + text + impact + submitted CI + opened_at.
- **Only `status: ok` results are kept:** errors and partial results are never cached.
- **Shared back-off:** after a failure, every process skips the ML service for 30 s (`lisa:ml_down:*`).
- **New model:** change `CACHE_VERSION`. Pinning the key to the model sha256 from `/health` is a possible later improvement.

### C4 — Embedding cache — done

`CachedEmbeddingClient` wraps the embedding client of both services (`ci_classifier_agent.py`, `kb_search_agent.py`).
- **Key:** client module + model name + method (`embed_single_text` / `embed_query_text`) + text, hashed.
- **Lifetime:** 1 day.
- **Size:** a vector is a few KB in Redis.

### C5 — Rerank score cache — done

`CachedRerankingClient` wraps the reranker of both services:
- **Qwen** scores each (query, candidate) pair on its own, so scores are cached per pair and **only uncached candidates are scored**.
- **Azure listwise** (`RERANKER=azure`) scores candidates *together*, so only an **identical full batch** (same query, same candidates, same order) is reused. This avoids the partial-batch problem.
- `pop_last_usage()` passes through. A full cache hit made no call, so it records no usage.

### C6 — ServiceNow read cache — done

| Read | Function | TTL | Cleared when |
|---|---|---|---|
| Queue list (up to 200 rows) | `list_incidents_for_dashboard` | 20 s | LISA writes to any incident (resolve, work note, comment, CI update) |
| Caller profile | `get_reporter_profile` | 10 min (empty profiles not cached) | — |
| Journals | `get_journal_entries` | 60 s | LISA writes a work note / comment / resolution on that incident |
| CMDB server lookup | `cmdb_lookup` (live mode) | 1 h (found or not found) | — |

Clearing works with a write **generation** counter: a write bumps it, so old entries are no longer looked up and simply expire. **Never cached:** `get_incident_record` (the ticket at intake) or any write.

### C7 — Health-check cache — done (models), open (Qdrant)

`check_model_health()` is reused for `HEALTH_CACHE_SECONDS` (default 10) per process. A dependency that goes down is noticed within 10 s. `check_qdrant_health()` should get the same treatment.

### C8 — Dashboard read model — not yet

`/api/resolution-engine`, `/api/followup/queue`, `/confirmations` and `/notices` call `app.get_state()` for every listed incident on every poll.
- **The change:** extend `sync_trace_to_db` (it runs after every `invoke`) to fill display columns, and let the GET routes read only tables.
- **No cache, so no invalidation is needed.**

### C9 — Static content — not yet (production)

- **Playbooks:** load once per process in production (immutable image); keep re-reading in dev.
- **Tool catalog:** optionally move it to Redis.
- **Frontend:** hashed assets with long cache headers.

---

## 4. Semantic caching — what Redis Stack makes possible, and where it is (not) safe

With Redis Stack you can now run a **semantic cache** locally (RedisVL `SemanticCache`, vector search in db 0). The notebook `LISA_semantic_cache_redisvl.ipynb` does this with LISA's own embedding model (`qwen3-embedding:0.6b`) and LLM.

### 4.1 Why not for ticket answers

| Risk | In LISA |
|---|---|
| **Wrong specifics** | Two "Outlook not opening" tickets look alike, but the draft must use *this* reporter's device, location, flight, KB match and priority |
| **Leaking one user's data to another** | Prompts and answers contain names, staff IDs, phone numbers, flights and incident numbers |
| **Weakened guardrails** | A cached "clean" triage verdict reused for a similar-looking text could skip the check for a malicious variant |
| **Stale after changes** | A changed playbook, KB article or model keeps serving old answers |
| **Audit** | "Why did LISA say this?" becomes "because another ticket was similar" |
| **Little to gain** | Each ticket's prompt combines unique context; true repeats are rare |

The notebook demonstrates the first risk (Part 2, section 10). In its test run, "VPN fails on my **iPhone**" and "**Outlook** fails on my laptop" ended up as close to a stored "VPN fails on my **laptop**" as a real paraphrase did. A threshold that catches the paraphrase therefore also serves the wrong answer. Run it with `qwen3-embedding:0.6b` to see the distances for your model.
C2 (exact) + C1 (provider prefix) give the safe savings.

### 4.2 The real version of "many similar tickets": outage correlation

When 30 people report "ResConnect not loading" within 10 minutes, link them to **one major incident**; don't serve a cached reply.
- Compare new tickets' embeddings (C4 makes this cheap) with tickets from the last N minutes, filtered on the same CI.
- Raise the Major Incident alert.

Use **Qdrant**, where past incidents already live, not a second vector store. Build it after the outcome data shows the pattern.

### 4.3 If you want a semantic FAQ cache

Only for **generic, PII-free how-to answers**, for example "how do I clear the Chrome cache". The notebook's Part 2, section 12, has the pattern:

| Rule | How |
|---|---|
| Scope every hit | Filters: same CI / playbook **and** same `cache_version`. Change the version when the KB or prompts change |
| No personal or ticket data | Skip the cache if the question contains an incident number, staff number, phone or email; never store such answers |
| A measured threshold | Label ~50 real question pairs ("should hit" / "must miss") and use the notebook's section 11. If no threshold gives correct hits without wrong ones, don't use it |
| Store only good answers | For example, only after the reporter confirmed it fixed the problem (`outcomes.reporter_confirmed`) |
| Expire | TTL, e.g. 1 day |
| Where | Redis Stack **db 0** (search indexes only work there), separate from LISA's db 1 |

It is not wired into LISA. Doing so would be a new, opt-in node for agent `search_kb(general=true)` answers, after the notebook shows a safe threshold on your data.

---

## 5. The cache helper (as implemented)

`incident_graph/cache.py`:
- `get` / `put` / `delete` / `cached(ns, key, seconds, compute, store_if=None)`.
- **Backend:** Redis when `REDIS_URL` is set, else an in-process dict (max 5,000 items).
- **Key rules:** `lisa:cache:<CACHE_VERSION>:<namespace>:<sha256 of the inputs>`. Ticket text never appears in a key or a log line.
- **A Redis error is logged once** and the value is computed as if nothing was cached.
- **Hit / miss counters** are exported by `/api/insights/cache`.
- **Client wrappers:** `CachedEmbeddingClient`, `CachedRerankingClient`.

---

## 6. Hosting

| | **Your PC (now)** | Local VMs | AWS | Azure |
|---|---|---|---|---|
| Cache + locks (C2–C6) | Redis Stack in Docker, **db 1** | Redis (Sentinel), TLS + ACL, `allkeys-lru` | ElastiCache (Valkey / Redis OSS) | Azure Managed Redis |
| Reset secrets | Same Redis Stack, db 1 (default `noeviction`, no memory limit) | **Separate** Redis instance with `noeviction` | Separate ElastiCache cluster | Separate Azure Managed Redis |
| Semantic FAQ cache (optional) | Redis Stack **db 0** | Redis Stack / Redis 8 | A Redis service with vector search (check support) | Azure Managed Redis with the search module |
| Frontend | Vite dev server | Nginx cache headers | CloudFront | Front Door / Static Web Apps CDN |

**Sizing:** small. Embeddings are a few KB each, LLM answers a few KB, scores bytes. On your PC: a few MB. Production: start with 1–2 GB for the cache instance.

**Eviction is set per Redis instance, not per database.** The earlier "separate database with `noeviction`" idea does not work. In production, secrets and cache need **separate instances**, so LRU eviction of the cache can never delete a reset secret. On your PC one instance is fine (no limit, `noeviction`).

**Persistence:** the Redis Stack image snapshots to `/data` every few minutes (no AOF).
- That's fine for caches.
- Secrets are encrypted at rest, and they expire.
- To keep data across `docker rm`, use the volume from integration guide step 0.2.

---

## 7. Measuring

| Metric | Source |
|---|---|
| Hit rate per cache (C3–C6) | `GET /api/insights/cache` (per process) |
| Exact LLM cache hits per node (C2) | `token_usage.cache_hit` |
| Provider-cached input tokens per node (C1) | `token_usage.cached_input_tokens` |
| Redis as a whole | `docker exec redis-stack redis-cli INFO stats` (`keyspace_hits`, `keyspace_misses`), `INFO memory`, `-n 1 DBSIZE`; or RedisInsight at http://localhost:8001 (database index 1) |
| LLM calls and tokens per ticket, before / after | `token_usage`, `outcomes.total_tokens` |
| ServiceNow calls per minute | `servicenow_client._request` (a counter is still to add) |

---

## 8. What is left, in order

| Order | Item | Effort | Why |
|---|---|---|---|
| 1 | Switch on Redis on your PC (integration guide steps 0–1) and check `/api/insights/cache` | Minutes | Everything below C1 is already built |
| 2 | Measure `token_usage` (§2 SQL) | Minutes | Know where the tokens go |
| 3 | **C1** prompt restructure, one node per PR, replay-checked | Medium | Biggest cost and latency saving |
| 4 | C7 for `check_qdrant_health` | Small | Removes a probe per request |
| 5 | C8 dashboard read model | Medium | Dashboard speed as incidents grow |
| 6 | Move C2 to `langchain-redis` | Small | `langchain_community` is being retired |
| 7 | C9 playbooks per version | Small | Production only |
| — | Semantic FAQ cache | — | Only if the notebook shows a safe threshold on your real questions (§4.3) |
| — | Semantic cache of ticket answers | — | Not planned (§4.1) |

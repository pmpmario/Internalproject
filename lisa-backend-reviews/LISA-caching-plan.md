# LISA — Caching plan (incl. semantic caching)

This plan is based on the LISA code (hybrid / PROD copy): what is cached today, where the same work
is repeated, and what is worth caching in production. It uses the Redis and settings introduced in
[`LISA-memory-integration-guide.md`](LISA-memory-integration-guide.md). Every cache falls back to
in-process (or off) when `REDIS_URL` is unset.


> **Which LISA version?** Written against the **hybrid PROD copy**
> (`LISA-Backend-playbooks-v2-hybrid-PROD`). Its code is identical to `-hybrid`, so it applies there
> unchanged. It **also applies to v2** (`LISA-Backend-playbooks-v2`): the files it changes —
> `password_reset.py`, `db.py`, `workflows/common.py`, `routers/followup.py`, `token_tracking.py`,
> `completeness_agent.py` — are identical in v2. v2 differences: C3 (triage ML cache) does not apply — v2 predicts CI / priority in-process; C5 (rerank cache) matters even more in v2, because the CI and priority branches each rerank (unless `PRIORITY_EVIDENCE_FROM_CI` reuses them), but v2 has only the Qwen reranker (no `RERANKER=azure`), so there is no partial-batch caveat.

---

## 1. Verdict

| Cache | Use it? | Where | Why |
|---|---|---|---|
| **C1 Provider prompt caching** (Azure OpenAI / Bedrock) | **Yes — the biggest win** | Every LLM call, above all **agent mode** | Large unchanging instructions are sent on every call; today they come *after* the ticket text, so the provider cannot reuse them |
| **C2 Exact LLM response cache** | **Yes (keep, move to Redis)** | `set_llm_cache` | Already there; serves repeated identical calls and retries. Must be shared by replicas |
| **C3 Triage ML result cache** | **Yes** | `ml_triage.classify` | Called up to twice per ticket (early look + real classification), often with the same text |
| **C4 Embedding cache** | Yes (cheap) | The 3 embedding clients | Same text embedded again on retries, agent re-searches and the KB retry loop |
| **C5 Rerank score cache** | **Yes**, especially with `RERANKER=azure` | `score_candidates` | A rerank is a gpt-4o call (Azure) or a slow CPU pass (Qwen); repeated searches re-score the same candidates |
| **C6 ServiceNow read cache** | **Yes** | Queue list, caller profile, journals | The dashboard polls every 20 s and each poll lists up to 200 tickets from ServiceNow |
| **C7 Health-check cache** | Yes | `check_model_health`, `check_qdrant_health` | They run on every run / continue / overview request |
| **C8 Dashboard read model** | Yes (a table, not a cache) | `/api/resolution-engine`, follow-up queues | They load the checkpoint state of **every** incident on every poll |
| **C9 Static content** | Yes (light) | Playbooks, tool catalog, CMDB app list, frontend assets | Immutable per release |
| **Semantic cache of LLM answers** | **No** | — | Unsafe for this project (§4) |
| Semantic cache of KB lookups for general questions | Optional, later | Agent `search_kb(general=true)` | Only if outcome data shows many repeated general questions (§4.3) |

---

## 2. What is cached today

| Item | Code | Scope |
|---|---|---|
| LLM responses, exact match on prompt + model settings | `workflows/common.py` → `make_tracking_cache(InMemoryCache)` (or `SQLiteCache`), `LLM_CACHE_BACKEND` | One process; hits recorded in `token_usage.cache_hit` |
| Qwen reranker weights | `_shared_qwen_reranker_cache.py` | One process (model load, not results) |
| Tools-server tool list / schemas | `tools_client._published()`, 5 min (30 s when down) | One process |
| CMDB application names | `context_extraction_agent._CI_APP_NAMES`, loaded at import | One process |
| ML-service back-off | `ml_triage._down_until`, 30 s | One process |
| Playbooks | **Not cached**: about 25 files re-read on every call (`playbooks/loader.py`), on purpose for live editing | — |

**First, measure what the current cache does** (the data already exists):

```sql
SELECT node_name, COUNT(*) AS calls, SUM(cache_hit) AS hits,
       ROUND(100.0 * SUM(cache_hit) / COUNT(*), 1) AS hit_pct,
       SUM(input_tokens) AS input_tokens
FROM token_usage
GROUP BY node_name
ORDER BY input_tokens DESC;
```

The nodes with the most input tokens are where prompt caching (C1) pays most.

---

## 3. The caches to build

### C1 — Provider prompt caching (restructure prompts static-first)

**The finding:** the large prompts start with the ticket and put the unchanging parts after it.

| Prompt | Starts with | Static part after the ticket text |
|---|---|---|
| `completeness_agent.assess_completeness` | `Incident: {incident_description}` | Category block + playbooks + `FOLLOWUP_GUIDANCE` |
| `core_nodes.write_resolution` | The incident text | KB context + guidelines + playbooks (`flows/draft_writing.md`) |
| `classification_agent.classify_incident` | `Incident: …` | `SUMMARY_GUIDANCE` (~750 tokens) + `POLICY_SIGNAL_GUIDANCE` (~950 tokens) |
| `targeted_followup_agent` (two prompts) | The incident text / KB | `TARGETED_*_GUIDANCE` + playbooks |
| `triage_agent._build_prompt` | **Static first, incident last** | Already good |
| `agentic/agent.py` `SYSTEM_PROMPT` | Static rules + playbooks, incident at the end; the history grows over **up to 24 calls per round** | Good shape. This is where caching saves most |

The playbooks alone add up to ~7,000 characters (`common.md`) + 5,000 (category) + 3,500 per
family / CI / flow (`loader.LIMITS`) per call. `INCIDENT_CATEGORY_GUIDANCE` is ~2,700 tokens.

**How providers cache:**
- **Azure OpenAI** caches a repeated **prompt prefix** automatically on supported models (prompts
  over ~1,024 tokens). Cached input tokens are billed at a discount and processed faster.
- **Bedrock / Anthropic** need explicit cache breakpoints.

Check your deployment and region for the exact rules and prices. Either way, **only an identical
prefix is reused**.

**The change:** send every structured call as **system message (static) + user message (this
ticket)**, and order the static part from most shared to least shared:

```
system:  persona → common.md → category playbook → family / CI playbook → flow playbooks → guidance constants → output rules
user:    incident text → follow-up answers → known context → reporter history → KB articles
```

`make_structured_llm(...).invoke(...)` already accepts a list of messages, so it is a per-node edit:

```python
from langchain_core.messages import HumanMessage, SystemMessage

static = f"{REPORTER_PERSONA}\n{playbook_block}\n{FOLLOWUP_GUIDANCE}\n{output_rules}"     # no ticket data in here
dynamic = f"Incident: {state['incident_description']}\n{answers}\n{known}\n{history}"
check = structured_llm.invoke([SystemMessage(static), HumanMessage(dynamic)],
                              config=llm_call_config(config, "assess_completeness"))
```

**Rules:**
- Nothing per-ticket (dates, names, IDs) in the system part.
- Keep the playbook order stable (the loader already sorts common → category → family → CI).

**Measure it:** `TokenTrackingCallback.on_llm_end` reads `usage_metadata`. Also store
`usage_metadata.get("input_token_details", {}).get("cache_read")` in a new
`token_usage.cached_input_tokens` column, and show it on the Token usage tab.

**Re-check after the change:** the prompts' wording is unchanged but the layout moves, so replay
saved incidents and compare the questions and drafts (the hybrid project's
`scripts/replay_playbooks.py` + the test suite).

### C2 — Exact LLM response cache → Redis

Keep the current behaviour, but shared by all replicas. A ticket resumed on another replica, or a
retried request, then gets the same answer:

```python
elif LLM_CACHE_BACKEND == "redis":
    from langchain_community.cache import RedisCache
    from incident_graph.infra import redis_client
    set_llm_cache(make_tracking_cache(RedisCache)(redis_=redis_client(), ttl=24 * 3600))
```

- **Key:** LangChain keys on prompt + model settings. Add a **prompt version** (the git commit of
  prompts / playbooks) to the model kwargs or the namespace, so a deploy invalidates old entries.
- **Privacy:** cached prompts contain ticket text. Use Redis with TLS + ACL on a private network,
  a 24 h TTL, and no persistence to disk, or encrypted persistence.

### C3 — Triage ML result cache

`ml_triage.classify` is called by the early look and again by the real classification. When the
reporter has added nothing, the text is identical:

```python
# ml_triage.py
import hashlib, json
from incident_graph.cache import cached     # helper below

def classify(text, impact=None, submitted_ci=None, opened_at=None) -> dict:
    key = hashlib.sha256(json.dumps([text, impact, submitted_ci, _model_version()]).encode()).hexdigest()
    return cached("ml", key, ttl=3600, compute=lambda: _classify_uncached(text, impact, submitted_ci, opened_at),
                  store_if=lambda r: r.get("status") == "ok")          # never cache an error result
```

`_model_version()` comes from the service's `/health` (the model sha256 values, refreshed every few
minutes), so a new model invalidates the cache. This is also hosting-doc item S8.

### C4 — Embedding cache

The key is `sha256(embedding model + query prefix + text)` → the vector (float32 bytes), TTL 24 h.
Wrap `embed_single_text` / `embed_query_text` in the embedding clients. Hits come from:
- retries
- agent re-searches with the same query
- the KB retry loop (review §4.3)
- the early look → real classification (when the text is unchanged)

This is cheap in Redis memory (a vector is about 4 KB).

### C5 — Rerank score cache

The key is `(reranker id, sha256(query), candidate id, candidate version)` → score, TTL 24 h. Only
**uncached** candidates go to the reranker in one batch, which keeps the Azure listwise call
smaller:

```python
def score_candidates_cached(client, query, candidates: list[tuple[str, str]]) -> list[float]:
    """candidates: (stable_id, text). stable_id = KB number + version, or the incident number."""
    keys = [f"{client.model_path}:{_h(query)}:{cid}" for cid, _ in candidates]
    hits = cache_mget("rerank", keys)
    missing = [i for i, v in enumerate(hits) if v is None]
    if missing:
        scores = client.score_candidates(query, [candidates[i][1] for i in missing])
        cache_mset("rerank", {keys[i]: s for i, s in zip(missing, scores)}, ttl=24 * 3600)
        for i, s in zip(missing, scores):
            hits[i] = s
    return hits
```

- **Batch effect with Azure:** the listwise reranker scores each candidate *in the context of the
  others*. Scores for a partial batch can differ slightly from a full batch. Compare on saved
  searches before enabling partial batches. If needed, cache only exact full-batch results
  (key = query + all candidate IDs).
- **Usage tracking:** `pop_last_usage()` must still record the call (or a cache hit) so token
  tracking stays correct.

### C6 — ServiceNow read cache

| Read | Function | TTL | Invalidate when |
|---|---|---|---|
| Queue list (up to 200 rows) | `list_incidents_for_dashboard` | 20 s (= the dashboard poll interval) | A ticket is started from the queue |
| Caller profile (`caller_id.*`) | `get_reporter_profile` | 10 min | — |
| Journals (work notes / comments) | `get_journal_entries` | 60 s | LISA writes a work note / comment on that ticket (`append_work_note`, `add_customer_comment`) |
| CMDB server lookup | `cmdb_lookup._live` | 1 h | — |

**Never cache:** `get_incident_record` (the ticket itself at intake), or any write.

This also protects the ServiceNow API rate limits from dashboard polling with several users.

### C7 — Health-check cache

`check_model_health()` (pings Ollama `/api/tags`, checks the reranker) and `check_qdrant_health()`
run on every `run-agent`, `continue`, voice turn and overview load. Cache the result for 15 s per
process. A dependency that goes down is then noticed within 15 s, without a probe on every request.

### C8 — Dashboard read model (instead of caching state reads)

`/api/resolution-engine`, `/api/followup/queue`, `/confirmations` and `/notices` call
`app.get_state()` for every listed incident on every poll. `/api/resolution-engine` also writes two
tables per incident on each GET.

Write the display fields once, when the state changes. `sync_trace_to_db` already runs after every
`invoke`, so extend it to fill `pending_question`, `draft_resolution`, `kb_titles` and
`similar_text` columns, and let the GET routes read only tables. No cache invalidation is needed.

### C9 — Static content

| Item | Production behaviour |
|---|---|
| **Playbooks** | In production they are part of the image (immutable), so load them once per process and key them by the image / playbook version. Keep "re-read on every call" in dev for live editing (`PLAYBOOKS_RELOAD=1`) |
| Tool catalog | Keep the 5 min cache; optionally in Redis so all replicas see the same catalog |
| Frontend assets | CDN / static hosting with hashed file names (Vite does this) and long cache headers; `index.html` not cached |

---

## 4. Semantic caching — why not for LISA's LLM answers

A semantic cache returns a stored LLM answer when a new prompt is *similar* (by embedding) to an old
one. For LISA that is the wrong trade:

| Risk | In LISA |
|---|---|
| **Wrong specifics** | Two "Outlook not opening" tickets look alike, but the draft must use *this* reporter's device, location, flight, KB match and priority. A reused draft or question is wrong for the second person |
| **Leaking one user's data to another** | Prompts and answers contain names, staff IDs, phone numbers, flights and incident numbers. A similar-but-different ticket would get another person's details |
| **Weakened guardrails** | Triage scores prompt injection and pasted secrets. A cached "clean" verdict reused for a similar-looking text could skip the check for a malicious variant |
| **Stale after changes** | A changed playbook, KB article or model would keep serving old answers unless every key carried all versions |
| **Audit** | "Why did LISA say this?" becomes "because another ticket was similar" |
| **Little to gain** | Each ticket's prompt combines unique context (answers, profile, KB set). True semantic repeats are rare, except during an outage |

The exact cache (C2) plus provider prompt caching (C1) give the safe savings: same input → same
output, and a cheaper shared prefix.

### 4.1 The real version of "many similar tickets": outage correlation

When 30 people report "ResConnect not loading" within 10 minutes, the right answer is not a cached
reply. It is to **link them to one major incident**. That is a feature, not a cache:
- compare new tickets' embeddings (C4) with tickets from the last N minutes in Qdrant (a filter on
  time and the same CI);
- above a threshold, tag them as related, raise the Major Incident alert (the triage ML service
  already computes a risk score) and route them together.

Build it only after the outcome data (memory step 6) shows the pattern.

### 4.2 Retrieval-level semantic cache (if ever)

If you ever add one:
- cache **KB search results** (article IDs + scores), never the LLM answer;
- use it only for PII-free **general questions** (agent `search_kb(general=true)`);
- require similarity ≥ 0.97 and key it on the KB index version.

Its value is small: the KB search is already fast compared with the LLM calls.

---

## 5. One small cache helper

```python
# incident_graph/cache.py  (new)
import json, threading, time
from incident_graph.infra import redis_client

_R = redis_client()
_local: dict[str, tuple[float, str]] = {}          # dev fallback: in-process TTL dict
_lock = threading.Lock()
VERSION = "v1"                                      # bump (or set from the release / git sha) to invalidate everything

def _k(ns: str, key: str) -> str:
    return f"lisa:{VERSION}:{ns}:{key}"

def get(ns, key):
    if _R is not None:
        raw = _R.get(_k(ns, key))
        return json.loads(raw) if raw else None
    with _lock:
        hit = _local.get(_k(ns, key))
        return json.loads(hit[1]) if hit and hit[0] > time.time() else None

def put(ns, key, value, ttl: int):
    data = json.dumps(value)
    if _R is not None:
        _R.set(_k(ns, key), data, ex=ttl)
    else:
        with _lock:
            _local[_k(ns, key)] = (time.time() + ttl, data)

def cached(ns, key, ttl, compute, store_if=lambda v: True):
    value = get(ns, key)
    if value is not None:
        _count(ns, hit=True)
        return value
    value = compute()
    if store_if(value):
        put(ns, key, value, ttl)
    _count(ns, hit=False)
    return value

def _count(ns, hit):            # export as metrics (Prometheus / OpenTelemetry): lisa_cache_requests_total{ns, result}
    ...
```

**Key rules:**
- Hash any text that goes into a key (no ticket text in key names or logs).
- Every key carries the version of whatever produced the value: prompt / playbook commit, model
  sha256, KB index version, embedding model.

---

## 6. Hosting

The caches use the **same Redis** as the memory plan, in a separate logical database or key prefix:

| | Local VMs | AWS | Azure |
|---|---|---|---|
| Redis | Redis (Sentinel), TLS + ACL | ElastiCache (Valkey / Redis OSS) | Azure Managed Redis |
| Sizing | Small: embeddings ~4 KB each, LLM responses a few KB, rerank scores bytes. Start with 1–2 GB and `maxmemory-policy allkeys-lru` (LRU eviction) for the cache database | Same | Same |
| Frontend | Nginx cache headers | CloudFront | Front Door / Static Web Apps CDN |

**Keep secrets out of the eviction pool.** Secrets from the memory plan (reset slots) must **not**
sit in an LRU-evicted database. Use a separate Redis database with `noeviction`, or a separate
instance.

---

## 7. Measuring

| Metric | Source |
|---|---|
| Hit rate per cache (C2–C7) | `_count` metrics |
| Cached input tokens per node (C1) | `token_usage.cached_input_tokens` (new) |
| LLM calls and tokens per ticket, before / after | `token_usage` |
| Turn latency per route (p50 / p95) | API metrics |
| ServiceNow calls per minute | `servicenow_client._request` counter |

---

## 8. Rollout order

| Order | Item | Effort | Why first |
|---|---|---|---|
| 1 | Measure today's `token_usage` (§2 SQL) | Minutes | Know where the tokens go |
| 2 | **C1** prompt restructure (static-first, system / user split) + `cached_input_tokens` | Medium; one node per PR, replay-checked | Biggest cost and latency saving, especially in agent mode |
| 3 | C6 ServiceNow read cache, C7 health cache | Small | Protects ServiceNow; removes per-request probes |
| 4 | C2 → Redis | Small | Needed as soon as there are 2+ replicas |
| 5 | C3 ML cache, C4 embedding cache | Small | Removes repeated calls per ticket |
| 6 | C5 rerank cache (validate partial batches first) | Medium | Matters most with `RERANKER=azure` |
| 7 | C8 dashboard read model | Medium | Dashboard speed as incidents grow |
| 8 | C9 playbooks per version | Small | Production only |
| — | Semantic LLM cache | — | Not planned (§4); outage correlation later, as a feature |

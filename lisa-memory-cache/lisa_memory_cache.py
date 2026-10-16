"""Memory and cache for a LangGraph app on PostgreSQL + Redis Stack + Qdrant.

Where everything lives (see README.md for the why):

  PostgreSQL    1. short-term memory   LangGraph checkpointer (PostgresSaver): one thread per ticket, interrupts, history
                2. long-term memory    LangGraph store (PostgresStore): facts per reporter, with source and date
  Redis Stack   3. node cache          LangGraph node cache (langgraph.cache.redis.RedisCache) for pure, expensive nodes
                4. LLM cache           langchain_redis.RedisCache, exact match, attached per model (not global)
                5. data caches         ttl_cache(): API reads, embeddings; bump() invalidates a namespace after a write
                6. coordination        lock(): one worker per ticket; secrets: encrypted, short-lived values
  Qdrant        7. episodic memory     EpisodicMemory: confirmed resolutions of past tickets, searched by similarity

Settings (environment): LISA_POSTGRES_URL, LISA_REDIS_URL, LISA_QDRANT_URL, LISA_SECRET_KEY (Fernet key, for secrets only),
LISA_CACHE_VERSION (bump to drop every cached value), LISA_DB_SETUP (0 = skip table creation; run it once at deploy instead).

Packages: langgraph langgraph-checkpoint-postgres "psycopg[binary,pool]" langchain-redis redis qdrant-client cryptography
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import time
import uuid
from array import array
from collections.abc import Callable, Iterator
from typing import Any

POSTGRES_URL = os.environ.get("LISA_POSTGRES_URL", "postgresql://lisa:lisa@localhost:5432/lisa")
REDIS_URL = os.environ.get("LISA_REDIS_URL", "redis://localhost:6379/0")
QDRANT_URL = os.environ.get("LISA_QDRANT_URL", "http://localhost:6333")
PREFIX = "lisa"


def _version() -> str:
    return os.environ.get("LISA_CACHE_VERSION", "v1")


def _digest(*parts: Any) -> str:
    """Stable hash of the inputs, so no ticket text ever appears in a Redis key."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()[:40]


# ======================================================================= connections (one per process)
@functools.lru_cache(maxsize=1)
def pg_pool():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    # autocommit + dict_row + no prepared statements: what LangGraph's PostgresSaver / PostgresStore expect from a hand-made pool
    return ConnectionPool(POSTGRES_URL, max_size=int(os.environ.get("LISA_POSTGRES_POOL", "10")),
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, open=True)


@functools.lru_cache(maxsize=1)
def redis_client():
    import redis

    return redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)


@functools.lru_cache(maxsize=1)
def qdrant_client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=QDRANT_URL, timeout=10)


def _setup_enabled() -> bool:
    return os.environ.get("LISA_DB_SETUP", "1") != "0"


# ======================================================================= 1. short-term memory: checkpoints in PostgreSQL
def make_checkpointer():
    """LangGraph checkpointer: the whole state of each ticket (thread), its interrupts and its history (time travel / trace)."""
    from langgraph.checkpoint.postgres import PostgresSaver

    saver = PostgresSaver(pg_pool())
    if _setup_enabled():
        saver.setup()                                   # creates / migrates the checkpoint tables (idempotent)
    return saver


# ======================================================================= 2. long-term memory: LangGraph store in PostgreSQL
def make_store(fact_ttl_days: float | None = None):
    """LangGraph store for cross-ticket memory. With fact_ttl_days, unused facts are deleted by a background sweeper."""
    from langgraph.store.postgres import PostgresStore

    ttl = {"default_ttl": fact_ttl_days * 24 * 60, "refresh_on_read": False, "sweep_interval_minutes": 60} if fact_ttl_days else None
    store = PostgresStore(pg_pool(), ttl=ttl)
    if _setup_enabled():
        store.setup()
    if ttl:
        store.start_ttl_sweeper()
    return store


class ReporterMemory:
    """Facts about a reporter that the REPORTER gave or a system of record holds - never facts an LLM inferred.

    Each fact is stored with its source and date, so the app can decide to OFFER it ("Is 055 123 4567 still your number?")
    rather than assume it. Namespace labels in a LangGraph store cannot contain '.', so emails are hashed into the namespace."""

    SOURCES = ("reporter_confirmed", "reporter_corrected", "reporter_provided", "system_of_record")

    def __init__(self, store) -> None:
        self.store = store

    @staticmethod
    def namespace(reporter_id: str) -> tuple[str, ...]:
        return ("reporters", hashlib.sha256(reporter_id.strip().lower().encode()).hexdigest()[:32], "facts")

    def remember(self, reporter_id: str, fact: str, value: str, source: str, ticket_id: str | None = None) -> None:
        if source not in self.SOURCES:
            raise ValueError(f"source must be one of {self.SOURCES}; LLM-inferred facts are not stored")
        self.store.put(self.namespace(reporter_id), fact, {"value": value, "source": source, "observed_at": time.time(), "ticket_id": ticket_id})

    def recall(self, reporter_id: str, fact: str, max_age_days: float = 90) -> dict | None:
        item = self.store.get(self.namespace(reporter_id), fact)
        if item is None or time.time() - item.value["observed_at"] > max_age_days * 86400:
            return None
        return item.value

    def facts(self, reporter_id: str) -> dict[str, dict]:
        return {item.key: item.value for item in self.store.search(self.namespace(reporter_id), limit=100)}

    def forget(self, reporter_id: str) -> int:
        """Privacy request: delete everything remembered about one reporter."""
        keys = list(self.facts(reporter_id))
        for key in keys:
            self.store.delete(self.namespace(reporter_id), key)
        return len(keys)


# ======================================================================= 3. LangGraph node cache in Redis
def make_node_cache():
    """Pass to builder.compile(cache=...). Only nodes added with cache_policy=CachePolicy(ttl=...) are cached, keyed on the node's input.
    Use it for pure, expensive nodes (retrieval, rerank, an ML prediction) - never for nodes that write, ask the user, or interrupt."""
    from langgraph.cache.redis import RedisCache

    return RedisCache(redis_client(), prefix=f"{PREFIX}:node:{_version()}:")


# ======================================================================= 4. exact LLM response cache in Redis
def make_llm_cache(ttl_seconds: int = 86400):
    """Attach per model - ChatModel(..., cache=make_llm_cache()) - on deterministic calls (temperature 0, structured output).
    Avoid set_llm_cache(): it is global and would also cache creative / conversational calls.
    Note: the LangChain docs example RedisCache(redis_client) fails - the first parameter is redis_url; pass redis_client=."""
    from langchain_redis import RedisCache

    return RedisCache(redis_client=redis_client(), ttl=ttl_seconds, prefix=f"{PREFIX}:llm:{_version()}")


# ======================================================================= 5. data caches (API reads, embeddings) in Redis
def _generation(namespace: str) -> int:
    try:
        return int(redis_client().get(f"{PREFIX}:gen:{namespace}") or 0)
    except Exception:  # noqa: BLE001
        return 0


def bump(namespace: str) -> None:
    """Invalidate every cached value of a namespace (call it after a write, e.g. after posting a work note on a ticket)."""
    with contextlib.suppress(Exception):
        redis_client().incr(f"{PREFIX}:gen:{namespace}")


def ttl_cache(namespace: str, ttl_seconds: int, *, keep: Callable[[Any], bool] | None = None):
    """Cache a function's JSON-able result in Redis for ttl_seconds, keyed by a hash of its arguments.
    keep(result) decides what is stored (e.g. never an error result). If Redis is down, the function simply runs."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = f"{PREFIX}:cache:{_version()}:{namespace}:{_generation(namespace)}:{_digest(fn.__module__, fn.__qualname__, args, kwargs)}"
            try:
                raw = redis_client().get(key)
                if raw is not None:
                    return json.loads(raw)
            except Exception:  # noqa: BLE001 - a cache must never break the request
                pass
            result = fn(*args, **kwargs)
            if keep is None or keep(result):
                with contextlib.suppress(Exception):
                    redis_client().set(key, json.dumps(result, default=str), ex=ttl_seconds)
            return result
        return wrapper
    return decorator


def cached_embedder(embed: Callable[[str], list[float]], model_name: str, ttl_seconds: int = 7 * 86400) -> Callable[[str], list[float]]:
    """Wrap an embedding function: the same text with the same model is embedded once (stored as float32 bytes)."""
    def embed_cached(text: str) -> list[float]:
        key = f"{PREFIX}:emb:{_digest(model_name, text)}"
        try:
            raw = redis_client().get(key)
            if raw is not None:
                return array("f", raw).tolist()
        except Exception:  # noqa: BLE001
            pass
        vector = embed(text)
        with contextlib.suppress(Exception):
            redis_client().set(key, array("f", vector).tobytes(), ex=ttl_seconds)
        return vector
    return embed_cached


# ======================================================================= 6. coordination and short-lived secrets in Redis
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


@contextlib.contextmanager
def lock(name: str, ttl_seconds: int = 600) -> Iterator[bool]:
    """Yields True if this worker got the lock (e.g. lock(f"ticket:{id}") so two workers never resume one ticket at once).
    Only the owner releases it; it expires on its own if the worker dies."""
    key, token = f"{PREFIX}:lock:{name}", uuid.uuid4().hex
    got = bool(redis_client().set(key, token, nx=True, ex=ttl_seconds))
    try:
        yield got
    finally:
        if got:
            redis_client().eval(_RELEASE, 1, key, token)


class Secrets:
    """Short-lived sensitive values (identity-check answers, one-time tokens): encrypted with Fernet, expiring, never in checkpoints."""

    def __init__(self, key: str | None = None, ttl_seconds: int = 900) -> None:
        from cryptography.fernet import Fernet

        key = key or os.environ.get("LISA_SECRET_KEY")
        if not key:
            raise RuntimeError("LISA_SECRET_KEY is not set - secrets are never stored unencrypted")
        self.fernet, self.ttl = Fernet(key.encode() if isinstance(key, str) else key), ttl_seconds

    def put(self, name: str, value: dict) -> None:
        redis_client().set(f"{PREFIX}:secret:{name}", self.fernet.encrypt(json.dumps(value).encode()), ex=self.ttl)

    def get(self, name: str) -> dict | None:
        raw = redis_client().get(f"{PREFIX}:secret:{name}")
        return json.loads(self.fernet.decrypt(raw)) if raw else None

    def delete(self, name: str) -> None:
        redis_client().delete(f"{PREFIX}:secret:{name}")


# ======================================================================= 7. episodic memory in Qdrant
class EpisodicMemory:
    """What fixed similar tickets before: one point per ticket whose fix the reporter CONFIRMED. No personal data in the text or payload.
    Search is filtered by CI / category, so a laptop VPN fix is not offered for a phone."""

    def __init__(self, embed: Callable[[str], list[float]], dims: int, collection: str = "lisa_resolutions") -> None:
        from qdrant_client import models

        self.embed, self.collection, self.models, self.client = embed, collection, models, qdrant_client()
        if not self.client.collection_exists(collection):
            self.client.create_collection(collection, vectors_config=models.VectorParams(size=dims, distance=models.Distance.COSINE))
            for field in ("ci", "category"):
                self.client.create_payload_index(collection, field, models.PayloadSchemaType.KEYWORD)

    def record(self, ticket_id: str, problem: str, resolution: str, *, ci: str, category: str, confirmed_fixed: bool, kb: str | None = None) -> bool:
        if not confirmed_fixed:
            return False                                # only fixes the reporter confirmed become memory
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lisa-ticket:{ticket_id}"))   # idempotent: re-recording a ticket overwrites it
        self.client.upsert(self.collection, [self.models.PointStruct(id=point_id, vector=self.embed(problem), payload={
            "ticket_id": ticket_id, "problem": problem, "resolution": resolution, "ci": ci, "category": category, "kb": kb, "recorded_at": time.time()})])
        return True

    def similar(self, problem: str, *, ci: str | None = None, category: str | None = None, limit: int = 3, min_score: float = 0.75) -> list[dict]:
        must = [self.models.FieldCondition(key=k, match=self.models.MatchValue(value=v)) for k, v in (("ci", ci), ("category", category)) if v]
        hits = self.client.query_points(self.collection, query=self.embed(problem), limit=limit, score_threshold=min_score,
                                        query_filter=self.models.Filter(must=must) if must else None).points
        return [{"score": round(h.score, 4), **h.payload} for h in hits]

    def forget_ticket(self, ticket_id: str) -> None:
        self.client.delete(self.collection, points_selector=self.models.PointIdsList(points=[str(uuid.uuid5(uuid.NAMESPACE_URL, f"lisa-ticket:{ticket_id}"))]))


# ======================================================================= wiring
def compile_graph(builder):
    """builder.compile() with all three: checkpoints + store in PostgreSQL, node cache in Redis."""
    return builder.compile(checkpointer=make_checkpointer(), store=make_store(), cache=make_node_cache())

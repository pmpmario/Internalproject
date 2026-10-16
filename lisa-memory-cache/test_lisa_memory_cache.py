"""Runs every part of lisa_memory_cache.py against REAL PostgreSQL, Redis Stack and Qdrant (skipped when they are not reachable).

    LISA_POSTGRES_URL=postgresql://lisa:lisa@localhost:5432/lisa LISA_REDIS_URL=redis://localhost:6379/0 LISA_QDRANT_URL=http://localhost:6333 \
    python -m pytest -q test_lisa_memory_cache.py
"""
import hashlib
import math
import operator
import re
import time
import uuid
from typing import Annotated, TypedDict

import pytest

import lisa_memory_cache as lmc

RUN = uuid.uuid4().hex[:8]                    # unique ids per run, so runs never see each other's data


def _reachable() -> str | None:
    try:
        with lmc.pg_pool().connection() as conn:
            conn.execute("SELECT 1")
        lmc.redis_client().ping()
        lmc.qdrant_client().get_collections()
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return None


pytestmark = pytest.mark.skipif(bool(_reachable()), reason=f"PostgreSQL / Redis / Qdrant not reachable: {_reachable()}")


def toy_embed(text: str, dims: int = 64) -> list[float]:
    """Deterministic bag-of-words embedding, only for tests (no model needed)."""
    vec = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ----------------------------------------------------------------------- 1. checkpoints (PostgreSQL)
class Ticket(TypedDict, total=False):
    reporter: str
    phone: str
    answer: str
    calls: Annotated[int, operator.add]


def _ask_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    def ask(state: Ticket):
        return {"answer": interrupt({"question": "Is 055 123 4567 still your number?"})}

    builder = StateGraph(Ticket)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=checkpointer)


def test_ticket_pauses_and_resumes_through_a_new_checkpointer_and_can_be_deleted():
    from langgraph.types import Command

    cfg = {"configurable": {"thread_id": f"INC-{RUN}-1"}}
    first = _ask_graph(lmc.make_checkpointer())
    first.invoke({"reporter": "ann"}, cfg)
    assert first.get_state(cfg).next == ("ask",)

    second = _ask_graph(lmc.make_checkpointer())            # as if the API restarted / another worker took the reply
    assert second.invoke(Command(resume="yes"), cfg)["answer"] == "yes"
    assert len(list(second.get_state_history(cfg))) >= 3   # history for a trace view

    lmc.make_checkpointer().delete_thread(cfg["configurable"]["thread_id"])   # retention
    assert second.get_state(cfg).values == {}


# ----------------------------------------------------------------------- 2. long-term memory (LangGraph store in PostgreSQL)
def test_reporter_memory_rules():
    mem = lmc.ReporterMemory(lmc.make_store())
    who = f"ann.{RUN}@example.com"
    mem.remember(who, "mobile_phone", "+971500000001", "reporter_corrected", ticket_id="INC1")
    fact = mem.recall(who, "mobile_phone")
    assert fact["value"] == "+971500000001" and fact["source"] == "reporter_corrected"
    assert mem.recall(who, "mobile_phone", max_age_days=0) is None          # too old to offer
    with pytest.raises(ValueError):
        mem.remember(who, "mobile_phone", "+97150", "llm_inferred")          # never store guesses
    assert mem.forget(who) == 1 and mem.recall(who, "mobile_phone") is None


def test_raw_email_cannot_be_a_store_namespace_label():
    """Why ReporterMemory hashes the id: LangGraph store namespace labels may not contain '.'."""
    with pytest.raises(Exception):
        lmc.make_store().put(("reporters", f"ann.{RUN}@example.com"), "x", {"v": 1})


def test_a_node_reads_the_store_through_runtime_and_a_cached_node_runs_once():
    from langgraph.graph import END, START, StateGraph
    from langgraph.runtime import Runtime
    from langgraph.types import CachePolicy

    who = f"bob.{RUN}@example.com"
    lmc.ReporterMemory(lmc.make_store()).remember(who, "mobile_phone", "+971500000002", "reporter_confirmed")

    def recall_phone(state: Ticket, runtime: Runtime):
        fact = lmc.ReporterMemory(runtime.store).recall(state["reporter"], "mobile_phone")
        return {"phone": fact["value"] if fact else ""}

    executions = []

    def expensive(state: Ticket):                          # stands in for retrieval / rerank / an ML call
        executions.append(1)
        return {"calls": 1}

    def build(cache_policy):
        builder = StateGraph(Ticket)
        builder.add_node("recall_phone", recall_phone)
        builder.add_node("expensive", expensive, cache_policy=cache_policy)
        builder.add_edge(START, "recall_phone")
        builder.add_edge("recall_phone", "expensive")
        builder.add_edge("expensive", END)
        return lmc.compile_graph(builder)

    plain = build(None)                                     # baseline: no cache policy -> the node runs every time
    for n in (2, 3):
        plain.invoke({"reporter": who, "calls": 0}, {"configurable": {"thread_id": f"INC-{RUN}-p{n}"}})
    assert len(executions) == 2

    executions.clear()
    app = build(CachePolicy(ttl=120))
    out1 = app.invoke({"reporter": who, "calls": 0}, {"configurable": {"thread_id": f"INC-{RUN}-2"}})
    out2 = app.invoke({"reporter": who, "calls": 0}, {"configurable": {"thread_id": f"INC-{RUN}-3"}})
    assert out1["phone"] == "+971500000002" and out1["calls"] == out2["calls"] == 1
    assert len(executions) == 1                                         # same input on the second ticket: served from Redis
    keys = [k.decode() for k in lmc.redis_client().keys(f"{lmc.PREFIX}:node:*")]
    assert keys, "node cache keys must carry the lisa:node: prefix"


# ----------------------------------------------------------------------- 3. LLM cache (per model, Redis)
def test_llm_cache_is_attached_per_model_not_globally():
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    prompt = f"classify ticket {RUN}"
    cached = GenericFakeChatModel(messages=iter([AIMessage(content="network"), AIMessage(content="SHOULD NOT BE USED")]), cache=lmc.make_llm_cache())
    assert cached.invoke(prompt).content == "network"
    assert cached.invoke(prompt).content == "network"                 # served from Redis, the model was not called again
    uncached = GenericFakeChatModel(messages=iter([AIMessage(content="a"), AIMessage(content="b")]))
    assert [uncached.invoke(prompt).content, uncached.invoke(prompt).content] == ["a", "b"]   # other models unaffected
    assert lmc.redis_client().keys(f"{lmc.PREFIX}:llm:*")


# ----------------------------------------------------------------------- 4. data caches (Redis)
def test_ttl_cache_keep_and_bump():
    calls = []
    ns = f"profile-{RUN}"

    @lmc.ttl_cache(ns, 60, keep=lambda r: r.get("status") == "ok")
    def fetch(ticket_id):
        calls.append(ticket_id)
        return {"status": "ok" if len(calls) > 1 else "error", "n": len(calls)}

    assert fetch("INC1")["status"] == "error"     # errors are not kept
    assert fetch("INC1")["n"] == 2
    assert fetch("INC1")["n"] == 2                # cached
    lmc.bump(ns)                                  # e.g. after LISA wrote to the ticket
    assert fetch("INC1")["n"] == 3


def test_caches_survive_redis_being_down(monkeypatch):
    import redis

    dead = redis.Redis.from_url("redis://localhost:6399/0", socket_timeout=0.2, socket_connect_timeout=0.2)
    monkeypatch.setattr(lmc, "redis_client", lambda: dead)

    @lmc.ttl_cache(f"down-{RUN}", 60)
    def fetch():
        return {"ok": True}

    assert fetch() == {"ok": True}
    assert lmc.cached_embedder(toy_embed, "toy")("vpn down") == toy_embed("vpn down")


def test_embedding_cache():
    calls = []
    embed = lmc.cached_embedder(lambda t: calls.append(t) or toy_embed(t), "toy-model")
    text = f"vpn not connecting {RUN}"
    first, second = embed(text), embed(text)
    assert len(calls) == 1 and all(abs(a - b) < 1e-6 for a, b in zip(first, second))


# ----------------------------------------------------------------------- 5. coordination and secrets (Redis)
def test_lock_is_exclusive_and_only_the_owner_releases_it():
    name = f"ticket:{RUN}"
    with lmc.lock(name) as got:
        assert got
        with lmc.lock(name) as second:
            assert not second
    with lmc.lock(name) as again:
        assert again


def test_secrets_are_encrypted_and_expire():
    from cryptography.fernet import Fernet

    secrets = lmc.Secrets(Fernet.generate_key().decode(), ttl_seconds=60)
    secrets.put(f"reset:{RUN}", {"password": "Tmp-Secret-123"})
    raw = lmc.redis_client().get(f"{lmc.PREFIX}:secret:reset:{RUN}")
    assert b"Tmp-Secret-123" not in raw and 0 < lmc.redis_client().ttl(f"{lmc.PREFIX}:secret:reset:{RUN}") <= 60
    assert secrets.get(f"reset:{RUN}") == {"password": "Tmp-Secret-123"}
    secrets.delete(f"reset:{RUN}")
    assert secrets.get(f"reset:{RUN}") is None


def test_secrets_refuse_to_run_without_a_key(monkeypatch):
    monkeypatch.delenv("LISA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        lmc.Secrets()


# ----------------------------------------------------------------------- 6. episodic memory (Qdrant)
def test_episodic_memory_keeps_confirmed_fixes_and_filters_by_ci():
    mem = lmc.EpisodicMemory(toy_embed, dims=64, collection=f"lisa_resolutions_{RUN}")
    try:
        assert not mem.record("INC10", "vpn fails on laptop", "reinstall client", ci="VPN", category="network", confirmed_fixed=False)
        assert mem.record("INC11", "vpn fails on laptop after update", "sign out and in of the VPN client", ci="VPN", category="network", confirmed_fixed=True)
        assert mem.record("INC11", "vpn fails on laptop after update", "sign out and in of the VPN client", ci="VPN", category="network", confirmed_fixed=True)
        assert mem.record("INC12", "outlook fails on laptop", "repair the Office install", ci="OUTLOOK", category="email", confirmed_fixed=True)
        time.sleep(0.5)
        hits = mem.similar("vpn fails on my laptop", ci="VPN", min_score=0.3)
        assert [h["ticket_id"] for h in hits] == ["INC11"]                          # one point per ticket, only the VPN CI
        assert mem.similar("vpn fails on my laptop", ci="OUTLOOK", min_score=0.9) == []
        mem.forget_ticket("INC11")
        assert mem.similar("vpn fails on my laptop", ci="VPN", min_score=0.3) == []
    finally:
        lmc.qdrant_client().delete_collection(f"lisa_resolutions_{RUN}")


def test_store_with_fact_expiry_sets_a_ttl_and_starts_the_sweeper():
    store = lmc.make_store(fact_ttl_days=180)
    try:
        lmc.ReporterMemory(store).remember(f"cara.{RUN}@example.com", "mobile_phone", "+971500000003", "reporter_provided")
        with lmc.pg_pool().connection() as conn:
            row = conn.execute("SELECT expires_at, ttl_minutes FROM store WHERE key = 'mobile_phone' AND prefix LIKE %s",
                               (f"reporters.{lmc.ReporterMemory.namespace(f'cara.{RUN}@example.com')[1]}%",)).fetchone()
        assert row["expires_at"] is not None and round(row["ttl_minutes"]) == 180 * 24 * 60
    finally:
        store.stop_ttl_sweeper()

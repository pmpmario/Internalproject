# CLAUDE.md — LISA (IT incident triage agent)

> Last verified: 2026-10-16. Update the "What LISA is" and "Infrastructure" sections whenever services or stores change.

## What LISA is
A LangGraph agent that triages IT incidents. It takes a ServiceNow ticket or a chat message, extracts context, classifies the ticket (ML model + LLM), retrieves KB articles and similar past incidents, asks the reporter follow-up questions (human-in-the-loop interrupts), and resolves or escalates. A FastAPI backend serves the dashboard.

## Infrastructure (Docker on dev PCs)
| Service | Holds |
|---|---|
| **PostgreSQL** | LangGraph checkpoints (`PostgresSaver`, one thread per ticket), LangGraph store (`PostgresStore`, per-reporter facts with source and date), app tables (outcomes, audit) |
| **Redis Stack** | Caches (LangGraph node cache, per-model LLM cache, API/embedding caches), locks, short-lived encrypted secrets — every key prefixed `lisa:`; search indexes only in db 0 |
| **Qdrant** | KB articles, past incidents, episodic memory (confirmed fixes only) |

Reference implementation and rules: `lisa-memory-cache/` (README + `lisa_memory_cache.py`). Reuse it — don't build a second cache or memory layer.

## Ground rules
- **Never invent business logic.** If intent is unclear, add `# TODO(paul): clarify — <question>` instead of guessing.
- **Preserve public behavior.** Functions, endpoints and CLIs used elsewhere keep their signature and return contract unless I ask for a change. If you find a bug, flag it — don't silently fix it inside an unrelated change.
- **One concern per change.** Don't mix renames, dead-code removal and new tests in one diff — go module by module.
- **uv only:** `uv add` / `uv sync` / `uv run`. No pip, no poetry, no `requirements.txt` edits.

## LangGraph rules
- **Node names** are verbs describing the decision they make (`route_by_confidence`, `ask_for_contact_number`), never `node_3` or `handle`.
- **State keys and interrupt payloads are contracts.** Paused tickets live in PostgreSQL with the old shape. Renaming or removing a key needs a compatibility plan for in-flight threads — ask first.
- **Nodes re-run from the start on resume.** Anything a node does before `interrupt()` runs again when the reporter answers, so side effects there (ServiceNow writes, notifications) must be idempotent or moved after the interrupt.
- **No secrets in state** — state is checkpointed. Identity-check answers, tokens and passwords go to Redis via `Secrets` (encrypted, TTL).
- **`CachePolicy` only on pure nodes** (retrieval, rerank, ML prediction) — never on nodes that write, call ServiceNow, ask the user or interrupt.
- **Reporter memory:** only facts the reporter gave or a system of record holds, stored with source and date, offered for confirmation — never assumed, never LLM-written free-text "memories".

## LLM rules
- Prompts are **static first, ticket last** (instructions and playbooks before ticket data) so the provider's prompt cache can reuse the prefix.
- Use structured outputs for decisions. Attach the exact LLM cache **per model** on deterministic calls only — never the global `set_llm_cache()`.
- **No semantic cache for ticket answers** — similar wording is not the same ticket.
- Model reasoning / `<think>` text never reaches customer-facing fields (work notes, comments, chat replies).

## Naming and docstrings
- Human-readable names, no tribal abbreviations (`cfg` → `config`, `inc` → `incident`).
- **Keep ServiceNow / external API field names exactly as they are** (e.g. `u_*` fields) — those aren't ours to rename.
- Docstrings: one line explaining **why** the code exists or why this approach, not what it does. Skip them when the name says it. Module docstring: one line on what the file owns and why it's separate.

## Structure
- Group by **service boundary first** (graph pipeline, agents/nodes, API backend, retrieval, ML service, integrations), then by layer within each (routes → services → models → clients).
- Shared code (cache/memory layer, retrieval and reranking clients, settings) lives in one common package — never copy-pasted across services. If the same logic exists in two places, compare both for drift before consolidating.
- Config (URLs, model paths, thresholds) comes from environment variables through one settings module per service — never hard-coded.
- One `README.md` per service (what it does, how it talks to the others) and a root `README.md` that is an index + architecture diagram. Don't duplicate content between them.

## Removing stale code
1. Confirm it's unreferenced across the **whole repo** (cross-service imports are easy to miss).
2. If unsure whether code is dead or not yet wired up, ask — don't delete prototypes that haven't been explicitly abandoned.
3. Delete, don't comment out. Git history is the archive. Remove docs of removed code in the same change.

## Tests (`pytest`, extend the existing suite)
Every test maps to a real behavior or a bug class this project has hit:
- **Classifiers (CI / priority):** accuracy on a committed held-out labeled set (`tests/fixtures/`) stays within a defined band — not just "returns a string".
- **LangGraph routing:** each confidence gate at just-above, just-below, and missing/malformed state.
- **Interrupt / resume:** a paused ticket resumes with the correct state through `PostgresSaver` against a real PostgreSQL (Docker). Mark these as integration tests and skip when the service isn't reachable.
- **Retrieval:** a small golden set (query → expected KB ids) so ranking regressions show up.
- **ServiceNow:** mock at the Table API boundary; test the agreed behavior when a write fails (a failed work-note PATCH must not crash the graph).
- **Memory / cache:** run against real PostgreSQL / Redis / Qdrant like `lisa-memory-cache/test_lisa_memory_cache.py`; prove a cache actually skips work (count executions), not just that it returns a value.
- **Every known past bug gets a regression test.**
- Skip: tests that only assert a mock returned what it was told to, tests whose only assertion is "didn't throw", tests that duplicate a type check.
- Expensive fixtures (model loads, service clients) go in `conftest.py`, session-scoped.

## Documentation
- Update docs only for what changed, in the same change.
- Docs that drift (model versions, accuracy numbers, endpoint lists) carry a one-line "Last verified: <date>" at the top.

## Never (project-specific)
- Never log ticket text or personal data (names, phone numbers, staff IDs, emails) at INFO level or above.
- Never put ticket text in cache keys or metric labels — hash it.
- Never commit `.env`, model weights, Qdrant/Postgres/Redis data folders, or exports of real tickets.

## Definition of done (per module / service)
- [ ] Tests cover the real failure modes of this module, not just the happy path — and they pass (show the output)
- [ ] Dead code removed, cross-checked repo-wide
- [ ] Names are self-explanatory without a comment; one-line "why" docstrings only where needed
- [ ] No duplicated logic across services
- [ ] `uv sync` resolves cleanly, no orphaned dependencies in `pyproject.toml`
- [ ] The service README reflects current reality

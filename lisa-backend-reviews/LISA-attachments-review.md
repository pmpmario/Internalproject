# Review of the attached scripts and ideas against LISA-Backend

Each item is checked on its own merits and **against the actual LISA code**: the hybrid / PROD copy
(`LISA-Backend-playbooks-v2-hybrid[-PROD]`), and v2 where it matters.

| # | Attachment | What it is | Verdict for LISA |
|---|---|---|---|
| 1 | `agent_tool_use_production.ipynb` | Generic tutorial: hardened tool-calling agent loop (Anthropic API, offline `ScriptedLLM`) | **Adopt the patterns, not the code.** LISA already has most of them; four are worth adding (§1.3) |
| 2 | `assess_resolvability_workflow.ipynb` | Template: an LLM decides whether a ticket can be fixed by tools (Opsify), with a plan | **Do not add as-is.** It lets the LLM plan a password reset **without identity verification**, and it duplicates LISA's existing assist / offer logic (§2) |
| 3 | `extract_technical_steps_llm_only` | — | **Not received.** Only five files were attached (the two notebooks, the two `.md` files, and the earlier `TECHSTACK.md`). Please re-attach it |
| 4 | `KB_Retrieval_SelfCorrecting_Loop_Plan.md` | Plan: grade the KB result, rewrite the query, retry once | **Good idea, but the plan needs changes before building.** As written it breaks agent mode, classify-first, the Teams graph and the tests (§4) |
| 5 | `retreivalpredictionchange.md` | Analysis of a wrong CI from v2's retrieval vote (100% vote share from generic wording) | **Already solved in hybrid / PROD.** The vote was removed; the server part is covered by the CMDB override. One gap remains (§5) |
| 6a | Screenshot: cognee | Library: documents, code and chats → a knowledge graph for agent memory | **Not now.** It adds a new store and new LLM costs; the useful part (CI / server relationships) is better taken straight from the ServiceNow CMDB (§6a) |
| 6b | Image: Self-Correcting Memory | Pattern: evidence-grounded claims, versioning, staleness checks, re-verify | **Worth applying, in two small places:** reporter memory and the KB index (§6b) |

---

## 1. `agent_tool_use_production.ipynb`

### 1.1 What it is

A 48-cell notebook implementing the patterns from the DEV article *Building AI Agents with Tool
Use*:
- a tool registry with JSON Schema
- structured tool errors
- argument validation
- per-tool timeouts
- retries with jitter
- token and dollar budgets
- per-tool rate limits
- a fail-closed confirmation gate for destructive tools
- an audit log with redaction
- idempotency keys
- an LLM client protocol

It runs offline against a scripted model. The six built-in tests pass in the saved outputs, and the
live Anthropic cell was skipped.

### 1.2 Quality notes

**Good:**
- An AST calculator instead of `eval`.
- The confirmation gate fails closed; blocked calls are audited too.
- `additionalProperties: false` rejects invented arguments.
- The "when not to use an agent" section.

**Limits, as the notebook itself says:**
- A timed-out tool thread is abandoned, not killed.
- Budgets are checked *after* each model call, so a run can overshoot by one call.
- Idempotency and rate limits are in-memory only.

**Pricing table:** the default model `claude-sonnet-5-5` is not in `PRICING_PER_MTOK`, so the cost
falls back to placeholder rates (the saved output shows the warning). The listed rates are the
article's examples.

**Format:** it is written for the Anthropic Messages format. LISA uses LangChain +
`AzureChatOpenAI` with `bind_tools`, so the loop cannot be dropped in.

### 1.3 Compared with LISA's agent mode (`incident_graph/agentic/`)

| Pattern | LISA today | Gap |
|---|---|---|
| Max steps | `AGENT_MAX_STEPS=24` per round, then escalate | — |
| One tool per step, no parallel calls | `bind_tools(..., parallel_tool_calls=False)`, one tool per visit | — |
| Allow-list / consent for side effects | `assist_tool`: per-task allow-list, explicit yes, call limits (3 lookups, 2 starts, 1 submit) | — |
| Model can only suggest a tool | Yes (`offer_tool`, `agent_gate` checks the decision against the tool log) | — |
| Structured tool results | `Outcome` + `result_text` | Errors are not uniform `{error, message, hint}`; worth standardising |
| **Token / dollar budget per ticket** | Tokens are recorded (`token_usage`), **never enforced** | **Add:** stop and escalate when a ticket passes N tokens or $X |
| **Argument validation against the schema** | Tools-server params are filtered to the declared ones, enum / pattern checked for needed details | **Add:** validate the agent's own tool arguments (`agentic/tools.py`) with `jsonschema` before running |
| **Per-call timeouts** | ServiceNow 10 s × 3, tools server 40 s, Azure LLM 60 s, ML service 5 s | Graph turns have no overall deadline (see hosting doc S1) |
| **Audit log incl. blocked calls** | `assist_events` (outcomes), `token_usage`, checkpoints | **Add:** one audit record per tool call: allowed / blocked / why, with redaction |
| **Idempotency for writes** | ServiceNow writes (`append_work_note`, `update_ci`, resolve) have **no idempotency key**; LangGraph re-runs a node from the top on resume, and the dashboard retries | **Add:** a key per (thread, node, purpose) stored in the DB; skip a write already done |

The notebook's "when not to use an agent" test fits LISA directly. Most of a ticket is a fixed
pipeline (fetch → context → triage → classify → KB → draft). The staged graph is that pipeline, and
agent mode only earns its cost for reading free-form replies. Keep both modes and measure them
(turns, tokens, outcome) before choosing one for production.

---

## 2. `assess_resolvability_workflow.ipynb`

### 2.1 What it is

- A prompt builder plus a node that asks an LLM: *can this ticket be fully resolved by tool calls?*
- The answer is JSON: `can_resolve_automatically`, `requires_human`, `reason`, `resolution_plan`
  (ordered tool calls), `risk_notes`, `suggested_assignment_group`.
- Tools are Opsify identity APIs: `search_users`, `get_user_status`, `reset_password`.
- There is an optional guard for confidence < 0.6.

### 2.2 Problems found

| Problem | Where | Why it matters |
|---|---|---|
| **It never calls a real model.** `DummyLLM` returns a hard-coded answer | Cell 108667f6 | The notebook proves only the plumbing, not the decision quality |
| **Password reset without identity verification.** The plan goes `search_users` → `get_user_status` → `reset_password`; `reset_password` is marked low-risk, `requires_approval: False`, `reversible: False` | Sample tools + DummyLLM plan | Anyone who opens a ticket in someone else's name gets that person's password reset. LISA's flow requires the staff ID, consent and identity questions, with limits and secrets kept out of state |
| **The LLM decides and plans side effects** | System prompt: "propose an ordered list of tools" | LISA's rule is that the model may only suggest and the code decides (`assist_tool` allow-list, consent, limits). Letting the plan drive execution undoes that |
| **Inconsistent thresholds.** The prompt requires confidence ≥ 0.7, the guard skips the LLM only below 0.6 | Cells 5b8fcc62 / 30162332 | Tickets at 0.6–0.7 cost an LLM call that is told to refuse |
| **Fragile JSON parsing.** It strips code fences and runs `json.loads` with no validation or retry | `assess_resolvability_node` | LISA's `make_structured_llm` (schema-constrained output) avoids this |
| **Wrong node shape for LISA.** `(state, llm)` instead of `(state, config)`; it returns a full state copy; the keys `incident`, `candidate_tools`, `kb_results` do not exist in LISA (`incident_description`, `tools_client.tool_catalog()`, `search_results`) | Whole notebook | Needs rewriting to fit |
| The user's email is put in the prompt | `incident_text` | Unnecessary PII sent to the LLM |
| Confidence scale | `classification.confidence` 0.94 | LISA's `ml_classification` stores **percent** (94.0); the stored `ci_confidence` is a fraction. Mixing them silently breaks thresholds |

### 2.3 What LISA already does instead

| The notebook's step | LISA |
|---|---|
| Decide whether a tool can fix it | Triage's assist reading (`wants_password_reset`, `account_action`, `suggested_tool`) + `assist_nodes.detect()`; after the draft, `offer_tool` checks the tool catalog |
| Tool metadata (risk, approval, reversible) | Tools server `_meta`: `when`, `effect`, `consent`, `facts` (`tools_client.tool_catalog()`) |
| Ordered plan | A scripted, code-owned flow: lookup → status → consent → identity questions → submit |
| Human needed | P1/P2 → human review; failures → escalation |

### 2.4 What is worth taking

1. **Opsify as the real backend for LISA-tools-mcp.** `.env.example` already uses
   `opsify_integration_user`. The notebook's tools map onto the tools server's:

   | Opsify (notebook) | LISA-tools-mcp | Note |
   |---|---|---|
   | `search_users` | `lookup_staff_name` | |
   | `get_user_status` | `check_account_status` | |
   | `reset_password` | `start_password_reset` + `submit_password_reset_answers` | **Keep the identity step** |

   That replaces the mock directory (hosting doc M8).
2. **A "resolvability" record for reporting**, built **by code** from what already exists (triage,
   tool catalog, assist outcome, priority). Not an LLM verdict. It gives the dashboard an "automated
   vs needed a human, and why" view.
3. **Precondition checks as code:** "account not disabled for HR reason" and "no active security
   incident on user" belong in the tools server's checks before a reset.

---

## 3. `extract_technical_steps_llm_only`

**Not received.** Please re-attach it and I will review it the same way.

---

## 4. `KB_Retrieval_SelfCorrecting_Loop_Plan.md`

### 4.1 The idea

After the KB search:
1. ask an LLM whether the top result really addresses the incident;
2. if not, rewrite the query once and search again;
3. stop after one retry (`MAX_KB_RETRY_ROUNDS = 1`).

It copies the bounded-loop shape of `assess_completeness` ↔ `ask_followup_question`.

### 4.2 Checked against the code

| Plan statement | In the code | Status |
|---|---|---|
| `assess_completeness` uses `MAX_FOLLOWUP_ROUNDS = 1` and `Command(goto=...)` | `completeness_agent.py:22`, routing by `Command` | Correct |
| `make_structured_llm` at `workflows/common.py:77-89` | Defined at line 78 | Correct |
| `find_similar_incidents` is called inside `classify_incident`, not a node | Yes | Correct |
| Target repo `C:\PROD\LISA-Backend` | The plan predates the hybrid / PROD copies | Re-target to the copy going to production |
| **"Remove the old `search_kb` top-level function"** | `search_kb` is also imported by `agentic/tools.py:254` (agent mode), `classify_first.py:56` (early look), `servicenow_workflow_teams.py`, and patched in `tests/test_agentic.py:117` | **Breaks** agent mode, classify-first, the Teams graph and tests |
| **Rename the `search_kb` node / replace its trace label** | `trace.py` `NODE_LABELS` + `_detail_for("search_kb")`; saved checkpoints of earlier incidents contain `search_kb` steps | Old traces would lose their label; **add** labels, do not replace |
| An LLM grade of relevance | The reranker already scores relevance 0–1 with a 0.7 floor. With `RERANKER=azure` that score **is already an LLM judgement** | A second LLM grade duplicates work and adds a call to every ticket |

### 4.3 The safer version

1. **Keep the `search_kb` node and function name.** Put the loop *inside* `search_kb`: search →
   rerank → check → (rewrite → search → rerank) → return. Nothing else changes: agent mode,
   classify-first, the Teams graph, trace labels and tests all keep working, and old traces stay
   readable.
2. **Trigger the retry from the scores, not a new LLM grade.** Retry only when:
   - there are no articles above 0.7, or
   - the top article is below 0.85 (the bar the code already trusts:
     `KB_CONFIRMED_CI_MIN_RELEVANCE`, the lockout / Chrome 85% rules).

   When the first result is good this costs nothing, and the rewrite call happens only when needed.
3. **Make the rewrite smarter than wording.** The retry can also drop the CI scope (a wrong CI is a
   common cause of weak KB hits) and use the follow-up answers. One `make_structured_llm` call
   returns `{rewritten_query}`.
4. Record `kb_retry_round` and `kb_rewritten_query` in state, as the plan says. Record the event in
   the trace detail of `search_kb` ("retried with: …").
5. **Agent mode:** leave it alone. The agent can already call `search_kb` again with its own query.
6. **Tests:**
   - offline unit tests with a fake KB service: a good first hit makes 0 extra calls; a weak hit
     makes exactly 1 rewrite and 1 extra search; the retry stops at the limit
   - an A/B on saved incidents for how often the retry fires and whether the final top article
     improved
   - the plan's live checks remain useful

---

## 5. `retreivalpredictionchange.md`

### 5.1 What it says

In **v2**:
- the SVM said S.A.MS-OUTLOOK (44.7%);
- the retrieval branch said S.A.RESCONNECT (100%) because all 5 reranked neighbours were ResConnect
  tickets with similar generic wording;
- reconciliation trusted retrieval.

The note correctly identifies the vote share as a measure of agreement, not correctness. It also
lists three chat links (ChatGPT, Claude, Perplexity), which are not part of the analysis.

### 5.2 Status in hybrid / PROD

| Point | Status |
|---|---|
| Retrieval vote share as CI confidence | **Gone.** `build_retrieval_prediction` and reconciliation were removed; the CI comes from model A (a calibrated SVM) via the triage ML service, blanked below 0.7 |
| Server tickets need a deterministic CMDB check | **Built.** `cmdb_lookup.find_server_ci` + `apply_ci_override` (server first). **But** `CMDB_LOOKUP` defaults to `mock` (a local list), and the server-name regex only knows `DXB…`, `DOWIAP…` and "server / host / vm NAME" cues |
| Similar-incidents panel from generic wording | Mitigated: candidates must be ≥ 0.7 relevance **and** share the final CI |

### 5.3 Remaining gap

Model A predicts **only 10 CIs**. A generic "cannot access server X" ticket where the regex misses
the name is still forced into one of those 10 apps, unless the model's confidence is below 0.7.

**Recommendation:**
- set `CMDB_LOOKUP=live` in production
- widen the server-name detection using the real CMDB naming conventions
- add a "not one of the 10 known CIs" signal, for example a server or infrastructure category from
  triage that blocks app CIs

---

## 6a. cognee (screenshot)

### What it is

An open-source Python library that builds a **knowledge graph + vector index** from documents,
code and chats, so agents can `remember` and `recall` facts.

The slide says it runs locally with no API key. Building the graph still needs an LLM for
extraction, so "local" means pointing it at a local model.

### Fit with LISA

| Possible use | Better option already available |
|---|---|
| CI ↔ server ↔ application relationships (the root cause in §5) | **The ServiceNow CMDB is already a graph** (`cmdb_ci`, `cmdb_rel_ci`). Query it live (`CMDB_LOOKUP=live`, extended to relationships) instead of building a second graph |
| KB ↔ incident ↔ CI links | Already in the Qdrant payloads (`configuration_item`) and the CI-scoped KB search |
| Reporter memory | `reporter_profiles` / `reporter_incident_history` (see §6b) |

**Cost of adding it:**
- a new store, plus graph extraction LLM calls on every ingest
- PII copied into another system
- one more thing to host, back up and secure

**Recommendation:** not for the production path now. Revisit it only if you need relationships the
CMDB does not hold (for example "this KB fixed these incident patterns").

## 6b. Self-Correcting Memory (diagram)

### The pattern

Memory stores **claims with evidence and a version**. A cheap deterministic check marks claims
**stale** when the source changes. Stale claims stay flagged, not deleted, until re-verified, and
the agent re-verifies in the normal flow of work.

The evaluation numbers on the slide (80 → 9 stale claims, 15 → 0 hallucinated) are the author's; I
cannot verify them.

### Where it fits LISA

1. **Reporter memory** (`db.py`: `reporter_profiles`, `reporter_incident_history`).
   - **Today:** `mobile_phone`, `location`, `applications_used` and `most_frequent_ci` are
     overwritten as "latest known value", with no source and no age. The prompts render them as
     *known* context (`completeness_agent._render_known_context` / `_render_reporter_history`).
   - **Apply:** store each fact with its **source** (ServiceNow profile / reporter answer / ticket
     text) and **timestamp**.
   - Treat facts older than N days, or not from ServiceNow, as **"to confirm"**, not known. LISA
     already re-fetches the ServiceNow profile on every ticket, so the evidence source exists.
   - Never let a stale fact skip a question.
2. **KB index** (`resolution_engine/artifacts/kb_retrieval`).
   - **Today:** the index is a frozen artifact with no refresh pipeline. An article can be edited or
     retired in ServiceNow while LISA keeps citing the old text.
   - **Apply:** store each article's `sys_updated_on` / version in the payload. A cheap check at
     answer time (or nightly) flags articles whose ServiceNow version changed as **stale**: don't
     cite them until re-indexed.
   - This is hosting-doc item S5 (index refresh) with the staleness flag added.
3. **Not needed:** a general "belief formation" layer. LISA's ticket state is per incident and
   rebuilt from ServiceNow each run.

---

## 7. Suggested order

| Priority | Action | From |
|---|---|---|
| 1 | Idempotency keys for ServiceNow writes (no duplicate notes or resolves on retries / resumes) | §1.3 |
| 2 | Token / dollar budget per ticket, enforced, escalating on breach | §1.3 |
| 3 | KB retry loop **inside** `search_kb`, triggered by scores | §4.3 |
| 4 | `CMDB_LOOKUP=live`, wider server-name detection | §5.3 |
| 5 | Evidence + age on reporter memory; staleness flag on KB articles | §6b |
| 6 | Opsify behind LISA-tools-mcp, identity verification kept | §2.4 |
| 7 | Code-built "resolvability" record for the dashboard | §2.4 |
| — | Not now: cognee; the LLM-planned `assess_resolvability` node | §6a, §2 |

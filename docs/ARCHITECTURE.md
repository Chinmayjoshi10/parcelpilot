# ParcelPilot — Architecture Note

> Agentic support and operations infrastructure. Cited, verifiable reasoning
> over heterogeneous documents and operational data, with tenancy enforced by
> the database.

Both user contexts are implemented: a **customer portal** scoped to one account,
and an **internal operations console** with tenant-wide visibility, proactive
issue detection, and an approval queue.

---

## 1. Agent and tool design

A bounded loop — **route → execute → synthesise → validate** — written directly
rather than on an agent framework, so the security hooks live *in* the loop.

```
route      (LLM, structured output)  chooses which tools to run
execute    (deterministic)           SQL templates + policy rules + retrieval
synthesise (LLM, structured output)  returns claims + verbatim quotes only
validate   (deterministic)           verifies every citation, or refuses
```

Bounds: 8 steps, 24k tokens, 90 seconds wall clock, all enforced before each
call rather than discovered after it.

**The model chooses tools; it never computes outcomes.** Routing is a genuine
model decision — that is what makes this agentic rather than a fixed pipeline —
but a fee, a threshold or an eligibility verdict comes from
`agentcore/policy/rules.py`. The router is explicitly instructed to prefer the
policy tool and never to do arithmetic itself.

### Seven tools

The assessment asks for three distinct tools. The first five are the core; the
last two exist because the internal console asks a different *shape* of question.

| Tool | Kind | Notes |
|---|---|---|
| `doc_search` | retrieval | Hybrid lexical + dense over policies, SOPs, product docs and the caller's own agreement |
| `data_query` | structured lookup | Parameterised templates only; no model-generated SQL |
| `policy_decide` | calculation | Deterministic rules returning a verdict **and its operative clause** |
| `ticket_history` | retrieval | Past resolutions, permanently `context_only` |
| `prepare_action` | **state change** | Writes a ledger row; executes nothing. A human confirms |
| `cohort_query` | structured lookup | Named templates for a question about a **set** rather than a record |
| `issue_scan` | calculation | The proactive detectors, reachable from chat; each finding carries its threshold clause |

Every tool the engine dispatches is declared in `config.yaml` with whether it is
tenant-scoped and whether it requires confirmation — and
`tests/test_fast_router.py::TestToolsAreDeclared` enforces that, because the two
cohort tools originally shipped dispatching in code and absent from the config.
`config.yaml` is the version-controlled answer to "what could this agent do in
August", so a capability with no declaration there is a capability with no review.

**Why the last two were needed.** Both of the internal console's headline
questions — *"show me all open P1 tickets across accounts"* and *"is TKT-501 an
SLA breach for Northstar?"* — refused with `low_confidence`, while the proactive
dashboard answered both correctly from the same database one tab away. The router
could only plan `doc_search`, which found nothing citable, because *"how many
tickets are past their target"* is not a sentence in any PDF. The logic was
written and tested; it was simply unreachable from chat.

`issue_scan` integrates without a validation exemption because every `Issue`
already carried the clause defining the threshold it applied. A document says
what the threshold *is*; a finding says which records crossed it. Together they
are a citable answer — the same shape as `policy_decide`.

Row-level security is what lets one cohort template serve both audiences: staff
see the tenant, a customer sees their own account, and neither needs a different
query. The scoping is on the connection, not a filter these tools remember to
apply.

`prepare_action` is the agent's only route to the real world, and it is a
proposal. The model chooses the action *type* and writes the summary a human
reads; the **payload is assembled from records the run actually retrieved**, so
the model cannot invent an order id, an amount or an account. An amount always
comes from a deterministic decision — with no eligible decision in the plan,
nothing is prepared, because there would be no figure that was not invented.

If the run then fails to produce a validated answer, the proposal is
**withdrawn**. An approval whose justification failed validation would present
an approver with a summary and nothing to check it against, which invites
approval on trust.

### Multi-step requests

The flagship example exercises the whole chain:

> *"Can Northstar cancel ORD-1001 without a cancellation fee? Explain why."*

`data_query` (order → account) → `policy_decide` (contract override resolved via
`accounts.contract_file`, elapsed time computed) → `doc_search` (the clauses) →
conflict detection (v3 supersedes v2) → synthesis → citation validation.

The answer cites the contract clause, *then* the default SOP it overrides, *then*
the precedence rule — showing the conflict rather than resolving it silently.

---

## 2. Access control and data privacy

**Enforced in the data layer, by PostgreSQL row-level security.** The runtime
role (`parcelpilot_app`) is neither a superuser nor a table owner, because
Postgres exempts both from RLS.

```sql
CREATE POLICY orders_scope ON orders
    USING (tenant_id = app_tenant() AND app_can_see_account(account_id));
```

Scope arrives from a verified JWT, is threaded to the database as transaction-
local settings, and **no layer below the API edge can construct or widen a
`Principal`**.

**Fail-closed.** The RLS helpers return `NULL` when scope is unset, and
`tenant_id = NULL` matches nothing. A query that forgets its filter returns zero
rows, not everyone's data.

**Rejected alternative.** Injecting `WHERE account_id = ?` into SQL templates via
a placeholder. That is the standard approach and it is a latent breach: one
template authored without the placeholder leaks, and it is found by a customer
rather than a test.

**How it is proven.** `tests/test_tenancy.py` issues `SELECT * FROM orders` with
**no WHERE clause at all** and passes because the database refuses. It also
asserts the runtime role is not a superuser, owns none of the tables it queries,
and that every table carrying `tenant_id` has RLS enabled — a check that caught
a real omission on `tenants`, which would have allowed tenant enumeration.

Roles: `customer` (one account), `support_agent` (tenant-wide read, may propose),
`operations_admin` (may approve). Writes are further restricted by grant: the
runtime role has `UPDATE` on exactly two tables and none on the corpus.

---

## 3. Document and structured-data handling

### Documents

PyMuPDF → normalised text → **section-aligned chunks**, split on numbered
headings rather than a fixed window. The corpus is numbered clauses and every
answer cites exactly one, so a fixed window would straddle two rules and a
citation could not say which it relied on.

Classification reads what each document **declares about itself** —
`Status: CURRENT`, `Status: DEPRECATED`, `Supersedes:`, `Account:` — never the
filename. Anything unresolved falls back to `context_only`, so an unclassified
document cannot silently acquire authority.

Contract ownership is **cross-checked**: the PDF's `Account:` line against
`accounts.contract_file`. Disagreement aborts ingestion, because getting it wrong
in one direction leaks a contract and in the other silently drops the override.

### Structured data

Explicit typed columns with a **declared column mapping**, not runtime schema
inference. The rule engine reads `orders.booked_at` by name and does arithmetic
on it; a schema that can change shape cannot support a rule that must answer the
same way twice. Unmapped columns are preserved in `raw_json`, and unmapped
*sheets* are reported with row counts rather than skipped silently.

Timestamps are interpreted in the tenant's civil timezone (`Asia/Kolkata`), not
the server's locale — every cancellation window and SLA countdown is wall-clock
sensitive, and a naive UTC parse would shift every fee decision by 5h30m.

### Retrieval

Hybrid: Postgres `tsvector` (GIN) + dense cosine over `float4[]`, fused by
**reciprocal rank fusion**. RRF is rank-based, so two scorers with unrelated
scales combine without inventing a normalisation.

Lexical carries more weight than expected here — the corpus is full of exact
tokens (`ORD-1001`, "cancellation fee", clause numbers) where a paraphrase-tuned
embedding is unhelpful. `EMBEDDING_BACKEND=none` therefore degrades to
lexical-only and remains usable; that mode is tested.

### Immutable index versions

Ingestion builds into a `building` version and flips it `active` in one
transaction. The server pins the active version and never mutates it. So
ingestion is a separate command, N replicas cannot race, readiness means "an
index is pinned", rollback is a pointer flip, and every past answer is
reproducible because the run records which index served it.

---

## 4. Source reliability and conflict handling

Two **orthogonal** properties, deliberately never blended into one score:

| | Meaning | Applied |
|---|---|---|
| **eligibility** | May this source ground a claim at all? | Hard filter |
| **authority** | Which of two valid sources wins? | Tie-breaker at conflict resolution |

```
customer_agreement  100  groundable      (account-scoped)
policy_current       90  groundable
sop_current          85  groundable
product_guide        80  groundable
policy_deprecated     0  conflict_only   ← retrievable, never citable
ticket_resolution    10  context_only    ← may be WRONG
```

**Why not one score.** The obvious `relevance*0.7 + authority*0.2 + freshness*0.1`
looks principled and is broken: authority 0 only *penalises* the deprecated
policy by 0.2, so a strongly-matching v2 chunk still outranks a weaker v3 one.
"Never cite this" must be a filter, because a weight can always be outvoted by a
good match.

The gate is applied **before** truncation to `final_k`, so a run of gated chunks
cannot crowd out the citable ones — the same failure by another route.

**Conflicts are surfaced, not silently resolved.** `policy_family` makes v2-vs-v3
a join rather than filename parsing, so the engine can state that v3 governs and
cite both. A customer's agreement overriding general policy is reported with both
documents and an explanation, because an answer that shows its conflict is
trusted more than one that hides it.

**Historical resolutions are permanently `context_only`.** TKT-450 quotes a fee
this account does not owe; TKT-451 reports a defect threshold as a plan limit.
Neither can ground a claim, and the operations dashboard actively flags both as
contradicting current policy.

**Uncertainty.** `INDETERMINATE` is a first-class verdict and the only one
allowed to omit a citation. Missing timestamps, unattributed fault, or incoherent
data (a cancellation predating its booking) all produce it plus an escalation
offer — the SOP says so explicitly.

---

## 5. Trust: citation validation

Every generated citation is checked against the chunks the run **actually
retrieved**:

1. **Existence** — a real clause the model was never shown is still fabricated
   reasoning.
2. **Eligibility** — the gate again, at the last possible moment. The deprecated
   chunk is in the prompt as conflict context, so the model *can* quote it
   verbatim, and the citation is still rejected.
3. **Verbatim span** — character-for-character. Whitespace-insensitive, because
   PDFs wrap mid-sentence; word-exact, so "30 minutes" never matches "60".
4. **Support** — a claim whose citations were all rejected is dropped, never kept
   as an uncited assertion.

Failure → one regeneration → honest refusal. `document_id` is taken from the
resolved chunk rather than the model, so a real quote cannot be misattributed to
the wrong contract. The answer schema has **no prose field**: displayed text is
assembled from surviving claims, so citation markers can never decorate text
nobody checked.

**Policy parameters are validated too.** All 22 carry the verbatim clause they
came from, re-located in the live corpus on every `policy validate`. Edit the SOP
and CI fails, rather than the engine applying a stale rule for months.

---

## 6. Confirmation before actions

Two phases. `prepare` records the effect and returns an `action_id`; `confirm`
takes **only that id**.

| Property | Mechanism |
|---|---|
| Untamperable | The client never holds the payload; it was frozen server-side |
| Exactly-once | `UPDATE … WHERE status = 'pending'` — the loser of a race updates zero rows |
| Re-authorised | Role checked at prepare **and** at confirm; permissions change |
| No expiry gap | `AND expires_at > now()` sits in the statement that executes |

`payload_sha256` is verified before executing: the client cannot tamper, so a
mismatch means the row itself was altered — refuse. Preparing is cheap and
reversible; committing is neither, so approval is a narrower privilege than
proposal.

Effects land in separate tables (`service_credits`, `follow_ups`) carrying the
originating `action_id`, because an approval never executed and an execution
never approved are different incidents that one table could not distinguish.

---

## 7. Observability

Every step is appended to a durable run log and committed as it happens, so the
SSE transport is a *reader* over that table with a cursor. A dropped connection
resumes from `Last-Event-ID`; the reasoning trace is replayable for audit; and
proxy idle timeouts stop being a design constraint.

`retrieval_candidates` records what was **considered**, not only what was cited —
without it a retrieval miss is invisible afterwards, and "why didn't it find the
clause" is the most common real question about a wrong answer.

The audit log is append-only **by permission**: `UPDATE`/`DELETE` are revoked
from the runtime role, so no code path can rewrite history.

---

## 8. Major technical trade-offs

| Decision | Chosen | Alternative | Rationale |
|---|---|---|---|
| Tenancy | PostgreSQL RLS | Filter injection in templates | Forgetting fails closed; the leak becomes an outage |
| Storage | One PostgreSQL | FAISS + SQLite | In-process state means one server forever: each worker its own index and its own approval queue — a correctness bug at N=2 |
| Vectors | `float4[]` + exact cosine | pgvector | Not installed on the target host, not load-bearing at 19 chunks, and isolated behind one query |
| Fusion | RRF (rank-based) | Weighted score blend | No normalisation to invent between unrelated scales |
| Trust model | Gate + separate tie-breaker | Single blended score | A weight can be outvoted by a good match; a filter cannot |
| Money | Deterministic rules | LLM reasoning | The dataset punishes exactly this (TKT-450) |
| SQL | Template registry | Text-to-SQL | No path from model output to the database |
| Structured schema | Declared mapping | Runtime inference | A rule that must answer identically twice needs stable column names |
| Chunking | Section-aligned | Fixed window | A citation must land on one clause |
| Orchestration | ~500-line loop | LangChain / LlamaIndex | Security hooks in the loop; full control of the failure path |
| Ingestion | Separate versioned job | Startup ETL | Startup ETL blocks readiness, races replicas, and cannot roll back |
| Provider | Vertex AI (Gemini 2.5) | Gemini API | Enterprise surface: regional residency, VPC-SC, CMEK, IAM instead of a secret, Cloud Logging audit |
| Eval | Offline/online split | One suite | Deterministic cases gate every commit; a suite needing an API key to test tenancy gets disabled |

---

## 9. Where it is weak

- Dense retrieval is a sequential scan — fine to a few thousand chunks, needs
  pgvector past ~10k.
- Ingestion re-embeds everything each run and holds a sheet in memory; needs a
  content-hash cache and `COPY` streaming for large corpora.
- "Business hours" in one agreement is approximated as wall-clock, and **labelled
  as such** everywhere it surfaces.
- Login issues tokens for known accounts without an identity provider. Token
  *verification* — signature, expiry, algorithm pinning, claim validation — is
  real.
- One tenant is provisioned; `tenant_id` is on every row but `config.yaml` is
  global.

The policy engine will never be domain-agnostic, and should not be. What
generalises is the trust machinery: citation validation, the eligibility gate,
tenancy, the action ledger, the audit trail.

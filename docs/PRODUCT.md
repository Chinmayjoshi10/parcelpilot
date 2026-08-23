# ParcelPilot — Product Note

---

## 1. Which additional client problem, and how it was addressed

**Both.** They are the same problem seen from two directions: a support team
cannot act on what it cannot trust, and cannot trust what it cannot check.

### Problem 2 — Trust and Reliability (the primary focus)

The whole architecture is an answer to this, so rather than restate it: five
mechanisms, each enforced structurally rather than by instruction.

| Mechanism | What it prevents |
|---|---|
| **Deterministic policy engine** | The model computing a fee. TKT-450 is a human making exactly the mistake an LLM makes — reasoning fluently from the general rule while missing the specific contract |
| **Eligibility as a gate** | A superseded policy being cited because it matched well. Authority is a tie-breaker, never a weight in a relevance score |
| **Verbatim citation validation** | Fabricated sources, misattributed quotes, invented numbers. Failure produces a refusal, not a warning |
| **Policy-pack drift detection** | A rule quietly disagreeing with the policy it cites. Every parameter's quote is re-located in the corpus, in CI |
| **RLS tenancy** | A leak surviving one forgotten `WHERE` clause |

Two behaviours worth naming as *product* decisions rather than technical ones:

**Refusing is a feature, and it is designed to feel like one.** The UI renders a
declined answer as a legitimate outcome with a route to a human, not as an error
state. If refusals looked like failures, every incentive would push the system
toward guessing — which is the exact failure this product exists to prevent.

**Conflicts are surfaced, not resolved silently.** When Northstar's agreement
overrides the SOP, the answer cites the contract, *then* the rule it displaces,
*then* the precedence clause. The conclusion would be identical if it cited only
the contract; the answer would be worse. Users trust a system that shows its
reasoning about disagreement more than one that quietly picks a winner.

### Problem 1 — Proactive Issue Detection

An internal console with six deterministic detectors. Every row **cites the
clause that defines the threshold it breached**, because a breach warning without
a citation is an opinion and nobody acts on it.

| Detector | What it surfaces |
|---|---|
| `sla_breach` | Open tickets past their **contracted** first-response target, resolved per account — Northstar's tickets are measured against their 15 minutes, not the 30-minute default |
| `credit_eligible` | Money owed right now, from the same rule engine the chat uses, so dashboard and chat cannot disagree |
| `pickup_overdue` | Leading indicator: BOOKED past its window. Deliberately separate from credit eligibility, because fault may not yet be attributed and the SOP forbids promising a credit when it is unknown |
| `recurring_issue` | The same failure across accounts, correlated with a known issue. Token-overlap clustering, so "why were these grouped" has a literal answer |
| `stale_answer` | Historical resolutions that contradict current policy — this is what catches TKT-450 and TKT-451 |
| `unapproved_action` | Approvals sitting in the queue, and how long they have left |

On the supplied data it finds **8 issues**: 3 SLA breaches, 1 credit owed
(INR 300, LumenWorks), 1 overdue pickup, 1 recurring bulk-upload cluster
correlated with KI-208, and both stale-answer contradictions.

Each row with an obvious next step carries a **suggested action** that an
operator can propose into the approval queue — so detection connects to action
without ever bypassing the human.

---

## 2. What I would build next, in priority order

**1. pgvector and an embedding cache.** The scaling ceiling. Dense retrieval is
currently a sequential scan — fine at a few thousand chunks, unusable at a
hundred thousand — and every ingest re-embeds everything. Both are contained
changes behind existing interfaces. This is first because it is the only item
that blocks the product working *at all* on a real corpus.

**2. Human-in-the-loop correction that becomes policy.** When a support manager
overrides an answer, that correction should become a reviewed policy parameter
rather than a note nobody reads. The mechanism already exists: every parameter
must cite a clause, so a correction enters the same review path as any other
rule. This is the feature that makes the system improve from use instead of
drifting from it.

**3. A per-tenant mapping registry.** Move `COLUMN_MAPPINGS` out of Python into
per-tenant YAML, plus an `ingest inspect` command that proposes a mapping from a
new workbook's headers for a human to correct. That is how you get "upload any
company's spreadsheets" without giving up the determinism the rule engine needs.
Roughly a week to go from this corpus to any company's tabular estate.

**4. Freshness gating for live integrations.** A CRM sync makes records stale in
a way documents are not. Store `synced_at`, surface it, and refuse when a field
is older than its SLA — the same shape as the eligibility gate. A six-hour-old
answer to "is this ticket still open?" is worse than a refusal.

**5. Guarded exploratory SQL.** I rejected text-to-SQL because injection could
leak across accounts — but that risk existed *because tenancy lived in
application code*. With RLS, a read-only role and statement timeouts, guarded SQL
becomes defensible for **exploration** ("how many late pickups by carrier last
quarter"), while decisions stay with the rule engine. The answer states which
path produced it.

**6. Automated SOP drafting.** When the detector sees five similar escalations,
draft a candidate SOP for human review. Cheap given the existing machinery, and
it turns recurring pain into documentation instead of tribal knowledge.

**7. Real authentication and per-tenant cost controls.** An identity provider,
rate limits, and spend caps per tenant. Necessary for production, but it blocks
nothing about whether the product is *right*.

---

## 3. What was intentionally left out

- **Text-to-SQL** — no path from model output to the database. Revisitable at
  scale for exploration only (see above), never for decisions.
- **A universal spreadsheet loader.** Schema inference sounds like the flexible
  choice and is the wrong one: the rule engine reads `booked_at` by name, and an
  inferred TEXT column where a timestamp was expected does not error — it makes
  every cancellation-window calculation quietly wrong. The mapping is declared,
  and now *reports* what it did not consume.
- **Fine-tuning.** Retrieval plus deterministic tools gives higher precision and
  zero retraining latency when a policy changes. A fine-tuned model would have to
  be retrained every time the SOP does.
- **Conversation memory.** Threaded through the schema — every turn shares one
  `conversation_id` and the runs are linked in the database — but nothing from an
  earlier turn reaches the agent, so a pronoun follow-up starts over.

  This is the one omission worth explaining rather than listing, because the
  reason is specific. The router decides both which tools run *and* whether a
  state change gets staged. Carry entities across turns naively and "cancel it"
  resolves `it` from conversation history into the action gate — a record the
  current turn never looked up, never verified through the scoped connection, and
  that the invisible-record guard therefore never checks. The right design
  carries an entity as a *candidate* that still has to survive the scoped lookup
  like any other id. That is an afternoon of work and an afternoon of testing,
  and shipping the first half alone would have put an untested path into a
  money-moving tool to look more complete.
- **A business-hours calendar.** One agreement quotes business hours; I
  approximate wall-clock and **label it** everywhere it surfaces. Showing an
  imperfect figure honestly beats showing a wrong one confidently.
- **Answers to purely data-shaped questions.** "List the open tickets" returns
  a refusal, because every claim requires a verbatim document quote and a table
  row has none. Questions that pair data with a *threshold* do work — "is TKT-501
  an SLA breach?" answers, because the threshold clause is quotable. Closing the
  gap means rendering rows as a server-authored table beside the answer, the way
  `runs.action_notice` already carries a fact the model is not allowed to claim.
  Left out because it is a change to the answer contract, not a prompt tweak, and
  guessing at that contract under time pressure is how the citation guarantee
  gets quietly weakened.
- **Hosting.** Runs locally against PostgreSQL 18; a `Dockerfile` and CI workflow
  are included but no deployment is provisioned.

---

## 4. The metric

> **First-contact correct resolution rate, with appropriate escalation**
>
> `(correct cited answers + correct escalations) / total questions`

A system that answers 75% correctly and correctly escalates 20% is far more
valuable than one that attempts 100% and is confidently wrong on 10% — because
the first can be trusted and the second must be checked, and checking costs more
than doing the work.

Two properties make it the right metric rather than a comfortable one:

**Refusals count as successes** when the evidence genuinely is not there. The
eval harness encodes this: `answer-refuses-out-of-corpus` passes *because* it
declines. A metric that scored every refusal as a failure would pressure the
system toward guessing.

**It is already measured, not asserted.** 16 offline golden cases run on every
commit — policy verdicts, retrieval ranking, tenancy isolation — and answer-quality
cases run against a live model before a release. Tenancy failures exit with a
distinct code, because a leak is a security incident rather than a percentage
point.

The leading indicator I would watch alongside it: **the rate at which support
staff go around the system.** If agents stop consulting it for the hard
questions, the accuracy number stops mattering.

---

## 5. AI tool usage

Built using **Claude Code** (Anthropic's CLI agent, Claude Opus) as the primary
development environment, driving the implementation end to end: schema and
migrations, the engine, the API, the frontend, tests, and these documents.

How it was actually used, honestly:

- **Design was collaborative and contested.** Several of the strongest decisions
  came from rejecting the first proposal — the blended relevance/authority score,
  the `{account_filter}` template placeholder, and startup ETL were all argued
  down to the versions now in the codebase, with the reasoning recorded in
  `docs/TECHNICAL_DECISIONS.md`.
- **Verification drove the work.** Every phase was run against real PostgreSQL
  and a live model rather than assumed working, which is how the ten defects in
  §5 of that document were found — the WIN1252 database, the AND-semantics
  retrieval failure, the missing RLS policy, the scope-dropping commit.
- **Tests were written adversarially on purpose.** The tenancy tests issue
  deliberately unfiltered queries; the validator tests each encode a way a model
  produces a plausible wrong answer.

The judgement calls — what to build, what to reject, what to leave out, and what
to admit is weak — are documented with their reasoning so they can be argued
with rather than taken on trust.

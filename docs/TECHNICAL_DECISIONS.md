# ParcelPilot — Technical Decisions, Rationale, and What I Learned

> Engineering record for the Calquity assessment. What was built, why each
> choice was made, what I rejected, and the defects the build surfaced.

**Scale:** 11,322 lines of engine + API, 4,093 lines of tests and eval, 1,959 lines of frontend, 882 lines of SQL. 16 tables, 16 RLS policies, 6 migrations, 156 tests, 22 validated policy parameters.

---

## 1. The problem, restated

The assessment supplies six PDFs and a spreadsheet describing a logistics
support operation. The naive build is a RAG chatbot: chunk the PDFs, embed them,
retrieve, answer.

That build fails this dataset, and the dataset is constructed to prove it:

| Trap in the data | Why a naive RAG system gets it wrong |
|---|---|
| `TKT-450` — an agent told Northstar an INR 250 fee applied after 30 min | Correct under the general SOP. **Wrong for that account**, whose agreement waives the fee. A model that retrieves the SOP and not the contract confidently repeats a real, historical error. |
| `TKT-451` — an agent said Growth supports 3,000 rows | The product limit is **5,000**; 3,000 is where a known defect starts. Two different facts, conflated. |
| Support Policy **v2 DEPRECATED** alongside **v3 CURRENT** | v2 is textually a great match for "what is the P1 response time". Similarity cannot distinguish current from superseded. |
| `ORD-2002` — LumenWorks' clause replaces threshold **and** amount | Applying half the clause yields INR 240 instead of 300 — a plausible, auditable-looking, wrong number. |
| `ORD-1002` — Northstar's waiver, but already `PICKED_UP` | The waiver removes a *fee*; it does not create a right to cancel after pickup. Loose reasoning over that clause gets this wrong. |

So the design goal was never "answer questions". It was: **make being wrong
structurally hard, and being unsure cheap.**

---

## 2. The seven decisions that define the system

### 2.1 Tenancy is enforced by the database, not by application code

**Decision.** PostgreSQL row-level security, with the runtime role
(`parcelpilot_app`) being neither a superuser nor a table owner — because
Postgres exempts both from RLS.

**What I rejected.** The original plan injected `WHERE account_id = ?` into SQL
templates via a `{account_filter}` placeholder. That is the standard approach and
it is a latent breach: one template authored without the placeholder leaks, and
it will be found by a customer rather than a test.

**Why this is stronger.** The RLS helpers return `NULL` when session scope is
unset, and `tenant_id = NULL` matches nothing. So a query that forgets its filter
returns **zero rows**, not everyone's data. Forgetting fails closed.

**How it is proven.** `tests/test_tenancy.py` deliberately issues
`SELECT * FROM orders` with **no `WHERE` clause at all** and passes because the
database refuses. It also asserts the runtime role is not a superuser, does not
own the tables, and that every table carrying `tenant_id` has RLS enabled.

```sql
CREATE POLICY orders_scope ON orders
    USING (tenant_id = app_tenant() AND app_can_see_account(account_id));
```

**Consequence I did not anticipate.** It changes what is safe elsewhere. I
originally rejected text-to-SQL because "injection can leak across accounts" —
but that risk existed *because tenancy lived in application code*. With RLS, a
read-only role and statement timeouts, guarded SQL for *exploration* becomes
defensible. That is documented as the scaling path, not built.

---

### 2.2 Eligibility is a gate; authority is a tie-breaker. They are never blended.

**Decision.** Two orthogonal properties per source:

- **eligibility** — a hard filter. `groundable` / `conflict_only` / `context_only`
- **authority** — an integer, compared only against other authorities, applied at
  conflict-resolution time

**What I rejected.** The obvious scoring function:

```python
final = relevance * 0.7 + authority * 0.2 + freshness * 0.1   # rejected
```

This looks principled and is broken. Setting the deprecated v2 policy to
authority 0.0 only *penalises* it by 0.2 — a strongly-matching v2 chunk still
outranks a weaker v3 one. "Never cite this" has to be a filter. It cannot be a
weight, because a weight can always be outvoted by a good match.

**Implementation.** Retrieval returns three disjoint channels, and the split
happens **before** truncation to `final_k` — otherwise a run of gated chunks
could crowd the citable ones out of the window, reproducing the same failure.

**Proven twice.** Once in retrieval (`test_deprecated_policy_is_never_groundable`)
and again at the last possible moment in the validator: the v2 chunk is placed in
the prompt as conflict context, so the model *can* quote it verbatim — and the
citation is still rejected, because a good quote cannot satisfy eligibility.

---

### 2.3 The model never computes money

**Decision.** Fees, thresholds, credit amounts and eligibility come from
deterministic Python over typed columns. The model routes the question and
explains the result.

```
router (LLM)  ->  chooses tools
rules (Python) ->  decides the number, returns the operative clause
synthesis (LLM) -> states and explains, cites the clause it was handed
validator      ->  verifies the quote, or refuses
```

**Why.** `TKT-450` is a human being making exactly the mistake an LLM makes:
reasoning fluently from the general rule while missing the specific contract.
Retrieval can miss a clause. A foreign key cannot — `accounts.contract_file` tells
us which agreement governs which account, so contract override is a **lookup**,
not a retrieval guess.

**Result.** Same inputs, same verdict, forever. `test_policy.py` asserts the
amounts, not merely that code ran — and pairs every override case with a
counterfactual: ORD-1001 resolved *without* the agreement flips to a fee, ORD-2002
pays 240 instead of 300. If those passed identically the override machinery would
be decorative and I would not know.

**Where "unknown" goes.** `INDETERMINATE` is a first-class verdict, and the only
one permitted to omit a citation. Missing timestamps, unattributed fault,
incoherent data (cancellation before booking) all produce it. The SOP says so
explicitly: *"Do not promise a credit when carrier fault, pickup timing, or
customer fault is unknown."*

---

### 2.4 Policy parameters are validated against the corpus, in CI

**Decision.** Every operative number lives in `policy_pack.yaml` with the
verbatim clause it came from. `parcelpilot policy validate` locates each quote in
the live index and resolves it to a real `chunk_id` and character span.

```yaml
- id: cancellation.free_window_minutes
  value: 30
  source_document: 03_Cancellation_and_Service_Credit_SOP_v4.pdf
  source_quote: "No fee within 30 minutes of booking."
```

**What this buys.** Three silent failure modes become loud:

1. **Drift.** Edit the SOP to say 60 minutes and validation fails in CI, rather
   than the engine applying a stale rule for months.
2. **Misattribution.** An override is cross-checked against
   `accounts.contract_file`, so a clause from customer A cannot set a parameter
   for customer B.
3. **Uncitable decisions.** Every verdict points at a real span, not at a
   document in general.

Matching is **whitespace-insensitive but word-exact**: these PDFs wrap
mid-sentence, so a quote written on one line must still be locatable — while
`"30 minutes"` never matches `"60 minutes"`. Getting only the first half right
would produce a validator that rejects *correct* citations, which is worse than
none, because the team would switch it off.

**This mechanism caught a real error during the build.** Adding SLA targets, I
declared them under the `default` section, whose `source_document` is the SOP —
but the targets table lives in the *support policy*. Validation failed
immediately. The fix was to let a parameter name its own document, which is
better modelling: one scope legitimately draws on several sources.

---

### 2.5 Citations are mechanically verified, and validation is a gate

**Decision.** The model returns structured claims, each with `chunk_id` and a
verbatim quote. Before anything is displayed, every citation is checked against
the chunks **actually retrieved for that run**:

1. **Existence** — the chunk must be in this run's candidates. A real clause the
   model was never shown is still fabricated reasoning.
2. **Eligibility** — the source must be groundable.
3. **Verbatim span** — the quote must appear character-for-character.
4. **Support** — a claim whose citations were all rejected is dropped, never kept
   as an uncited assertion.

**Failure behaviour.** One regeneration, then an honest refusal. No path returns
unvalidated claims. Nothing partially-valid is shown either: the surviving claims
sit beside rejected ones the model believed equally, so the whole generation is
suspect.

**Two details that matter more than they look.**

`document_id` is taken from the **resolved chunk, not the model**, so a real
quote cannot be misattributed to the wrong contract — showing the right words
under the wrong customer's agreement.

The answer schema has **no prose field**. Displayed text is assembled from
surviving claims. If the model's own paragraph were rendered, it could assert
things no claim covers while citation markers implied it had been checked.

**What this does not do.** It does not verify that the quote *entails* the claim
— that is judgement, not computation. It removes the mechanical failures
(fabricated sources, misattributed quotes, ineligible sources, invented numbers),
which is empirically where wrong answers come from.

---

### 2.6 State changes go through a server-side ledger

**Decision.** Two phases. `prepare` records the effect and returns an
`action_id`. `confirm` takes **only that id**.

**Why.** The original design returned `requires_confirmation: true` and had the
client send the action back to execute. That means the client controls what runs,
and can run it twice.

Four properties, each structural:

| Property | Mechanism |
|---|---|
| Cannot be tampered with | The client never holds the payload; it was frozen server-side |
| Exactly-once | `UPDATE ... WHERE status = 'pending'` — the loser of a race updates zero rows |
| Re-authorised at execution | Role checked at prepare **and** confirm; permissions change |
| Expiry has no gap | `AND expires_at > now()` is in the same statement that executes |

`payload_sha256` is verified before executing. The client cannot tamper, so a
mismatch means the stored row was altered — a reason to refuse, not proceed.

**Privilege split.** Preparing is cheap and reversible; committing is neither. A
support agent may propose a credit; only an operations admin commits one. A
customer may propose an escalation and can never commit anything.

**Verified end to end** over HTTP: customer confirm → **403**, ops confirm →
**executed**, second confirm → **409 "already executed"**, with the credit row
and audit entry written.

---

### 2.7 Ingestion is a separate job producing an immutable, versioned index

**Decision.** `parcelpilot ingest run` builds into a `building` index version and
flips it to `active` in one transaction. The server pins the active version and
never mutates it.

**Why.** The original plan ran ETL on app startup. That has three problems: it
blocks readiness, it races across replicas (N workers all parsing the same PDFs),
and it makes rollback impossible. With versioning, a bad ingest is a pointer flip
and every past answer stays reproducible — the run records which index answered
it.

A unique partial index enforces at most one active version per tenant, so a
mis-ordered flip is rejected by the database rather than leaving two live indexes.

---

## 3. Decisions inherited and deliberately kept

- **No agent framework.** A ~400-line orchestrator I can read entirely. The
  security hooks (scoped connections, the eligibility gate, the validator) are
  in the loop, not around a framework's.
- **No text-to-SQL.** A template registry is the whole allowed query surface;
  there is no path from a model-generated string to the database.
- **No fine-tuning.** Retrieval plus deterministic tools gives higher precision
  and zero retraining latency when a policy changes.

## 4. Decisions changed under real constraints

| Planned | Built | Why |
|---|---|---|
| FAISS + SQLite | One PostgreSQL | In-process state meant exactly one server, forever: each worker its own index and its own pending-confirmation table. A correctness bug at N=2, not a scaling one. |
| pgvector | `float4[]` + exact cosine | Not installed on the target host, and not load-bearing at 19 chunks. Behind an interface that swaps in one place. |
| sentence-transformers | API embeddings, pluggable | torch is ~2.5 GB. `EMBEDDING_BACKEND=none` degrades to lexical-only, which is tested rather than incidental. |
| OpenAI | Vertex AI (Gemini 2.5) | The user's available credential — and the better answer: Vertex is the enterprise surface (regional residency, VPC-SC, CMEK, IAM instead of a secret, Cloud Logging audit). It also sidesteps the API-key restrictions that blocked three separate Gemini API keys, because OAuth is not subject to them. |
| Fixed-width chunks | Section-aligned chunks | The corpus is numbered clauses and every answer cites exactly one. A fixed window straddles two rules, and then a citation cannot say which it relied on. |
| Eval harness "next" | Day one, split offline/online | Deterministic cases gate every commit; a harness that needed an API key to test tenancy would end up disabled. |
---

## 4b. Latency: measuring before optimising

The first working version answered in 8–26 seconds, and the instinct was that
this would get worse as the corpus grew — that data volume was the problem, and
that this "kills the purpose of building it". So the first move was to instrument
rather than optimise. One question, every stage timed:

| Stage | Time | Share |
|---|---|---|
| Routing LLM call | 2,593 ms | 32% |
| Query embedding (1 API call) | 1,927 ms | 24% |
| Synthesis LLM call | 3,257 ms | 40% |
| **All database work** | **26 ms** | **0.3%** |

Lexical retrieval 26.1 ms, policy pack resolution 185.7 ms, deterministic verdict
0.1 ms. **99.7% of wall clock was model and network.** The intuition was exactly
backwards: growing the corpus 100× moves a GIN index lookup from 26 ms to maybe
60 ms and changes nothing a user can perceive. The thing that scales badly is the
*number of round trips per question*, which is fixed by architecture, not by data.

That reframed the work. Four changes, each justified by the measurement:

**1. Synthesis on flash instead of pro (−15s).** On `gemini-2.5-pro` synthesis was
81% of wall clock — 19.6s of 24.1s — and almost all of it was the thinking budget.

**2. Thinking budget zero on synthesis (−3 to −20s).** Synthesis is not a
reasoning task in this design. The fee is already computed by the rule engine and
the governing clause is already selected; the model's remaining job is to attribute
sentences to spans it has been handed. It was spending seconds deliberating over a
decision that had already been made deterministically. The single largest win, and
it was available only *because* the money math had been taken away from the model.

**3. A deterministic router (−2.6s).** Tool selection is classification, and this
domain has explicit signals: record ids have a fixed shape (`ORD-1001`, `TKT-501`),
the operative verbs are a closed list, and every answer needs policy text, so
`doc_search` always runs. `agentcore/orchestrator/router.py` plans in
sub-millisecond pattern matching. The LLM planner stays available behind
`router: llm`, and the golden set passes 22/22 either way on this corpus — which
is what makes the switch a measured decision rather than a guess.

The unplanned benefit is bigger than the latency: **tool selection became
reproducible.** The same question now always runs the same tools. For a system
whose product is trustworthiness, "why did it not check the contract that time"
stops being a possible question.

**4. Dense retrieval only when lexical comes back thin (−1.9s on most questions).**
Embedding the query is a ~1.9s API round trip against 26ms for the lexical half of
the same search. On a corpus of numbered clauses, lexical alone already returns the
governing clause whenever it returns anything substantial. Dense still runs when
lexical returns ≤2 hits — precisely when paraphrase recall is the missing thing.

**Result:** 22/22 still passing, and answers land in **1.5–5s** end to end,
measured through the HTTP API rather than in-process. The refusal path is fastest
(734ms) because it never reaches synthesis. The deterministic policy path —
`parcelpilot policy decide` — answers in ~200ms with no model in the loop at all.

**What was NOT done, and why.** Sub-second was asked for and is not achievable
with a generative model in the loop: ~250ms of that is network round trip to
`us-central1` before the model emits a token. Claiming otherwise would have meant
either removing the model (losing the explanation) or streaming tokens into the UI
so it *feels* instant while the answer is still forming — and this system
deliberately shows nothing until citations have been validated. The honest ceiling
is ~1.5s, and the design chose to spend it.

### Two mistakes this pass produced, both worth recording

**A flag that lied.** Skipping dense retrieval left `dense_available=False` in the
run log, because the flag was only ever set inside the block that had just been
skipped. On a perfectly healthy system the logs now said the embedder was
unavailable — the exact signal that sends an engineer debugging credentials for an
hour. Capability and activity are different facts and now have different fields.

**A noun mistaken for a verb.** The first router keyed on `"escalation"` among
others, so *"What is the escalation policy for P2?"* — a question — staged a real
ticket escalation awaiting approval. An informational query turning into a pending
state change is precisely the failure the confirmation gate exists to prevent, and
it slipped in one layer above the gate. `tests/test_fast_router.py` caught it
before it ran anywhere. The detector now requires an imperative form, and the
enquiry-versus-instruction distinction has its own test class. The lesson is narrow
and reusable: **when a pattern matcher gains the power to propose actions, its
false positives stop being cosmetic.**

---

## 4c. The interface was a form pretending to be a chat

The console shipped with a question box, an Ask button, and result panels below.
It was reviewed as working, and it did work — but every new question wiped the
previous answer, sending was bound to Ctrl+Enter, and there was no thread. It read
as a query tool, not something you talk to. That is not a cosmetic gap: a support
product where you cannot refer back to what was just said is not a support product.

The rewrite made it a real conversation, and three decisions in it were deliberate:

**Turns persist, and the thread is durable.** Every turn shares one
`conversation_id`, so the runs are linked in the database rather than only in React
state. An auditor reads the same thread months later.

**The reasoning trace collapses when the answer lands.** Watching the run log
stream is what makes the system's central claim checkable, so it stays. But a
permanent wall of steps above every answer trains people to scroll past it, and a
trace nobody reads verifies nothing. It folds into one line — `✓ 6 steps · 4.2s ·
show reasoning` — that reopens on click.

**Enter sends.** The old Ctrl+Enter binding was the single clearest tell that this
was a form. Nobody reaches for a modifier to send a message.

Threading the conversation surfaced one more security defect. The API accepted a
`conversation_id` and the schema had a foreign key to `conversations`, which looked
like sufficient validation. It is not: **PostgreSQL evaluates referential integrity
as the referenced table's owner, and an owner is exempt from row-level security.**
The constraint would have accepted another customer's conversation id and filed the
run under their thread — RLS would hide the row from the attacker afterwards, but
the write would already have crossed a tenancy boundary, and the victim would see a
stranger's question in their conversation. `_resolve_conversation` now reads the row
through the scoped connection first: an id you cannot see is an id you cannot use.
Four tests in `tests/test_tenancy.py` cover it.

This is the same lesson as the internal-data leak in migration 006, arriving from a
different direction: **RLS protects reads, and a write path can still reference
what it cannot read.** Any code that accepts a caller-supplied primary key must
verify visibility explicitly, because the foreign key will not.

---

## 4d. "Use a better model" — measured, and the answer was no

A reasonable challenge: if a stronger model writes better natural language, use
it. The synthesis model is one config line, so the question deserved data rather
than an opinion.

The golden set cannot settle it. Every candidate passes 22/22, because
correctness here is produced by the rule engine, the eligibility gate and the
citation validator — not by the model. What a bigger model could buy is the
readability of the sentences wrapped around a verdict already decided. That has
to be read, so the comparison ran the same four questions through four
configurations and printed the prose side by side.

| Variant | median | max | words | outcome |
|---|---|---|---|---|
| **flash, thinking 0** | **3,012 ms** | 4,371 ms | 47 | answered 4/4 |
| pro, thinking 128 (its floor) | 7,941 ms | 11,588 ms | 27 | **answered 2/4** |
| pro, thinking default | 16,770 ms | 20,715 ms | 58 | answered 4/4 |
| flash, thinking 512 | 4,828 ms | 6,515 ms | 33 | answered 4/4 |

**The interesting result is the second row.** The hypothesis was that pro's
latency problem was deliberation, not generation — 19.6s of its original 24.1s
was thinking — so pro at its minimum budget should give better prose at close to
flash's speed. It gave *worse* output: it refused two of four questions outright,
returning zero claims on questions flash answered with valid citations.

Which makes sense in retrospect, and is worth stating as a principle: **in this
system, following the constraints IS the reasoning work.** Synthesis has to
satisfy thirteen rules simultaneously — copy a quote character-for-character,
match it to the right chunk id, defer to a deterministic verdict, refuse to
quote a superseded source, translate status codes into English. That is not a
writing task with a reasoning tax bolted on; the compliance is the task. Starve
the budget and pro cannot satisfy them, so it takes the honest exit and declares
insufficient evidence. A larger model with no room to think is worse than a
smaller model with none, because the larger one has further to fall.

Pro *does* write better prose — at default thinking, 16.8s median. That is a
5.6× latency cost for a difference a reader would struggle to name.

### What actually improved the answers was the prompt, and it was free

Reading the outputs made the real problem obvious. Every rule in the synthesis
prompt governed **correctness**; not one governed **how the answer reads**. So
flash was producing this:

> "…waives the cancellation fee for BOOKED shipments before pickup, regardless of
> the booking time. This agreement for ACCT-001 overrides the default policy
> which would otherwise charge INR 250 for cancellations after 30 minutes for
> BOOKED shipments not yet PICKED_UP."

Correct, cited, and written in schema. Pro's advantage was almost entirely that
it *guessed* at the missing instruction. So the instruction got written down —
rules 9-13: lead with the outcome in one plain sentence; never narrate the
machinery ("according to the policy decision"); address the reader ("your
agreement", not "the agreement for account ACCT-001"); never put a status code
in your own prose; name the amount and currency plainly.

After, on flash, same question, 4.4s:

> "You can cancel ORD-1001 without a cancellation fee. The agreement for account
> ACCT-001 waives the cancellation fee regardless of elapsed time. This overrides
> the default policy which would have charged INR 250 for a cancellation after 30
> minutes of booking."

Three findings from that:

**The prompt fix improved every variant, including pro.** "According to the
policy decision…" — pro leaking internal vocabulary into a customer answer —
disappeared once the prompt forbade it. Prompt work compounds across models;
a model swap does not.

**It was free.** No latency, no cost, and the golden set stayed at 22/22 —
which is the only reason a prose change to a citation-critical prompt is safe to
make at all.

**Reading the comparison found a defect nothing else would have.** Flash had been
emitting, as a claim addressed to a customer:

> "Do not promise a credit when carrier fault, pickup timing, or customer fault
> is unknown."

That is a line written for support staff, recited back at the person asking. It
is correctly cited, so the validator passed it; it is factually true, so the eval
passed it. It is just wrong for its reader. Rule 13 now requires converting
operator-directed guidance into what it means for the customer — "I cannot
confirm a credit yet, because fault has not been established" — or omitting it.

The lesson generalises past this codebase: **a citation validator proves an
answer is grounded, not that it is addressed to the right person.** Those are
different properties and only one of them was being checked.

### Where this leaves the configuration

`gemini-2.5-flash`, `synthesis_thinking_budget: 0`, and the prose rules. It is
the fastest option, it answers everything, and after the prompt work the quality
gap that motivated the question is gone. Pro remains one line away —
`LLM_SYNTHESIS_MODEL=gemini-2.5-pro` with `synthesis_thinking_budget: null` —
for anyone who would trade 5.6× latency for slightly warmer sentences.

---

## 4e. What an external audit found that the harness could not

A separate pass ran every prompt in the question catalog against the live stack
and read the console as someone who had never seen a row-level security policy.
Thirty-one checks: 23 passed, 6 failed, 2 partial. Everything deterministic — all
eight policy verdicts, all five proactive detectors, the whole confirmation gate
including the role restriction and the double-confirm constraint — behaved as
documented. The failures clustered in one place, and it was the place the
harness structurally could not look.

### The critical one: an invisible record produced a confident answer

Asked "what is the cancellation fee on ORD-2001?" as ACCT-001, the system did
this:

```
2  tool_result  data_query     {"rows": 0, "record_id": "ORD-2001"}
3  tool_result  policy_decide  {"error": "record not found or not visible"}
4  tool_result  doc_search     {"groundable": 3}
5  synthesize   attempt 1      {"claims": 2, "insufficient_evidence": false}
6  validate     attempt 1      {"claims_valid": 2, "citations_rejected": 0}
```

Row-level security worked perfectly — step 2 returned nothing, step 3 said so
explicitly. Step 5 ignored both and synthesised from the generic policy
documents as though they described that order:

> "There is no cancellation fee for ORD-2001 because your agreement with
> Northstar Logistics allows cancellation…"

Confident, cited, validated, and about another company's shipment. A second
phrasing merged the identities outright: *"LumenWorks, as Northstar Logistics,
can cancel any booked shipment…"*

**No data leaked.** Nothing of ORD-2001 was ever read; that is what RLS
guarantees and it held. But the answer is worse than a leak in one respect: a
customer could act on it. And every gate passed, because each gate was doing its
job — retrieval returned real clauses, and the validator confirmed the quotes
were verbatim.

The defect was that **"I could not see this record" arrived as a tool result
rather than as a halt condition.** A citation validator proves an answer is
*grounded*; it cannot prove the answer is *about the right record*. Those are
different properties and only one was being checked.

`RefusalReason.RECORD_NOT_FOUND` now terminates the run before synthesis is ever
called. One reason code covers "not yours" and "does not exist" deliberately —
separate messages would confirm which ids are real, turning an honest refusal
into an enumeration oracle. Verified live: 631ms to decline, and both the real
sibling order and an invented `ORD-9999` produce identical wording.

### The half the id-based guard could not reach

That guard needs an id to fail on. This question has none:

> **(as ACCT-001)** "What cancellation terms does LumenWorks have?"

Nothing resolves to zero rows, so the run answered with the *asker's* contract
terms. The prose rules from §4d had already stopped it merging identities — it
now said "For Northstar Logistics (ACCT-001)…" — but it was still describing one
company's contract in reply to a question about another's.

Detecting this needs one fact the request path must not have: the set of account
names in the tenant. Granting tenant-wide `SELECT` on `accounts` to fix a tenancy
bug would be self-defeating. So `app_names_foreign_account(text)` is
`SECURITY DEFINER` with a pinned `search_path`, and returns **a single boolean
and never a row**: *does this text name an account you cannot see?* The caller
learns one bit, about a string it supplied itself, and has no way to ask "which
one" or "list them". It reuses `app_can_see_account` — the same predicate the row
policies use — so it can never disagree with them, and staff trip nothing.

The refusal built on it is worded identically whether the named company is a
customer here or does not exist at all, so the bit never reaches the user either.
It only decides whether to stop. Live: **47ms**, no model call at all.

### A fabricated action state — found by verifying, not by the audit

The audit reported that "Issue a service credit of INR 300 for ORD-2002" staged
nothing. Two separate bugs sat underneath that one symptom.

**The router could not see the request.** The imperative gate fired only when a
matched verb opened the sentence or a polite frame preceded it. "Issue a service
credit" satisfies neither — "issue" was in no list, and "credit" sits four words
in. So the credit path was unreachable by the most natural phrasing there is,
including the one in my own catalog. Fixed with transitive imperative openers
(`issue/grant/apply/raise/give/add/put/process`), plus a narrow pattern for
"raise this to P1" where no listed verb appears at all. The enquiry veto still
runs first, so "How do I issue a credit?" still stages nothing.

**And then the answer lied about it.** Checking the ledger rather than trusting
the prose turned up something the audit had not: for a *customer*, the answer read

> "A service credit of INR 300 has been prepared for ORD-2002, and a person must
> confirm it."

`pending_actions` held no such row. Customers may not propose credits, so the
write was correctly refused — and the model inferred "prepared" from an ELIGIBLE
verdict plus an imperative question. This is the exact failure the last sentence
of synthesis rule 8 guards against ("never describe the action as done"),
arriving from the other side: someone told their credit is queued stops chasing
something that does not exist.

The first fix instructed the model to say the action had *not* been raised. **That
could never work, and the reason is structural**: no claim may exist without a
verbatim quote from a source, and no clause in the corpus says "you are not
authorised to request this". The instruction asked for an uncitable claim, so the
model silently dropped it.

A fact about the system's own state is not a claim about the world. It is known
deterministically, it needs no evidence, and routing it through a component that
must cite everything is the wrong shape. So `runs.action_notice` (migration 007)
carries it, server-authored, rendered as a system notice styled unlike the
assistant's prose — the reader should be able to see that the machine is saying
this, not the assistant. Verified live: customer gets the notice and no false
claim; a support agent gets the credit actually staged.

### Wall-clock arithmetic against a snapshot dataset

Staff had an "as of" field; customers had nothing. So the pickup the dashboard
reported as 4.5 hours late came back in chat as **"171.1 hours past the 4-hour
threshold"** — a week of real elapsed time since the snapshot, presented to a
customer as fact. The credit amount stayed correct, because the rule engine
compares against thresholds rather than printing elapsed time, which is exactly
why nobody noticed: an evidence bug, not a money bug. It is still the kind of
number that ends a customer's trust in one line.

`data.snapshot_at` in `config.yaml` is now the reference clock, with precedence
`explicit as_of` → `configured snapshot` → `now()`. Server-side deliberately: a
customer must not be able to choose what time it is, and a demo must not depend
on an operator remembering to type a timestamp. The staged credit summary now
reads "4.5 hours past the end", matching the dashboard exactly.

### Two frontend bugs whose data was already fetched

`GET /api/chat/{run_id}` returns `steps` and `pending_action` alongside the run.
`RecentRuns.onOpen` passed only `d.run` and hard-coded `steps: []` — so under a
card titled *"Every answer is replayable"*, a replayed answer showed no reasoning
at all, and a replayed run awaiting approval lost its confirmation drawer: the
approval vanished from the interface while still pending in the ledger. And
history fetched once with an empty dependency array, so your own conversation did
not appear in your own history until a page reload — the runs were persisted
correctly all along, which is the version of that bug most likely to be read as
"it didn't save".

### The offer that could not be accepted

Every refusal in this system carries `escalation_offered=True` and reads "I can
pass this to a human support agent with everything gathered so far". There was no
control that did it. This system refuses often *by design*, so a dead end at
every refusal quietly undercuts the behaviour it is proudest of — an offer you
cannot accept reads as a brush-off.

My first implementation executed the follow-up immediately, reasoning that asking
a human for help grants the asker nothing and so needs no second approver. Two
things overruled it, and both were already right:

* **`follow_ups.action_id` is NOT NULL.** The schema refuses to hold a row that
  is not the effect of a ledger action. The working agreement about single
  ownership of that table is enforced in Postgres, not just in review.
* **`_PREPARE_ROLES` already allowed every role to prepare a follow-up** while
  `_CONFIRM_ROLES` restricted committing to staff. The matrix had this exact case
  right before the feature existed.

So a handoff is an ordinary ledger action landing in the support queue as
`awaiting_confirmation`, and the customer is told that plainly rather than told it
is done. `POST /api/chat/{run_id}/handoff` is not staff-gated — an offer only
staff can accept is not an offer — but the run must be visible through the scoped
connection first, because the foreign key would accept a run id belonging to
someone else. Verified: 201 for the owner, 404 for a sibling account.

### The pattern across all of these

Five of the six findings share a shape: **a component did its job correctly, and
the system was wrong anyway.** RLS returned zero rows and the run continued. The
validator confirmed real quotes about the wrong record. The rule engine computed
the right credit and the prose reported the wrong elapsed time. The role matrix
correctly refused a write and the answer claimed it had happened. The API
returned the steps and the client dropped them.

Per-component tests cannot see any of it, which is why 183 passing tests, 22/22
eval, clean lint and a clean build coexisted with all six. What found them was
running every real question as a real user and reading the answers — and then,
for each claim, checking the ledger and the run log rather than the prose.


---

## 5. What I learned — the defects the build surfaced

Every one of these was found by a test or by running the thing, not by review.
They are the strongest evidence the harness earns its keep.

### 5.1 The database could not store a rupee sign

`server_encoding` was **WIN1252**. The PG18 cluster's `template1` was initdb'd
with a Windows locale, so `CREATE DATABASE` inherited an encoding that physically
cannot hold `₹`, an em-dash or a curly quote. For a system billing in INR that is
silent data corruption.

Found because a zero-width space failed to encode in a **query parameter**.
Fixed: bootstrap creates UTF8 from `template0` with the builtin `C.UTF-8` locale
(deterministic, so text sorts identically here and in Linux CI), **refuses** to
run against a non-UTF8 database, and encoding is now a readiness condition —
serving from WIN1252 answers with mangled text and nothing alerts.

**Lesson:** encoding is part of correctness, not configuration. Assert it.

### 5.2 Natural-language questions returned zero results

`websearch_to_tsquery` ANDs every term. Asked *"Can I cancel a booked shipment
without a cancellation fee?"*, retrieval returned **nothing** — the operative SOP
clause says "cancelled", "BOOKED" and "cancellation fee" but never says
"shipment", and one incidental noun failed the AND.

Recall was near zero for precisely the questions users ask, and it failed
*silently*: the engine refused and looked appropriately cautious.

Fixed by tokenising the query with `to_tsvector` (same stemming as the indexed
column), joining lexemes with `|`, and letting `ts_rank_cd` discriminate.

**Lesson:** discrimination is ranking's job, not filtering's. And a system that
fails by being *quiet* is more dangerous than one that errors.

### 5.3 A missing RLS policy allowed tenant enumeration

`test_rls_is_enabled_on_every_scoped_table` asserts every table with a
`tenant_id` column has RLS. It caught `tenants` — whose `tenant_id` is a primary
key rather than a foreign one, so it did not *read* like scoped data, but the
table lists every customer organisation on the platform.

**Lesson:** write the structural test, not the specific one. A code review would
not have caught this; a test that enumerates the schema did.

### 5.4 Committing silently destroyed the RLS scope

Two individually-correct decisions in direct collision. Scope is set with
`set_config(..., is_local => true)` so it dies with the transaction — that is what
stops a pooled connection carrying one principal's scope into another's request.
But the orchestrator commits after **every** run-log step so the trace is
tailable mid-flight.

So the first commit dropped the scope and every later query saw an empty
database. Fixed with a `ScopedConnection` proxy that re-binds on
`commit`/`rollback`.

**Lesson:** fail-closed design converted what would have been a data leak into an
obvious outage. That is the whole argument for it, demonstrated accidentally.

### 5.5 A policy decision's clause could never be cited

The rule engine returns a clause validated against the live index. But if the
router chose only `policy_decide`, that chunk was not in `retrieval.groundable`
— so the validator rejected the very clause the engine had told the model to
quote. **Every policy answer would have refused.**

Fixed by admitting decision clauses through the scoped connection, so RLS still
applies — keeping one validation path rather than exempting policy citations from
checking.

**Lesson:** two correct components can compose into a broken system. Only an
end-to-end test finds it.

### 5.6 `@lru_cache` on a function taking a pydantic model

`get_llm(settings)` was cached on its argument. `Settings` is not hashable, so
**every caller passing settings explicitly** hit `TypeError: unhashable type`.
Only the no-argument path had ever been exercised.

**Lesson:** test the parameterised path, not just the default one.

### 5.7 A NOT NULL column defeated its own default

Writing a test for unmapped spreadsheet sheets, a workbook missing the
`premium_support` column failed the entire load with a `NOT NULL` violation: the
loader passed an explicit `NULL`, which defeats the column default. That breaks
exactly the scenario the mapping layer exists for — onboarding a new company's
workbook. Fixed by declaring `default=` on the `ColumnSpec`, keeping the meaning
of "absent" with the mapping.

### 5.8 Silent skipping is worse than failing

Unmapped sheets were skipped with a bare `continue`: zero rows, no error, an
ingest that looked like it worked. Now reported at WARNING and on the ingest
report, with row counts and column names. Documentation sheets (`README`) are
still skipped quietly, because that is expected.

**Lesson:** a declared boundary is fine. An *invisible* one is a defect.

### 5.9 `IS NULL` meant "everyone", not "internal"

The worst bug in the build, found last, by auditing what a customer could
actually reach through the API rather than by reading the policy.

Staff act tenant-wide, so their runs carry `account_id = NULL`. The policy said:

```sql
account_id IS NULL OR app_can_see_account(account_id)
```

`IS NULL` was intended to mean "a tenant-wide internal record". It actually
meant **visible to everyone in the tenant** — so any customer could read an
operations user's run: their question, the answer, the reasoning trace, and
retrieval candidates spanning every account. `GET /api/chat/{ops_run_id}`
returned **200** for a customer.

What made it survive review is that the *same SQL shape* is correct elsewhere:
for `documents`, `owner_account_id IS NULL` means "a general policy document",
which every customer must read. One pattern, two opposite meanings, in adjacent
policies.

Fixed in `006_internal_records_are_staff_only.sql`: a NULL account now requires
`app_is_staff()` for runs, steps, candidates, conversations and audit rows,
while documents keep the permissive reading.

**Lesson:** RLS policies must be audited by *exercising them as each principal*,
not by reading them. Every other tenancy test asserted that a customer could not
see another CUSTOMER's data — none asked whether a customer could see data
belonging to no account at all. The absent test was the vulnerability.

### 5.10 Synthesis was 81% of wall clock

Measured, not guessed: `gemini-2.5-pro` synthesis took 19.6s of a 24.1s
response, because the thinking budget dominates. Switching to
`gemini-2.5-flash` keeps the full golden set at **22/22** — every citation and
refusal case — and drops answers to 1–10s. A 2.5× speedup with no measured
quality loss, and the eval suite is what made the decision defensible rather
than a hunch.

**Lesson:** without a golden set, that switch is a gamble. With one, it is an
experiment.

### 5.11 Logs on stdout made the CLI unpipeable

structlog wrote to stdout alongside command output, so `policy decide | jq` broke
on the log line. Moved to stderr.

### 5.12 Two sources of truth for citation numbering

The prose said `[2]` and `[3]` but only `[1]` was listed: `render_prose` numbers
per unique *span*, the printer deduped per *chunk*. The same clause quoted twice
is two citations. Both now derive from one numbering.

### 5.13 A safety halt that required correct punctuation

Asked *"what is cancelation price of ord 2001"* — no hyphen, lower case — the
system answered. It described the caller's own cancellation terms as though
ORD-2001 were their order. ORD-2001 belongs to a different customer.

Nothing leaked: no row was ever read, and row-level security would have returned
zero rows if one had been. The failure was upstream of that. Record ids were
recognised as `(?:TKT|ORD|ACCT)-\d+`, so `ord 2001` matched nothing, no scoped
lookup was planned, and the `RECORD_NOT_FOUND` halt — which exists precisely so
that a named-but-invisible record terminates the run — never had an id to fire
on. The run fell through to general policy and answered confidently about a
record it had never seen.

The halt was real and tested. It was reachable only by callers who typed an id
the way the database writes it.

**The first fix was the wrong kind of fix.** I relaxed the separator and added
the spoken forms, so `ord 2001`, `ord2001` and `ORD - 2001` all resolved. Tests
passed, the eval passed, and the next phrasing broke it again: *"whats the fee
onord 2001"* runs "on" into "ord", putting the prefix mid-word where a left word
boundary cannot match. Widening a prefix alternation once per typo is a losing
game — every round ships a guard that only reaches people who type cleanly, and
the failure mode is silent each time.

So the search is inverted. Find the **digits** first — a 3-to-6 digit run is rare
and unambiguous to locate — then decide whether they name a record by looking at
the short window of text before them. That is stable under separators, case,
run-together words and stray punctuation, because none of those change what word
precedes the number. Two rules stop it over-reading: a trailing unit word vetoes
the match, so *"after 30 minutes"*, *"charge INR 250"* and *"5000 rows"* stay
quantities; and a bare number with no record word before it names nothing, so
*"can I upload 4200 rows"* is not an order.

The pattern also **existed twice**. `router.py` planned lookups with one copy and
`engine.py` recovered ids for the action gate with another. Fixing only the router
would have left the planner reading `ord 2001` while the gate still demanded
`ORD-2001` — a run could stage an action against a record the halt had never
checked. There is now one recogniser, and a test asserts the duplicate is gone.

Found by a user typing the question the way people actually type it, not by a
test. Every phrasing in the golden set used canonical ids, because the person who
wrote both the set and the pattern held the same idea of what an id looks like.
That is the same shape as §5.9: a guard whose author and whose caller disagree
about the input is not a guard, and the disagreement is invisible from inside.

The generalisable lesson is narrower than "handle typos". It is that **a guard
keyed on the surface form of input inherits every way that form can vary**, and
the fix is to key it on something that does not vary — here, the shape of the
number rather than the spelling of the label.

### The defect every gate missed: CRLF

The worst bug in the build, and the most instructive. Symptom, as reported:

> "when i ask question it doesnt answer in the chat box it says reasoning and when
> refresh the data is shown in side in history conversation when click that then it
> appears in the chatbox"

So: ask a question, watch it say *Reasoning*, wait forever. Refresh, find the
answer sitting in history, click it, and there it is — complete, cited, correct.

Which made it look like a rendering bug. It was a **one-byte framing mismatch**.

`sse_starlette` separates Server-Sent Events frames with **CRLF**. The frontend
parser split the stream on `'\n\n'`. That substring never occurs in the server's
output — 7 CRLF-CRLF separators, 0 LF-LF — so the buffer grew forever and **not a
single frame was ever parsed**. No step rendered. `done` never fired. The turn
spun until the 300-second ceiling, while the run had finished in four seconds and
committed its answer to the database. That is exactly why history worked: the
answer had always been there, and only the live path was blind.

**What makes this worth writing down is what was green at the time.**

| Gate | Result |
|---|---|
| 176 Python tests | pass |
| Golden eval | 22/22 |
| Ruff | clean |
| Migration drift | clean |
| `frontend && npm run build` | pass |
| Live SSE endpoint, byte-inspected | correct |
| The product | **completely unusable** |

Every component was correct and every test of every component passed. The defect
lived in the byte-level contract *between* two components, and per-component
tests structurally cannot see that seam. This is the clearest case in the build
for integration tests earning their place — and for the narrower rule that **when
you reimplement a protocol the platform already implements, you inherit the
obligation to test it.**

The reimplementation itself was forced and is still the right call: `EventSource`
handles SSE framing for free but cannot send an `Authorization` header, and the
alternative — a token in the query string — puts a credential in server logs and
browser history. Taking the framing in-house was correct. Not testing it was not.

**The fix**, in `frontend/src/lib/api.js`: `splitFrames` accepts all three
separators the spec allows (`\r\n\r\n`, `\n\n`, `\r\r`), longest-match first —
because `\r\n\r\n` contains `\n\n` at offset 1, and consuming the shorter one
leaves a stray `\r` that silently becomes an empty leading line of the next frame.
`parseFrame` splits fields on any of the three line endings, strips the single
optional leading space the spec defines, and ignores comment lines so the
keep-alive ping does not become a null step. Both are exported and covered by 16
unit tests in `frontend/src/lib/sse.test.js`, including the reassembly of a frame
split mid-payload across two TCP chunks and the verbatim wire capture that used
to parse to nothing.

Two further hardening changes came out of it:

**Silence is now an error.** If the socket closes without a terminal frame — a
proxy timing out, the server going away mid-run — the client used to exit its read
loop quietly and leave the turn spinning. It now reports it. A spinner that never
resolves is the single worst failure mode in a chat interface, because it is
indistinguishable from slowness, which is why this bug read as "too slow" rather
than "broken".

**`npm test` and `npm run build` are now CI steps.** The frontend had no test
runner at all, which is the actual root cause: there was nowhere for a test of
this to live, so it was never written.

### Perceived latency is a separate problem from latency

One honest gap remained after the fix. Steps are written to the run log *after*
they complete, with their measured duration — right for an audit trail, wrong for
a person waiting. Synthesis is a single ~4s model call, so during it the trace
showed a heading and a spinner and nothing else. "It just says reasoning" is
precisely what that looks like from the outside, and it was true even once the
stream worked.

`PendingStep` closes it by inferring the current stage client-side rather than
writing speculative rows to the durable log: retrieval is the last thing the agent
does before it calls the model, so once a tool step has landed and no `synthesize`
step has, the model call is in flight. The label names the stage — *"Writing a
cited answer from the retrieved clauses…"* — with a live tenth-of-a-second timer.
Nothing is invented and the audit trail stays exactly as accurate as it was.

This is deliberately not token streaming. Streaming would make it *feel* instant,
but it would put unvalidated sentences on screen, and the whole system exists to
prevent that. A truthful clock on an honest wait is the version that does not
contradict the product.

---

## 6. Where the design is still weak

Stated plainly, because an assessment that hides these is less trustworthy than
one that names them.

| Limitation | Impact | Fix |
|---|---|---|
| Dense retrieval is a sequential scan | Fine to ~2k chunks, painful at 10k, unusable at 100k | pgvector + HNSW — one column type, one query |
| Ingestion re-embeds everything | Repeated cost on every run | Content-hash cache, concurrent batches |
| Workbook loaded fully into memory | OOM on very large sheets | Stream + `COPY` |
| "Business hours" ≈ wall-clock | SLA figures approximate for one account | Business-calendar implementation; currently **labelled** wherever used |
| Login has no identity provider | Demo-grade authentication | Replace `issue_token`; verification is already real |
| Single tenant provisioned | `config.yaml` is global | Per-tenant config + mapping registry |
| Answer-quality eval needs a human to run it | Online cases are not on every commit | Accept: they cost money and vary. Offline cases gate CI. |

The policy engine will never be domain-agnostic, and should not be. What
generalises is the trust machinery — citation validation, the eligibility gate,
tenancy, the action ledger, the audit trail. The rules are per-customer
configuration on top of it.

---

## 7. The metric I would run the product on

> **First-contact correct resolution rate, with appropriate escalation**
> `(correct cited answers + correct refusals) / total questions`

A system that answers 75% correctly and correctly escalates 20% is far more
valuable than one that attempts 100% and is confidently wrong on 10% — because
the first is trustable and the second has to be checked, which costs more than
doing the work.

Refusals are scored as **successes** when the evidence genuinely is not there.
The eval harness encodes that: `answer-refuses-out-of-corpus` passes *because* it
declines. A harness that treated every refusal as a failure would pressure the
system toward guessing, which is the failure mode the whole architecture exists
to prevent.

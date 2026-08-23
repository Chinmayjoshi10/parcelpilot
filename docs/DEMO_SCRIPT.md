# ParcelPilot — 6-Minute Demo Script

> For a Loom recording. Timings are cumulative. Spoken lines are what to say;
> **[SHOW]** is what should be on screen.
>
> The through-line, repeated three times in different words: **in high-stakes
> operations, a fluently wrong answer costs more than no answer.** Everything in
> the demo is evidence for that one sentence.

---

## Before you hit record

```powershell
# 1. Terminal A — API
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# 2. Terminal B — frontend
cd frontend; npm run dev            # http://localhost:5173

# 3. Terminal C — kept free for live commands

# 4. Sanity: all three must be green
.\.venv\Scripts\python.exe -m agentcore.cli db health        # ready: true
.\.venv\Scripts\python.exe -m agentcore.cli llm probe        # structured_output ok
.\.venv\Scripts\python.exe -m agentcore.cli policy validate  # ok: true
```

**Checklist**
- [ ] `llm probe` is green — a live model is needed for the chat segment
- [ ] `ingest run` has been executed **with** embeddings (`embedded_count: 19`)
- [ ] Browser zoom ~110%, dark room, terminal font ≥ 16pt
- [ ] Two browser tabs ready: signed in as Northstar, and as Operations
- [ ] Close Slack/email notifications

**If the model credential is dead**, do not cancel — say so on camera and pivot:
"the model is unavailable right now, and notice what the system does — it
refuses rather than guessing. That's the degraded path, and it's tested." Then
run the deterministic segments (§3, §5, §6), which need no model at all. That is
a *stronger* demo than a happy path, if you frame it deliberately.

---

## 0:00–0:40 — The problem, not the product

**[SHOW]** `docs/TECHNICAL_DECISIONS.md` §1, the table of traps.

> "ParcelPilot is an agentic support system, but I want to start with why the
> obvious build fails.
>
> This dataset is a trap. Ticket 450 records a real human agent telling Northstar
> that a 250-rupee cancellation fee applied. That's correct under the general
> SOP — and wrong for that customer, because their signed agreement waives the
> fee outright.
>
> A RAG chatbot reproduces that error every time retrieval surfaces the policy
> and misses the contract. There's also a deprecated policy sitting next to the
> current one that's textually a *better* match for the obvious question.
>
> So I didn't build a system that answers questions. I built one where being
> wrong is structurally hard, and being unsure is cheap."

---

## 0:40–1:50 — The flagship answer

**[SHOW]** Browser, signed in as **Northstar Logistics (Enterprise)**.

Type: *"Can I cancel ORD-1001 without a cancellation fee? Explain why."*

> "Watch the reasoning stream while it works. That's not a loading animation —
> it's the durable run log being tailed. What I'm watching live is exactly what
> an auditor reads back in six months."

Point at the steps as they land: `decompose` → `policy_decide` → `doc_search` →
`conflict` → `synthesize` → `validate`.

When the answer appears:

> "Three things here.
>
> **First** — it says no fee. That's the right answer, and it's the one the human
> agent got wrong.
>
> **Second** — look at the citations. It cites Northstar's agreement, *then* it
> cites the default SOP it's overriding, *then* the precedence rule. It shows the
> conflict instead of hiding it. An answer that showed only the contract would be
> a worse answer, even though the conclusion is identical.
>
> **Third** — 'Conflicts resolved' at the bottom. It's telling you which document
> won and why."

Click citation chip **[1]**.

> "And this is the part I'd point at hardest. That's the exact stored paragraph,
> with the cited span highlighted — positioned using character offsets the
> *validator* resolved when the answer was produced. The quote was matched against
> this text character-for-character before the claim was allowed on screen."

---

## 1:50–2:35 — The number is not computed by the model

**[SHOW]** Terminal C.

```powershell
.\.venv\Scripts\python.exe -m agentcore.cli policy decide --order ORD-2002 `
  --rule failed_pickup_credit --as-of "2026-08-16T11:00:00+05:30"
```

> "The fee in that answer didn't come from the language model. It came from here.
>
> LumenWorks' agreement replaces *both* the delay threshold and the credit
> amount. Default policy says: past two hours, pay the lower of 500 rupees or 10%
> of the fee — that's 240. Their contract says: past four hours, pay a flat 300.
>
> Apply half the clause and you get 240. It's plausible, it looks auditable, and
> it's wrong."

Point at `verdict`, `inputs`, `citation`.

> "Verdict, the arithmetic it actually did, and the clause it relied on. No model
> in that path. Same inputs, same answer, forever — and my tests assert the
> *amount*, not that code ran. Each override case is paired with a counterfactual
> that resolves without the agreement: if the verdict didn't flip, the override
> would be decorative and I wouldn't know."

---

## 2:35–3:25 — Tenancy: enforced by the database

**[SHOW]** Browser → **Sources** tab, still as Northstar.

> "Five documents. Their own agreement, plus the global policies."

Switch tab → signed in as **LumenWorks**. Sources tab.

> "Same code, different documents. Northstar's agreement isn't in that list."

**[SHOW]** Terminal C — `tests/test_tenancy.py`, scroll to `UNFILTERED`.

> "Here's why that's not just a filtered view. Every query in my tenancy tests is
> written *deliberately wrong* — `SELECT * FROM orders`, no WHERE clause at all.
>
> They pass because PostgreSQL row-level security refuses. The runtime role isn't
> a superuser and doesn't own the tables — Postgres exempts both from RLS, so
> that's the single most important property of that role.
>
> And it fails closed: an unscoped connection sees **zero rows**, not everything.
> Forgetting the filter is an outage, never a leak."

Back in the browser, as Northstar, ask: *"What is the cancellation fee on
ORD-2001?"* (LumenWorks' order.)

> "It declines. The record is invisible, so the tool found nothing — and
> 'not found' is deliberately indistinguishable from 'not yours', because
> confirming that order exists would itself be a disclosure."

---

## 3:25–4:20 — Proactive detection and the confirmation gate

**[SHOW]** Browser → sign in as **Operations Admin** → Operations tab.

> "That was the reactive half. This half asks the questions.
>
> Every row is a deterministic detector, and every row cites the clause it
> breached — because a breach warning without a citation is an opinion."

Point at rows:

> "Ticket 501 is past its first-response target. Notice it's measured against
> **fifteen minutes**, not the thirty-minute default — that's Northstar's
> contracted SLA, resolved per account.
>
> ORD-2002 owes 300 rupees. That's the same rule engine the chat uses, so the
> dashboard and the chat can't disagree.
>
> And these two are the dataset's traps, caught: ticket 450's answer contradicts
> the agreement, and 451 reported a known defect threshold as if it were the plan
> limit."

Click **Propose** on the credit row → it appears in the approval queue.

> "Nothing has happened yet. That's a proposal."

Click **Approve & execute**.

> "Now it's executed. And here's the design: approval sends only an action id.
> The client never holds the payload — it was frozen server-side when the action
> was prepared, so this UI *cannot* alter what executes."

Click **Approve** again on the same item (or show the 409 in terminal).

> "Second approval: 409, already executed. Exactly-once, enforced by a
> conditional UPDATE, so two operators clicking together can't pay twice."

---

## 4:20–5:10 — Measured, not asserted

**[SHOW]** Terminal C.

```powershell
.\.venv\Scripts\python.exe -m agentcore.cli eval run --offline
```

> "Sixteen out of sixteen, in about a second. Policy verdicts, retrieval ranking,
> tenancy isolation — deterministic, free, no model. That's what gates every
> commit.
>
> The split matters. Answer-quality cases need a live model, cost money and vary
> slightly, so they run before a release. If testing tenancy needed an API key,
> the pipeline would end up switched off.
>
> Tenancy failures exit with code 2 rather than 1 — a leak is a security incident,
> not a percentage point."

```powershell
.\.venv\Scripts\python.exe -m agentcore.cli policy validate
```

> "And this is the drift check. Twenty-two parameters, each carrying the verbatim
> clause it came from, each re-located in the live corpus. Change '30 minutes' in
> the SOP and this fails in CI — instead of the engine quietly applying a stale
> rule for months.
>
> It actually caught me during the build: I declared the SLA targets against the
> wrong source document and it refused."

**[OPTIONAL — 20s, if you have room. Strong material: it shows you profile before you optimise.]**

> "One more thing worth showing, because it's the mistake I nearly made.
>
> The first version answered in eight to twenty-six seconds, and my assumption was
> that this would get worse as the data grew — that it was a retrieval problem.
> So I instrumented it instead of guessing.
>
> Routing model call: two and a half seconds. Query embedding: two seconds.
> Synthesis: three seconds. **All of the database work: twenty-six milliseconds.**
>
> Ninety-nine point seven percent was model and network. My intuition was exactly
> backwards — a hundred times the corpus moves a GIN index lookup from 26ms to
> maybe 60ms. What scales badly is the number of round trips per question, and
> that's architecture, not data.
>
> So: synthesis moved to flash, thinking budget to zero, the router became
> deterministic, and query embedding now only runs when lexical search comes back
> thin. Answers land in one and a half to five seconds. Still twenty-two out of
> twenty-two on the golden set — which is the only reason I trust any of it.
>
> The interesting one is thinking budget zero. That's only safe *because* the
> model doesn't do the arithmetic. The fee is computed and the clause is chosen
> before synthesis is called, so the model was spending seconds deliberating over
> a decision that had already been made. A trust decision paid for a performance
> win.
>
> And I won't oversell it: sub-second isn't possible with a generative model in
> the loop — a quarter of a second of that is just network to us-central1. I
> could make it *feel* instant by streaming tokens, but this system deliberately
> shows nothing until the citations are validated. I'd rather it be a second
> slower and never show you an unverified sentence."


---

## 5:10–6:00 — What the build taught me, and what's missing

> "Three things I'd want you to take away.
>
> **One** — the tests found real defects before any user could. The database was
> WIN1252, so it physically could not store a rupee sign — for a system billing in
> INR that's silent corruption. Natural-language questions were returning *zero*
> results because Postgres full-text search ANDs every term, and it failed
> quietly, which is worse than erroring. And a structural test caught a table
> where I'd forgotten row-level security, which would have let any customer
> enumerate the whole client roster.
>
> **Two** — fail-closed design paid off in a way I didn't plan. Committing inside
> a run silently dropped the RLS scope. In a system where tenancy lives in
> application code, that's a data leak. Here it was an obvious outage, because
> unscoped means zero rows.
>
> **Three** — what's *not* done, honestly. Dense retrieval is a sequential scan;
> it needs pgvector past ten thousand chunks. Ingestion re-embeds everything each
> run. Login issues tokens without an identity provider, though verification is
> real. One agreement quotes business hours, which I approximate as wall-clock and
> label everywhere it's used.
>
> The policy engine will never be domain-agnostic, and shouldn't be — cancellation
> rules are logistics rules. What generalises is the trust machinery: citation
> validation, the eligibility gate, tenancy, the action ledger, the audit trail.
> That's the reusable product. The rules are configuration on top.
>
> The metric I'd run this on is first-contact correct resolution *with appropriate
> escalation*. A system that answers 75% correctly and escalates 20% properly beats
> one that attempts everything and is confidently wrong on 10% — because the first
> one you can trust, and the second one you have to check, which costs more than
> doing the work yourself."

---

## Appendix — questions you'll likely be asked

**"Why not LangChain?"**
> The orchestrator is about 400 lines I can read entirely. The security hooks —
> scoped connections, the eligibility gate, the citation validator — are *in* the
> loop, not wrapped around a framework's. And when validation fails I control
> exactly what happens: one regeneration, then refuse.

**"Why is authority not part of the relevance score?"**
> Because then it's a weight, and a weight can be outvoted by a good match.
> Setting the deprecated policy to authority zero in a blended score only
> penalises it — a strong lexical hit still wins. "Never cite this" has to be a
> filter.

**"How do you know the citations are real?"**
> Every quote is located character-for-character in the chunk it points at, and
> that chunk must be one this run actually retrieved. A real clause the model was
> never shown is still fabricated reasoning. Failing that check produces a refusal,
> not a warning.

**"Would this survive a real corpus?"**
> The document side yes — it's content-classified and per-file failures are
> contained. Spreadsheets need a per-tenant mapping registry, and the mapping is
> declared rather than inferred on purpose: the rule engine reads `booked_at` by
> name, and a schema that can change shape can't support a rule that must answer
> the same way twice. Retrieval needs pgvector past ten thousand chunks. I'd
> estimate about a week to go from this corpus to any company's tabular estate.

**"What about prompt injection?"**
> The agent reads customer-authored ticket text, so it's a live risk. Three
> layers: retrieved content is wrapped in delimited untrusted blocks and can't
> forge its own terminator; money comes from the rule engine, so no text can
> change a fee; and actions execute from the ledger after a human approves, with
> the role re-checked. Worst case is odd prose, not moved money — and there's a
> test that asserts exactly that.

**"Why Vertex rather than the Gemini API?"**
> It's the enterprise surface — regional data residency, VPC-SC, CMEK, IAM instead
> of a bearer secret in a file, audit logs in Cloud Logging. For a system whose
> selling point is trustworthiness, "runs inside your compliance boundary" is a
> real answer. It also sidesteps API-key restrictions entirely, which is what
> blocked three separate keys during the build.

**"What would you do next?"**
> pgvector and an embedding cache, because those are the scaling ceiling. Then a
> real identity provider. Then the human-in-the-loop correction path: when a
> support manager overrides an answer, that correction should become a reviewed
> policy parameter — which the validation mechanism already supports, since every
> parameter has to cite a clause.

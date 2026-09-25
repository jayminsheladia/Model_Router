# Model Router

A policy-aware routing layer for LLM requests. Instead of just picking "cheap
model vs. frontier model" based on prompt complexity (which RouteLLM,
LiteLLM, Not Diamond, etc. already do), this adds the layer those tools
don't: **who is asking**, **what project they're working on**, **what
they're allowed to spend**, **whether the request is even legitimate work
use**, and a **feedback loop** that lets routing decisions improve over
time — with a full audit trail explaining every decision.

All six components from the original design are implemented:

1. **Classifier** (`app/classifier.py`) — heuristic complexity/off-policy scorer, fronting a
   real LLM fallback (`app/llm_classifier.py`) for genuinely ambiguous prompts
2. **Identity & context** (`app/identity.py`) — users, teams, budgets, project-level access
3. **Policy engine** (`app/policy.py`) — off-policy block, budget block, project authorization,
   tier caps, and a Quality/Cost/Balanced routing-mode dial
4. **Budget ledger** (`app/budget.py`) — per-user monthly spend with soft/hard limits, using
   reservations so concurrent requests can't collectively overshoot a cap
5. **Audit trail** (`app/audit.py`) — full decision trace for every request
6. **Feedback loop** (`app/feedback.py`) — escalate/downgrade signals bias future classifier suggestions

Two things borrowed from studying Azure Foundry's and AnythingLLM's model
routers round this out: a **routing-mode dial** (Quality/Cost/Balanced, `app/policy.py`)
and **automatic failover** (`app/router.py` steps down a tier if the chosen
one is "unavailable" — a simulated marker, or a real transient error).

The model backend is real when a `GROQ_API_KEY` is set: `cheap`/`mid`/`frontier`
map to three actual Groq-hosted models with real cost, latency, and generated
output (see "Real model serving" below), falling back to a simulated backend
(`app/models/mock_client.py`) otherwise. The *classification* path separately
makes a real Claude call for ambiguous prompts, see "Real LLM classification"
below — the two are independent and can be enabled separately.

## How a request flows

```
POST /route {user_id, prompt, project?, routing_mode?, conversation_id?}
  -> identity lookup       (team, budget, max tier, allowed projects, per-project tier overrides)
  -> conversation lookup    (if conversation_id given: prior turns for this user, or blocked if the
                             conversation belongs to someone else -- see "Conversation memory" below)
  -> classifier              (heuristic complexity scorer, biased by past /feedback signals;
                              genuinely ambiguous prompts get a real Claude Haiku call, see below)
  -> budget check              (this month's spend vs. monthly limit -> soft/hard limit flags)
  -> policy engine                (off-policy? hard limit? user allowed on this project? project itself
                                   restricted to other users? tier cap/override? cost/quality routing
                                   mode adjustment? soft-limit downgrade?)
  -> model call                      (real Groq call if GROQ_API_KEY is set, else simulated; steps
                                      down a tier and retries if the chosen one is "unavailable" --
                                      automatic failover, real or simulated)
  -> budget ledger update
  -> audit log                          (SQLite row: full decision trace, returns an audit_id)

POST /feedback {audit_id, signal: "escalate" | "downgrade_ok"}
  -> looks up the audit row, records the signal against its complexity bucket
  -> future /route calls in that same bucket get nudged up/down a tier once signals cross a threshold
```

All persistent state (users, budget usage, audit log, feedback) lives in a
single SQLite file at `data/router.db`, created automatically on first run
and seeded from `data/users.json`.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://localhost:8000/** for the dashboard — submit a request, watch it get
routed (or blocked) with full reasoning, see live budget bars per user, give
escalate/downgrade feedback on a decision with one click, and browse the
audit trail. It's a static page (`app/static/`) served by the same FastAPI
app and talking to the JSON API below — no separate frontend build.
Interactive API docs are still at `/docs`.

Seeded demo users (`data/users.json`):

| user  | team     | monthly budget | max tier | allowed projects                    | project overrides            |
|-------|----------|-----------------|----------|--------------------------------------|-------------------------------|
| alice | platform | $50.00          | frontier | (none — unrestricted)                | —                              |
| bob   | support  | $1.00           | mid      | (none — unrestricted)                | —                              |
| carol | platform | $20.00          | mid      | internal-tools, growth-experiments   | internal-tools -> frontier     |

### Project authorization is two-sided

Two independent checks both have to pass, from two different config sources:

- **Per-user** (`allowed_projects` in `data/users.json`) — "must *this user* pick from a specific
  set of projects." A user with an empty list is unrestricted at this level and can reference any
  (or no) project.
- **Per-project** (`data/projects.json`, `app/projects.py`) — "who's allowed to touch *this project*
  at all." This applies to *every* user, regardless of whether they have their own restriction list.
  A project not present in this registry is "open" — nobody has declared it restricted, so
  referencing it doesn't add any extra rule.

This matters because a project isn't "owned" by whoever happens to have it in their personal
allowlist — e.g. `internal-tools` appearing in carol's `allowed_projects` doesn't stop any other
(otherwise-unrestricted) user from typing that same project name unless it's *also* registered in
`data/projects.json` with an explicit `authorized_users` list. Both seed files currently agree
(carol is the only one listed for `internal-tools`/`growth-experiments` in both places), which is
what makes those two projects actually restricted to her.

### Routing modes

`routing_mode` on `POST /route` is `"balanced"` (default), `"quality"`, or `"cost"`. It only
changes *preference*, not governance — off-policy blocks, hard budget blocks, and tier caps/overrides
apply identically in every mode:

- **Cost** pre-emptively steps the classifier's suggested tier down one level before anything else
  runs, on top of whatever budget-driven downgrades still apply.
- **Quality** skips the soft-budget-limit downgrade entirely (still respects the hard block).
- **Balanced** is today's default behavior — no mode-specific adjustment.

### Automatic failover

If the tier a request was routed to is "unavailable," the router transparently retries at the next
tier down (frontier → mid → cheap) rather than failing the request, and the response's `final_tier`
and `reason` reflect what actually served it. In simulated mode there's no such thing as a real
outage, so you trigger one on demand: pass `[[simulate-outage:<tier>]]` (e.g.
`[[simulate-outage:frontier]]`) anywhere in the prompt text (works from the dashboard too — no
restart needed), or construct `MockModelClient(force_fail_tiers={...})` directly in code/tests. In
real (Groq) mode, the same marker still works (checked before any network call), *and* genuine
transient errors (rate limits, timeouts, 5xx) trigger the same failover path automatically — see
"Real model serving" below.

### Real model serving (Groq)

By default the `cheap`/`mid`/`frontier` tiers are simulated (`app/models/mock_client.py`) —
fake cost, fake latency, canned output text. Set `GROQ_API_KEY` and `RouteOrchestrator` switches
to `app/models/groq_client.py` automatically, which maps the three tiers to three real
Groq-hosted models with real per-token pricing:

| Tier | Model | Input / Output $ per 1M tokens |
|---|---|---|
| cheap | `openai/gpt-oss-20b` | $0.075 / $0.30 |
| mid | `openai/gpt-oss-120b` | $0.15 / $0.60 |
| frontier | `qwen/qwen3.8-27b` | $0.80 / $4.00 (reasoning model) |

> **Why these three, and a caveat.** The ladder originally ran
> `llama-3.1-8b-instant` → `gpt-oss-20b` → `gpt-oss-120b`. Groq has since moved the Llama
> models off self-serve, so `llama-3.1-8b-instant` returns a 404 and the cheap tier had to be
> rebuilt from what's actually served. The current ladder is strictly increasing in price
> (13x from cheap to frontier on output), which is what the routing argument needs. The
> caveat: `qwen/qwen3.8-27b` is a *preview* model on Groq, labelled for evaluation rather
> than production — it's the only served model meaningfully more capable and more expensive
> than `gpt-oss-120b`, so it fills the frontier slot, but a production deployment would want
> a production-tier frontier model.
>
> `GroqModelClient.verify_models()` checks every configured model ID against the live model
> list, so a retirement like the Llama one fails loudly at startup instead of 404-ing partway
> through a benchmark run. Both benchmark scripts call it before doing any work.

```bash
cp .env.example .env
# edit .env and set GROQ_API_KEY=gsk_... (get one at console.groq.com)
```

`output_text`, `cost_usd`, and `latency_ms` in the response are then genuine — real generated
text, real token usage from the API response, real wall-clock latency. `model_name` reports the
exact model that served the request (previously always said `"MockModelClient"`).

Automatic failover (above) now does double duty: `GroqModelClient` converts any `groq.APIError`
(rate limit, timeout, connection error, 5xx) into the same `ModelUnavailableError` the mock
backend raises, so a real transient failure steps down a tier exactly like the simulated
`[[simulate-outage:<tier>]]` marker does — which still works in real mode too (checked before
any network call, and stripped from the prompt actually sent to Groq).

Without a key, everything falls back to the simulated backend exactly as before — this is also
what the test suite runs under, so tests never make a real network call (they either construct
`MockModelClient` explicitly, or a `groq.APIError` is injected via `monkeypatch` to test the
failover-mapping logic without touching the network).

### Real LLM classification

`app/classifier.py` stays a fast, pure heuristic — no network calls, fully unit-testable. But when it
lands on `medium` complexity with **zero** matched signal either way (genuinely ambiguous, not just
"happened to be medium"), `app/llm_classifier.py` makes one real call to `claude-haiku-4-5` to ask for
a second opinion, and overrides the tier if the model disagrees. Requires `ANTHROPIC_API_KEY`:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

Without a key, `LLMClassifier` is a no-op (`enabled = False`) and the router runs on the heuristic
alone — this is also what the test suite runs under, so tests never make a real network call, even if
your shell happens to have `ANTHROPIC_API_KEY` exported for something else (the test fixtures
construct `LLMClassifier(api_key=None)` explicitly to guarantee this).

### Conversation memory

By default every `POST /route` call is stateless — a follow-up like "now make that async" gets
answered with zero knowledge of what "that" refers to, because each call is a genuinely
independent, context-free request. Passing `conversation_id` fixes this: prior turns get threaded
into the model call as real chat history.

- **First call**: omit `conversation_id` (or pass `null`). If allowed, the response includes a
  freshly minted `conversation_id` — save it to continue the thread.
- **Follow-up calls**: pass that same `conversation_id` back. The prior turns (your prompts + the
  model's real replies) get prepended to the model call, so follow-ups actually work.
- **Each turn is still independently policy-checked** — its own classification, budget check,
  tier decision, and audit row. A conversation is shared *message history for the model call*, not
  a single policy unit, so tier can legitimately change turn to turn (turn 1 might be `cheap`,
  turn 3 `frontier`, if the questions warrant it).
- **Conversations are scoped to the user who started them.** Passing someone else's
  `conversation_id` doesn't leak their history — it's blocked with a clear reason
  (`ConversationAccessError` in `app/conversations.py`), the same way an off-policy prompt or an
  unauthorized project is blocked. This is deliberate: a policy-aware router shouldn't have a
  cross-user data leak sitting in its one stateful feature.
- Blocked turns are never appended to conversation history — there's no real completion to
  represent, so nothing gets threaded into the *next* turn's model call.
- **History is capped to the last 20 messages** (`MAX_HISTORY_MESSAGES`). Every turn re-sends
  the whole history as prompt tokens, so an uncapped conversation makes per-turn cost climb
  with conversation length — which would make the router's one stateful feature the most
  expensive thing it serves. The window makes per-turn cost flat instead of growing.

### Budget enforcement

Two things here are easy to get wrong, and the first version got both wrong.

**The month actually means a month.** Spend is summed over the current calendar month only
(`created_at >= month_start`). Summing the whole table instead turns `monthly_budget_usd` into a
lifetime cap — a user who hits it is blocked permanently, and the limit never resets — while the
block message still says "monthly budget", so the bug reads as correct behaviour.

**Concurrent requests reserve before they spend.** A check-then-spend ledger is a race: N
simultaneous requests all read the same pre-spend total, all pass the limit check, and together
blow past the cap. Instead, an allowed request calls `reserve()`, which takes the SQLite write
lock (`BEGIN IMMEDIATE`), re-checks the limit, and writes a *pending* row holding the call's
worst-case cost. After the model returns, `settle()` rewrites that row with the real cost;
`release()` drops it if every tier failed. In-flight spend is therefore visible to every other
request. With 8 concurrent requests against a $1.00 cap each reserving $1.00, the old code
granted all 8 and committed $8.00; the reservation grants exactly one
(`test_concurrent_requests_cannot_overshoot_the_hard_limit`).

Reservations are worst-case by construction — output is billed at the full completion-token cap,
since the real length isn't known until the call returns. Over-reserving is safe because the
reservation settles down to the true cost; under-reserving would reopen the hole.

The dashboard surfaces this as a "Continue this conversation" checkbox (checked by default once a
conversation exists) and renders the growing thread above the latest turn's full detail.

```bash
first=$(curl -s -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "remember the number 42"}')
cid=$(echo "$first" | python3 -c "import json,sys; print(json.load(sys.stdin)['conversation_id'])")

curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d "{\"user_id\": \"alice\", \"prompt\": \"what number did I just tell you?\", \"conversation_id\": \"$cid\"}"
  # -> real Groq response correctly references 42

# bob trying to continue alice's conversation -> blocked, not leaked
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d "{\"user_id\": \"bob\", \"prompt\": \"what did alice say?\", \"conversation_id\": \"$cid\"}"
```

Try it:

```bash
# Normal work request -> routed to an appropriate tier
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "refactor this function to be async"}'

# Off-policy (personal use) -> blocked
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "write my personal cover letter"}'

# carol needs a project, and only has access to two of them
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "carol", "prompt": "fix the bug in checkout"}'                                    # blocked: no project given
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "carol", "prompt": "fix the bug in checkout", "project": "some-other-repo"}'        # blocked: not authorized
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "carol", "prompt": "design the architecture for a migration", "project": "internal-tools"}'       # frontier (project override)
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "carol", "prompt": "design the architecture for a migration", "project": "growth-experiments"}'   # mid (default cap)

# alice has no restrictions of her own, but internal-tools is registered to carol only -> still blocked
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "fix a bug", "project": "internal-tools"}'

# See which projects are registered as restricted, and to whom
curl localhost:8000/projects

# Routing modes: same prompt, different tiers
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "design the architecture for a migration", "routing_mode": "cost"}'      # mid (stepped down)
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "design the architecture for a migration", "routing_mode": "quality"}'   # frontier, ignores soft-budget downgrades

# Automatic failover: force the frontier tier "unavailable" for this request
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "design the architecture for a migration [[simulate-outage:frontier]]"}'  # served at mid, reason explains the failover

# With GROQ_API_KEY set: output_text/cost_usd/latency_ms/model_name are all real
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "explain what a race condition is in one sentence"}'

# bob has a $1.00 budget -- repeat a few times to see downgrade, then block
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "bob", "prompt": "design the architecture for a distributed migration"}'

# Feedback loop: route a medium-complexity prompt, then tell the router it under-routed
curl -X POST localhost:8000/route -H 'content-type: application/json' \
  -d '{"user_id": "alice", "prompt": "update the config for the staging environment"}'
# ^ note the "audit_id" in the response, then:
curl -X POST localhost:8000/feedback -H 'content-type: application/json' \
  -d '{"audit_id": 1, "signal": "escalate"}'
# after two escalate signals against the same complexity bucket, that bucket's suggested tier bumps up one level

# Inspect the decision trail
curl localhost:8000/audit

# Check remaining budget for a user
curl localhost:8000/users/bob/budget
```

## Test

```bash
pytest tests/
```

## Benchmark

`scripts/benchmark_classifier.py` evaluates the heuristic classifier (in isolation from the
policy/budget layer) against a 30-prompt hand-labeled oracle set (10 each cheap/mid/frontier),
using **real Groq API calls** for cost and latency — not estimates:

```bash
cp .env.example .env   # set GROQ_API_KEY
python scripts/benchmark_classifier.py
```

Measured result (one run; real per-token costs vary slightly run to run with output length):

| Metric | Result |
|---|---|
| Accuracy vs. hand-labeled oracle | **100%** (30/30) |
| Cost vs. always routing to frontier | **28.1% cheaper** |
| Avg. real latency per request | **~10s** |

Latency is dominated by the frontier tier being a *reasoning* model (`qwen/qwen3.8-27b`), which
spends tokens thinking before it answers. That is a real cost of the current tier ladder and not
a measurement artifact: the earlier ladder, whose frontier was `gpt-oss-120b`, averaged ~1–1.6s.
Routing away from frontier therefore buys latency as well as money — a dimension this benchmark
records but doesn't yet score.

This measures the pure keyword heuristic with the feedback loop and LLM-classification fallback
both switched off — i.e. a floor, not the ceiling. In real usage both of those mechanisms exist to
correct whatever the heuristic still gets wrong, without needing to touch the keyword lists by hand.

**Iteration history, and an honest wrinkle worth keeping:** the first version of this heuristic
scored 80% accuracy and *41–47%* cost savings. Fixing the three real gaps behind the six misses
— `"explain"` matching debugging questions ("explain why this query is slow") as well as
conceptual ones, no verb form of "architecture" (`"architect a system"` matched nothing), and no
`"microservices"`/`"monolith"` signal — brought accuracy to 100%, but the savings number dropped
to 27.9%. That's not a regression: some of the original "savings" came from *misclassifying*
mid-complexity prompts as cheap, which is under-serving a request, not a real win. The honest
number went down because it stopped being inflated by wrong answers. This is exactly the kind of
result a 30-prompt, single-person-labeled eval set will produce — a larger or adversarially-chosen
set would be a stronger claim than this one, and the eval set and label judgments are both sitting
in `scripts/benchmark_classifier.py` for anyone to disagree with.

### Does routing down actually cost quality?

The benchmark above has a blind spot worth being explicit about: it scores the classifier against
**tier labels I wrote myself**. That answers "did the heuristic agree with me?" — not "was the
cheaper answer good enough?" A router that sent every prompt to the cheapest model would look
terrible on that metric and might still be fine in practice; one that agreed with my labels
perfectly could still be quietly under-serving every request. Cost savings without a quality
denominator is half a number.

`scripts/benchmark_quality.py` supplies the other half. For each prompt it calls the tier the
router chose *and* the frontier tier, then shows both answers to a judge model in randomized
order and asks which better serves the request:

```bash
python scripts/benchmark_quality.py
```

| Metric | Result |
|---|---|
| Quality held vs. always-frontier | **93.3%** (28/30) |
| Cost vs. always routing to frontier | **33.2% cheaper** |

Judge verdicts: 13 ties, 5 wins for the *cheaper* answer, 10 already routed to frontier, and
2 losses. Both losses were `mid`-tier prompts — "update the database config for the staging
environment" and "add input validation to this form handler" — where the frontier model's extra
depth genuinely helped.

Caveats, stated plainly: it's one judge, not a panel, and the judge is the same model family
that produces the frontier answers, which is a real bias risk even with the comparison blinded.
The order of the two answers is randomized per prompt because judge models systematically favour
whichever they read first. 30 prompts is small. The honest version of the headline is
*"33.2% cheaper at 93.3% quality retention (n=30, single judge, pairwise)"* — and the savings
figure differs from the 28.1% above because output lengths, and therefore real token costs, vary
between runs.

## Project layout

```
app/
  static/          Dashboard UI: index.html / style.css / app.js, served at "/"
  main.py          FastAPI app: POST /route, POST /feedback, GET /audit, GET /users, GET /projects, GET /users/{id}/budget
  schemas.py        Shared Pydantic models
  db.py               Shared SQLite db path + timestamp helper
  identity.py           User/team registry, SQLite-backed, seeded from data/users.json
  projects.py            Project registry (who's authorized per project), seeded from data/projects.json
  classifier.py             Heuristic complexity + off-policy classifier, feedback-biasable
  llm_classifier.py           Real Claude Haiku fallback for ambiguous prompts; no-ops without a key
  budget.py                     Per-user monthly spend ledger (SQLite); reserve -> settle
  policy.py                       Policy engine: off-policy > hard-limit > per-user project allowlist >
                                   per-project authorization > tier cap/override > routing-mode
                                   adjustment > soft-limit downgrade
  feedback.py                       Escalate/downgrade signal store, keyed by complexity bucket
  conversations.py                    Per-user-scoped conversation history (SQLite), for multi-turn threading
  router.py                             Orchestrator wiring the full request + feedback + failover + conversation flow
  audit.py                                SQLite audit log, returns an id per decision
  models/
    mock_client.py                        Simulated model tiers (cheap/mid/frontier), simulated outages
    groq_client.py                        Real Groq-backed tiers; enabled automatically if GROQ_API_KEY is set
data/
  users.json                                Human-editable seed data for demo users
  projects.json                               Human-editable seed data for restricted projects
  router.db                                     Created at runtime (gitignored)
scripts/
  benchmark_classifier.py                         Real-Groq-call benchmark vs. a hand-labeled oracle set
  benchmark_quality.py                            Judge-scored: does routing down actually cost quality?
.env.example                                      Copy to .env to enable real model serving + classification
```

## Follow-ups

- **Not RAG.** Conversation memory (above) threads *prior turns of the same conversation* into the
  model call. It does not retrieve from any external knowledge base/document store — there's no
  vector index or retrieval step anywhere in this project. That would be a separate, orthogonal
  system sitting upstream of the router, not a natural extension of it.
- Add more providers (OpenAI, Anthropic, etc.) behind the same `call()` interface as
  `groq_client.py`, so tiers can mix providers instead of being Groq-only — [LiteLLM](https://github.com/BerriAI/litellm)
  is the natural way to do this without hand-rolling a client per provider.
- Benchmark routing accuracy against an oracle on a SWE-bench-lite style eval set, now that
  both the classifier and the serving path make real calls to benchmark against.
- Feedback loop currently biases at the complexity-bucket level; a richer
  version could key off matched keywords, per-user history, or a learned
  embedding similarity instead.
- Model subset / configurable model pool (Azure Foundry's other flagship feature) — restrict which
  tiers are eligible for routing per team or policy, independent of budget/tier-cap logic.

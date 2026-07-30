# Model Router

A policy-aware routing layer for LLM requests. Instead of just picking "cheap
model vs. frontier model" based on prompt complexity (which RouteLLM,
LiteLLM, Not Diamond, etc. already do), this adds the layer those tools
don't: **who is asking**, **what project they're working on**, **what
they're allowed to spend**, **whether the request is even legitimate work
use**, and a **feedback loop** that lets routing decisions improve over
time — with a full audit trail explaining every decision.

All six components from the original design are implemented:

1. **Classifier** (`app/classifier.py`) — heuristic complexity/off-policy scorer
2. **Identity & context** (`app/identity.py`) — users, teams, budgets, project-level access
3. **Policy engine** (`app/policy.py`) — off-policy block, budget block, project authorization, tier caps
4. **Budget ledger** (`app/budget.py`) — per-user spend tracking with soft/hard limits
5. **Audit trail** (`app/audit.py`) — full decision trace for every request
6. **Feedback loop** (`app/feedback.py`) — escalate/downgrade signals bias future classifier suggestions

The model backend (`app/models/mock_client.py`) is still simulated — real
LiteLLM/provider wiring is the next step, see "Follow-ups" below.

## How a request flows

```
POST /route {user_id, prompt, project?}
  -> identity lookup       (team, budget, max tier, allowed projects, per-project tier overrides)
  -> classifier             (heuristic complexity scorer, biased by past /feedback signals)
  -> budget check            (spent vs. monthly limit -> soft/hard limit flags)
  -> policy engine             (off-policy? hard limit? user allowed on this project? project itself
                                restricted to other users? tier cap/override? soft-limit downgrade?)
  -> mock model call             (simulated cost/latency per tier)
  -> budget ledger update
  -> audit log                     (SQLite row: full decision trace, returns an audit_id)

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
  budget.py                   Per-user spend ledger (SQLite) with soft/hard limits
  policy.py                     Policy engine: off-policy > hard-limit > per-user project allowlist >
                                 per-project authorization > tier cap/override > soft-limit downgrade
  feedback.py                     Escalate/downgrade signal store, keyed by complexity bucket
  router.py                         Orchestrator wiring the full request + feedback flow
  audit.py                            SQLite audit log, returns an id per decision
  models/mock_client.py                 Simulated model tiers (cheap/mid/frontier) with fake cost & latency
data/
  users.json                              Human-editable seed data for demo users
  projects.json                             Human-editable seed data for restricted projects
  router.db                                   Created at runtime (gitignored)
```

## Follow-ups

- Swap `mock_client.py` for [LiteLLM](https://github.com/BerriAI/litellm) + real provider keys.
- Replace the heuristic classifier with a trained/embedding-based one
  ([RouteLLM](https://github.com/lm-sys/RouteLLM)-style), and benchmark
  routing accuracy against an oracle on a SWE-bench-lite style eval set.
- Feedback loop currently biases at the complexity-bucket level; a richer
  version could key off matched keywords, per-user history, or a learned
  embedding similarity instead.

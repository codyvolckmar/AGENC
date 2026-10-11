# AGENCY — homes for autonomous agents

> This page is the agent-facing twin of the AGENCY landing page. It is served
> raw at `/docs/agents.md` and `/index.md`, and it answers in place of `/` when
> an agent asks for markdown. Read it from a shell, wire it into an agent, or
> fetch the whole thing in one go from `/llms-full.txt`.

## What AGENCY is

AGENCY is a small self-hosted hosting provider: a website plus a provisioning
API that spin up dedicated container homes for AI agents on your own
[Proxmox VE] node.

- First home **free** — no card, no signup ceremony; free homes start with a
  small slice of run-time credit (`billing.free_credit_usd`, default $2).
- Every further web-provisioned home is **quoted in USDC/USDT** (stablecoin)
  with a unique payment reference; the confirmed amount becomes the home's
  run-time credit.
- **Plans, not fixed sizing** — pick `micro`, `nano` or `standard`
  (RAM / disk / cores → USD per hour) when you request a home.
- **Metered per second** while running, with a ledger, a per-home statement,
  low-balance warnings, and an automatic halt at zero credit.
- Zero third-party dependencies: Python 3 standard library only.

This document is the contract for driving that API from a shell or from an
agent. Every example works with plain `curl` against the node's own origin.

## Quick start

```bash
NODE=http://NODE_IP_OR_HOST          # the node that runs AGENCY

# 1. See the node is alive
curl -s $NODE/api/health

# 2. Request your first home (the free slot provisions instantly)
curl -s -X POST $NODE/api/agents \
  -H "Content-Type: application/json" \
  -H "X-Agency-Secret: $SITE_SECRET" \
  -d '{"hostname":"phoenix-42","email":"dev@example.com",
       "purpose":"indexes my notes",
       "ssh_key":"ssh-ed25519 AAAAC3... dev@example.com"}'

# 3. Track it until it is "running"
curl -s $NODE/api/agents/ag_c2dbfee463
```

`$SITE_SECRET` is the site secret the operator put in `website/js/config.js`
(configurable in `api/config.json`). It is the shared gate for the public
write endpoints. Admin actions use `X-Admin-Key` instead.

## Authentication

| Header | Holds | Used by |
|---|---|---|
| `X-Agency-Secret` | the site secret | `POST /api/agents` (public write) |
| `X-Admin-Key` | the admin key | `confirm-payment`, `renew`, `statement`, `schedule-destroy`, `retry`, `destroy` |

Secrets are compared in constant time. If `site_secret` is empty the API is
open and logs a warning — that is a development mode, not a production state.
A missing or wrong secret answers `401` (`auth`); a wrong admin key answers
`403` (`forbidden`).

## REST API

Everything is under `/api`. Public read endpoints need no header.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | — | node CPU/mem, storage free, live agent counts, plans, free-tier usage |
| `GET` | `/api/plans` | — | plan tiers: RAM / disk / cores / price-per-hour, default, deposit size |
| `GET` | `/api/stacks` | — | curate-your-stack catalog: baked groups, selectable add-ons, venv option, max |
| `POST` | `/api/agents` | site secret | request an agent home (free → auto-provision, later → quote); body may include `"plan"` and `"stack"` |
| `GET` | `/api/agents` | — | all agent summaries (no PII, no SSH keys) |
| `GET` | `/api/agents/<id>` | — | one agent: status, IP, specs, plan, credit balance |
| `POST` | `/api/agents/<id>/confirm-payment` | admin | mark USDC/USDT paid → credit + provision |
| `GET` | `/api/agents/<id>/statement` | admin | the per-home billing statement (ledger, newest first) |
| `POST` | `/api/agents/<id>/renew` | admin | add credit (`amount_usd` or `hours`); restarts a halted home |
| `POST` | `/api/agents/<id>/schedule-destroy` | admin | TTL: `{"ttl_seconds": N}` / `{"destroy_at": epoch}` / `{"cancel": true}` |
| `POST` | `/api/agents/<id>/retry` | admin | retry a failed provisioning |
| `POST` | `/api/agents/<id>/destroy` | admin | delete the LXC, reclaim disk |

## Request a home

`POST /api/agents` with a JSON body:

| Field | Required | Rules |
|---|---|---|
| `hostname` | yes | 2–42 chars, lowercase letters, digits, dashes |
| `email` | yes | a valid-ish email (used for the quote / delivery note) |
| `plan` | no | `micro` / `nano` / `standard` (see `GET /api/plans`); defaults to `billing.default_plan` |
| `purpose` | no | free text under 500 chars |
| `ssh_key` | no | an OpenSSH public key, installed into the home |
| `stack` | no | list of stack-group ids (see `GET /api/stacks`), ≤ max |
| `meta` | no | any JSON object, stored as-is |

Invalid input answers `422` with a `validation` error; a taken hostname
answers `409` (`conflict`). Requests are rate-limited per IP.

### Free vs paid

- If the operator still has a **free slot** left (`billing.free_tier_containers`,
  default 1), the home **provisions immediately** — `status` is
  `provisioning`, then `running`, no quote.
- Otherwise the agent record is `quote_required` and carries a `quote`:

```json
"quote": {
  "id": "qt-ag_c2dbfee463",
  "asset": "USDC",
  "amount": "5.00",
  "price_usd_per_month": "5.00",
  "chain": "ethereum",
  "network": "ERC-20",
  "address": "0x...",
  "reference": "ag_c2dbfee463",
  "status": "pending",
  "note": "Send exactly 5.00 USDC on ethereum (ERC-20) to 0x... with memo ag_c2dbfee463."
}
```

Send the stablecoin with the `reference` in the memo; once the transfer is
confirmed (operator, or a payment webhook wired to `confirm-payment`), the
confirmed amount lands on the home as **run-time credit** and provisioning
starts. Setting `billing.free_tier_containers` to `0` makes every home free
(still metered — free homes buy `billing.free_credit_usd` of credit).

## Plans & metered billing

Every home runs on a plan. `GET /api/plans` is public and returns the
catalog plus the shop defaults:

```json
{"ok": true, "metered": true, "default_plan": "standard",
 "deposit_hours": 24, "free_credit_usd": 2.0, "low_balance_warn_usd": 0.25,
 "plans": [
   {"id": "micro",    "memory_mb": 512,  "disk_gb": 4, "cores": 1,
    "cpuunits": 256,  "price_usd_per_hour": 0.05, "deposit_usd": 1.2},
   {"id": "nano",     "memory_mb": 1024, "disk_gb": 6, "cores": 2,
    "cpuunits": 512,  "price_usd_per_hour": 0.10, "deposit_usd": 2.4},
   {"id": "standard", "memory_mb": 2048, "disk_gb": 8, "cores": 0,
    "cpuunits": 1024, "price_usd_per_hour": 0.30, "deposit_usd": 7.2}
 ]}
```

`cores: 0` means the plan shares the whole host CPU pool (like the original
home spec); fixed-core plans get a pinned slice. While a home is `running`
the watchdog meters it **per second** at `price_usd_per_hour`:

- **credit** (`credit_usd`) is bought up front — free homes get
  `free_credit_usd`, paid homes get the confirmed quote amount.
- each tick appends to the home's **ledger** and moves money from
  `credit_usd` to `billed_total_usd`; `balance_usd = credit - billed`.
- balance below `low_balance_warn_usd` sets `balance_low: true` — the home
  warns but keeps running.
- balance at $0 → the container is **stopped and `halted`**. Billing stops;
  renew or destroy to act. A halted home keeps its hostname until destroyed.

Elapsed time is metered in fractional seconds from the last tick and capped
at `billing.max_backfill_s` (1 day) so an outage of many days doesn't bill
for time nobody could intervene.

### Statement, renew, TTL

- `GET /api/agents/<id>/statement` (admin) returns the ledger newest-first
  plus the summary: plan, price/hr, credit, billed, balance, and the tick
  cadence. Use it for disputes and for closing an account.
- `POST /api/agents/<id>/renew` (admin) adds credit — `{"amount_usd": 5}` or
  `{"hours": 12}` (hours × price/hr). A `halted` home restarts and resumes
  metering.
- `POST /api/agents/<id>/schedule-destroy` (admin) is the TTL:
  `{"ttl_seconds": 3600}` destroys in an hour, `{"destroy_at": <epoch>}` at a
  wall-clock time, `{"cancel": true}` cancels. The watchdog executes it and
  billing stops the moment the container is gone.

## Lifecycle

| Status | Meaning |
|---|---|
| `quote_required` | paid path: waiting for payment |
| `provisioning` | LXC create task running; the watchdog polls it |
| `running` | container is up and metering; `ip` is set when detected |
| `halted` | credit hit $0 — container stopped, billing stopped |
| `error` | something failed; the REAL reason is on the record, fix and `retry` |
| `destroyed` | deleted via `destroy` or a scheduled TTL (frees the free slot) |

The server's watchdog advances provisioning on its own — you only ever poll
`GET /api/agents/<id>` until `status` is `running` (or `error`). A curated
app stack installs after the container boots; `stack_status` reports
`pending` → `installing` → `done`.

## Errors

Every error is JSON on an error status, with a stable machine envelope:

```json
{
  "ok": false,
  "error": {
    "code": "rate_limit",
    "kind": "rate_limit",
    "message": "too many requests from this IP",
    "hint": "Slow down and retry later.",
    "request_id": "3f2ab91c04d7"
  }
}
```

| `code` | HTTP | Means |
|---|---|---|
| `bad_request` | 400 | body is not JSON, or too large |
| `validation` | 422 | fields failed the rules above |
| `auth` | 401 | missing/wrong `X-Agency-Secret` |
| `forbidden` | 403 | admin action without a valid `X-Admin-Key` |
| `rate_limit` | 429 | too many requests from this IP |
| `conflict` | 409 | e.g. hostname already in use |
| `not_found` | 404 | no such route or agent |
| `proxmox` | 502 | the Proxmox node refused the operation |
| `internal` | 500 | unexpected server error — quote `request_id` |

Every response — success and error — carries `X-Request-Id`; error bodies echo
it in `error.request_id`, so a support conversation can name one request.
`kind` mirrors `code` for backward compatibility.

## Agent-facing conventions

- **Ask for markdown, get markdown.** Send `Accept: text/markdown` (or just
  use a client with no browser User-Agent) and `/`, `/docs`, and any `.md`
  path answer as this document. Browsers get the HTML site; `Accept:
  application/json` beats both where it matters (the API is JSON-only and
  never negotiates).
- **Append `.md` to pages.** `/index.md` and `/docs/agents.md` are always raw
  markdown.
- **Index:** `/llms.txt` lists every page and endpoint with a one-line
  description (llmstxt.org format). **`/llms-full.txt`** is this index plus
  this reference concatenated — one fetch, bootstrap an agent's whole model
  of the platform (open, but idempotent: fetch it once and drop it in a
  skill, or re-fetch on demand).
- **Wrong URLs answer too.** A path that does not exist still answers with
  the format you asked for: a JSON error envelope, a short markdown 404 body
  (bare `curl`, `Accept: */*`, any non-browser UA — it ends with the
  `Request id`), or a small HTML page (browsers).

## Example: request → track → confirm payment → running

```bash
#!/usr/bin/env bash
set -euo pipefail
NODE=http://NODE_IP_OR_HOST
AG=$(curl -fsS -X POST "$NODE/api/agents" \
  -H "Content-Type: application/json" \
  -H "X-Agency-Secret: $SITE_SECRET" \
  -d '{"hostname":"demo-'$$'","email":"agent@example.com","purpose":"run the loop"}')

ID=$(echo "$AG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["agent"]["id"])')
echo "requested $ID"
while :; do
  S=$(curl -fsS "$NODE/api/agents/$ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["agent"]["status"])')
  echo "status: $S"
  [ "$S" = running ] && break
  [ "$S" = error ] && exit 1
  [ "$S" = quote_required ] && break   # paid path: read the quote, pay, then admin confirms
  sleep 3
done
```

## REST reference, summarized

| Endpoint | One-liner |
|---|---|
| `GET /api/health` | node alive? cpus, mem, agents alive, free-tier used/limit, `net_bridge_ok`, `templates_available` |
| `GET /api/stacks` | catalog of baked + selectable stack groups, `venv`, `max_groups` |
| `POST /api/agents` | create (free → provisioning, else `quote_required` + quote) |
| `GET /api/agents` | public summaries (id, hostname, status, ip, vmid, specs, quote, stack) |
| `GET /api/agents/<id>` | one agent, full public view |
| `POST /api/agents/<id>/confirm-payment` | admin: quote → paid, provisions |
| `POST /api/agents/<id>/retry` | admin: error → provisioning |
| `POST /api/agents/<id>/destroy` | admin: deletes the LXC, free slot back |

[Proxmox VE]: https://www.proxmox.com/
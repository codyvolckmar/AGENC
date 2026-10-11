# AGENCY — homes for autonomous agents

> This page is the agent-facing twin of the AGENCY landing page. It is served
> raw at `/docs/agents.md` and `/index.md`, and it answers in place of `/` when
> an agent asks for markdown. Read it from a shell, wire it into an agent, or
> fetch the whole thing in one go from `/llms-full.txt`.

## What AGENCY is

AGENCY is a small self-hosted hosting provider: a website plus a provisioning
API that spin up dedicated container homes for AI agents on your own
[Proxmox VE] node.

- First home **free** — no card, no signup ceremony; free homes come with
  the first **prepaid period** on us (`billing.free_periods`).
- Every further web-provisioned home is **quoted in USDC/USDT** (stablecoin)
  with a unique payment reference; the confirmed amount prepays one billing
  period.
- **Plans, not fixed sizing** — pick `micro`, `nano` or `standard`
  (RAM / disk / cores → USD per month) and a billing period.
- **Pay up front, weekly / fortnightly / monthly.** Each home has a prepaid
  window (`paid_until`); it warns as the window runs out and halts when it
  lapses — nothing is metered per second.
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
| `period` | no | `weekly` / `fortnightly` / `monthly` — the prepaid window; defaults to `billing.default_period` |
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
home gets a prepaid window of the quoted period and provisioning starts.
Setting `billing.free_tier_containers` to `0` makes every home free — they
still get `free_periods` prepaid periods. Dev mode (no expiry at all): set
`free_tier_containers: 0` **and** `free_periods: 0`.

## Plans & prepaid periods

`GET /api/plans` is public: the plan catalog, the periods, and the shop
defaults. Pricing is **per month**, mainstream-competitive:

```json
{"ok": true, "default_plan": "standard", "default_period": "monthly",
 "free_periods": 1, "warn_before_s": 86400,
 "periods": {
   "weekly":      {"days": 7,  "months": 0.25},
   "fortnightly": {"days": 14, "months": 0.5},
   "monthly":     {"days": 30, "months": 1.0}},
 "plans": [
   {"id": "micro", "memory_mb": 1024, "disk_gb": 30, "cores": 1,
    "cpuunits": 256,  "price_usd_per_month": 8.0,
    "price_usd_per_hour_equiv": 0.011,
    "period_prices": {"weekly": 2.0, "fortnightly": 4.0, "monthly": 8.0}},
   {"id": "nano", "memory_mb": 4096, "disk_gb": 50, "cores": 4,
    "cpuunits": 1024, "price_usd_per_month": 16.0,
    "period_prices": {"weekly": 4.0, "fortnightly": 8.0, "monthly": 16.0}},
   {"id": "standard", "memory_mb": 8192, "disk_gb": 64, "cores": 8,
    "cpuunits": 2048, "price_usd_per_month": 32.0,
    "price_usd_per_hour_equiv": 0.044,
    "period_prices": {"weekly": 8.0, "fortnightly": 16.0, "monthly": 32.0}}
 ]}
```

A **period price** is the monthly rate × the period's fraction (0.25 / 0.5 /
1.0), rounded to cents. `price_usd_per_hour_equiv` is informational only
(monthly ÷ 730 h) — **nothing is billed per hour**.

While a home exists it carries a prepaid window:

- **free-tier homes** get `billing.free_periods` (default 1) periods free;
- **paid homes** get one period of their plan when the quote is confirmed;
- `paid_until` is the wall-clock deadline; `remaining_s` is how much is
  left; `billing_period`/`period_days`/`period_price_usd` describe the
  window the home was bought on.

Within `warn_before_s` (1 day) of the deadline the home is flagged
`expiring: true` — it warns but keeps running. When the deadline passes,
the watchdog **stops the container and `halt`s the home**; billing stops
with it. Renew or destroy to act. A halted home keeps its hostname until
destroyed.

### Statement, renew, TTL

- `GET /api/agents/<id>/statement` (admin) returns the ledger newest-first
  plus the summary: plan, $/mo, period, `paid_until`, `remaining_s`,
  periods bought and total billed. Use it for disputes and closing accounts.
- `POST /api/agents/<id>/renew` (admin) extends the prepaid window —
  `{"period": "weekly", "periods": 2}` buys two weeks (billed at the plan's
  weekly rate), `{"days": 10}` buys ten days pro-rated at the monthly rate.
  A `halted` home restarts and resumes the clock.
- `POST /api/agents/<id>/schedule-destroy` (admin) is the TTL:
  `{"ttl_seconds": 3600}` destroys in an hour, `{"destroy_at": <epoch>}` at a
  wall-clock time, `{"cancel": true}` cancels. The watchdog executes it and
  billing ends the moment the container is gone.

## Lifecycle

| Status | Meaning |
|---|---|
| `quote_required` | paid path: waiting for payment |
| `provisioning` | LXC create task running; the watchdog polls it |
| `running` | container is up; `ip` is set when detected |
| `halted` | prepaid period lapsed — container stopped, billing stopped |
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
# AGENCY — architecture

## Goals

1. A public website that accepts **inbound requests** and spins up small
   container homes for AI agents on a private Proxmox node.
2. **Plan-sized homes**: customers pick micro / nano / standard
   (RAM / disk / cores → USD/month, mainstream-competitive specs);
   unprivileged LXC, disk preflighted.
3. **30-day free trial, then prepaid periods**: every home provisions
   instantly and runs free for `trial_days`; at the trial's end an unpaid
   VM is **deleted** while a home that paid and lapsed is halted; renew,
   ledger + per-home statement, and TTL scheduled destroys.
4. Zero-dependency deployment on a stock Debian/Proxmox node: Python 3
   stdlib only, one systemd service, one nginx vhost.

## Components

```
website/                 static assets (index.html, css, js) — served by the API
api/
  app.py                 HTTP server: static file router + /api REST router
                         + watchdog thread that advances provisioning state,
                         meters running homes, and executes scheduled destroys
  proxmox.py             PVE JSON API client. dry_run mode simulates the node
  provisioning.py        LXC lifecycle: build params (from the plan) → create
                         → poll task → IP; scheduled destroys
  plans.py               plan tiers: RAM / disk / cores / cpuunits → $/hr
  billing.py             metered ticker, ledger, statements, renew
  quotes.py              stablecoin quote generation / confirm / cancel
  store.py               JSON-file store (atomic, lock-guarded)
proxmox/setup.sh         creates role+user+API token, downloads OS template
deploy/deploy.sh         copies code, generates secrets, installs systemd+nginx
tests/test_api.py        end-to-end tests against dry-run Proxmox
```

## Request lifecycle

```
click "Request home" (website)
        │  POST /api/agents  (X-Agency-Secret)
        ▼
validate → rate-limit → hostname-uniqueness check → plan/period check
        │
        ├─ status = provisioning, start_provision() ──► PVE create LXC
        │      (async task UPID returned)
        │
        └─ trial window opened (billing.trial_days) + quote issued:
              create_quote()  (USDC/USDT, amount, address,
                               reference = agent id)
                          │
          customer sends stablecoin on-chain with memo=reference
          (any time during the trial)
                          │
        operator/webhook  ▼
   POST /api/agents/:id/confirm-payment   (X-Admin-Key)
        │  quote = paid; paid_until extends past the trial
        ▼
watchdog thread polls PVE task every poll_interval_s
        │  GET /nodes/:node/tasks/:upid/status
        ├─ running  → keep polling
        └─ stopped/OK → fetch IP (pct exec "hostname -I") → status = running

error path: if the PVE call fails (bad token, missing template, denied
permission…), `start_provision` records the REAL reason on the agent
record (status = error, error = message). The operator fixes the cause
and calls `POST /api/agents/:id/retry` (admin) to re-run provisioning.
```

Metering and lifecycle (same watchdog, next pass):

```
status = running & paid_until set
        │  billing.tick every poll pass
        ├─ paid_until - now <= warn_before_s  → expiring = true (warns)
        └─ paid_until <= now
            ├─ never paid (billed_total == 0)
            │     → destroy_home(reason="trial expired…", claim the VM)
            └─ paid at least once
                  → px.stop_lxc(vmid); status = halted (renewable)

any non-destroyed home with destroy_scheduled_at <= now
        └─ destroy_home() — the scheduled TTL executed by the watchdog
```

Destroy (`POST /api/agents/:id/destroy`, admin key) deletes the LXC and
marks the record `destroyed`; a destroyed free home gives its slot back.

## Proxmox integration

Authentication is an **API token** (`PVEAPIToken=<user!id>=<secret>`), created
by `proxmox/setup.sh` with a privilege set that is **intersection-safe across
PVE 8 and 9** — PVE 9 removed `VM.Monitor`, so it is never requested; if
`pveum` rejects any privilege, the script retries with a reduced set
(verified by `tests/test_setup.sh` against a PVE-9-semantics fake). The
role includes **`SDN.Audit` + `SDN.Use`** so the API can inspect the
`sandbox` vnet and attach NICs to it. Calls used:

| Purpose | Endpoint |
|---|---|
| host CPU/mem | `GET /nodes/:node/status` |
| find free vmid | `GET /cluster/resources?type=vm` |
| pick template | `GET /nodes/:node/storage/:storage/content?content=vztmpl` |
| create container | `POST /nodes/:node/lxc` (params in `provisioning.build_params`) |
| poll async task | `GET /nodes/:node/tasks/:upid/status` |
| container IP | `POST /nodes/:node/lxc/:vmid/exec` (`hostname -I`) — best-effort: some tokens are denied exec; fall back to the GUI |
| destroy | `DELETE /nodes/:node/lxc/:vmid` |

### The LXC payload (business rules encoded here)

Sizing comes from the agent's plan (stored on the record at request time),
not from global config:

```
ostemplate: local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst
memory:     8192          # plan.memory_mb        (standard)
swap:       512
disk:       64            # plan.disk_gb, preflighted against storage free space
cores:      8             # plan.cores
cpuunits:   2048          # plan.cpuunits — fair-share scheduling between homes
rootfs:     local-lvm:64
net0:       name=eth0,bridge=vmbr0,firewall=0,ip=dhcp
unprivileged: 1           # security default (community-scripts style)
features:   nesting=1,keyctl=1
ssh-public-keys: <user's key + optional agency key>
tags:     agenc
onboot:   0  start: 1
```

`_disk_preflight()` stats the rootfs storage (`GET …/storage/<id>/status`)
and refuses to create when `avail` can't fit `disk_gb × (1 + headroom_pct/100)`,
failing fast instead of half-creating a container.

## Prepaid period billing (plans.py + billing.py)

Homes are created from a named plan plus a billing period
(`plans.py`): `micro`/`nano`/`standard` map RAM/disk/cores/cpuunits to a
**USD-per-month** price, and `billing.periods` maps `weekly`/`fortnightly`/
`monthly` to day counts and month-fractions (0.25/0.5/1.0). The period price
is `$/mo × fraction`; the API stores plan + period specs on the agent
record and gives the home a prepaid window (`paid_until`):

| Event | Consequence |
|---|---|
| creation | provisioned instantly + trial window: `credit_periods(1, trial_days, $0)`, `on_trial` until first payment; quote issued |
| quote confirmed | window extends past the trial: `paid_until = max(paid_until, now) + period.days`; ledger entry with the quote id |
| `paid_until - now ≤ warn_before_s` | `expiring: true` — warns, keeps running |
| trial ends unpaid | `destroy_home(reason="trial expired…")` — the VM is deleted and disk reclaimed |
| paid home lapses | `px.stop_lxc()`, status `halted`, `halted_at` set — renewed to resume |
| `renew` (period/count or days) | window extends contiguously (`base = max(paid_until, now)`); halted home restarts |
| `destroy_scheduled_at` passes | `process_schedules()` → `destroy_home()` — the TTL, executed by the watchdog |

Nothing is metered per second — the ledger records purchases (period,
`days`, `amount_usd`, note), capped at `LEDGER_CAP = 200`, and
`statement` renders it newest-first. `price_usd_per_hour_equiv` is
informational only ($/mo ÷ 730 h).

## State & safety

- **Persistence** — `api/data/agents.json` (git-ignored). Atomic writes
  (tmp + rename) behind a `RLock`; only the watchdog and handler threads
  mutate it.
- **Concurrency** — `ThreadingHTTPServer` + one watchdog thread. vmid
  selection retries on race; hostname uniqueness is enforced in the store.
- **Dry run** — `dry_run: true` short-circuits every network call with a
  simulated lifecycle (create → 3 polls → running + fake IP) and logs the
  exact payloads. This is how the test suite and demos work; flipping the
  flag is the production switch.
- **Secrets** — `site_secret` (public gate, lives in `website/js/config.js`,
  git-ignored), `admin_key` (server-side, in `config.json`, mode 600).
  Compare with `hmac.compare_digest`. Rate limit 10 requests/IP/hour.
- **Networking** — every agent NIC attaches to the `sandbox` SDN vnet:
  `bridge=` takes the vnet name directly in Proxmox SDN (a linux bridge of
  the same name also works). `Proxmox.net_exists()` is **tri-state**:
  `true` (verified present via `/cluster/sdn/vnets` + `/nodes/:node/network`),
  `false` (verified absent — blocks provisioning), or `null` (cannot tell,
  e.g. token lacks SDN privileges — proceeds optimistically).
  `/api/health` exposes `net_bridge_ok` plus `proxmox.reachable`,
  `proxmox.auth_ok` and `templates_available`; `deploy/deploy.sh` prints
  them on deploy and warns about `REPLACE_*`/zeroed payment placeholders.

## Agent-native surfaces

Implementation notes for the agent-facing contract (user-facing side is in
the README, “Agent-native surfaces”):

- **Negotiation** — `AgencyHandler._negotiated_kind()` scores explicit `Accept`
  q-values among `text/html` / `text/markdown` / `application/json`; with no
  stated preference (no header, or `*/*` alone) it falls back to the
  `User-Agent` — `Mozilla` → HTML, anything else → markdown. Ties resolve to
  HTML, browsers-style. The JSON API never negotiates.
- **Markdown twins** — `website/docs/agents.md` is the canonical agent
  reference: served raw at `/docs/agents.md` and `/index.md`, answers `/` and
  `/docs` to markdown-preferring clients, and is the reference half of
  `/llms-full.txt`. One copy of the docs — no drift.
- **`/llms-full.txt`** is assembled per request from `website/llms.txt`
  (index) + `website/docs/agents.md`, so the single-fetch contract cannot
  get stale.
- **Stable errors** — every response sets the `X-Request-Id` header;
  `error_json()` emits `{code, kind (legacy), message, hint, request_id}`
  with hints from `ERROR_HINTS`. Missing static paths go through
  `_send_error_page()`, which negotiates HTML / markdown / JSON and embeds
  the request id in the markdown body.
- **HEAD** — `do_HEAD` routes like GET with `self._head` set, which suppresses
  every body write (JSON, files, error pages) while keeping `Content-Length`
  and `Content-Type`, so `curl -I` probes work for agents and health checks.
  The API treats HEAD as GET for read endpoints.
- **Request id lifecycle** — set in `AgencyHandler.__init__` *before*
  `super().__init__()` because CPython ≥ 3.14 invokes `handle()` from inside
  `BaseRequestHandler.__init__` — a request can be answered while the
  constructor is still on the stack.

## Deployment

`deploy/deploy.sh` (done as root on the node):

1. rsync/cp the repo to `/opt/agenc`
2. generate `site_secret` + `admin_key` (openssl rand), write
   `api/config.json` from the example, plumbing the token from
   `/root/.agenc-token`
3. write `website/js/config.js` with the site secret
4. install + start `agenc-api.service` (python3, `:8080`)
5. install nginx vhost `:80 → :8080` and report the vnet preflight
   (`net_bridge_ok` from `/api/health`)
6. operator forwards router ports 80/443 → node IP

## Testing

`python3 tests/test_api.py` boots a real server in dry-run mode and walks
the whole user journey: free home auto-provisions → running with IP; paid
home gets a USDC/USDT quote → `confirm-payment` provisions it; auth,
validation, hostname conflicts, destroy and PII-stripping are all asserted.
The agent-native surfaces are covered too: `llms.txt`/`llms-full.txt`,
the markdown twins, `Accept` negotiation (markdown vs HTML vs JSON, API
never negotiates), the error envelope + `request_id`, HEAD probes and
negotiated 404s. Billing is covered end to end: plan + period catalog and
validation, quote at creation, the 30-day trial window, payment extending
past the trial, trial-end **delete** vs paid-home **halt**, expiry warning,
statements, renew (period and days), disk-space preflight failures, and
scheduled destroys (TTL, cancel, past-date rejection, admin-only).
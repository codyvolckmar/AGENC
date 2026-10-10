# AGENCY — architecture

## Goals

1. A public website that accepts **inbound requests** and spins up small
   container homes for AI agents on a private Proxmox node.
2. Strict per-agent limits: **≤ 8 GB disk**, **2 GB RAM**, **shared access
   to every host CPU** (no pinning), unprivileged LXC.
3. **Free first home**; every subsequent web-provisioned home is sold for
   **USDC/USDT** via a machine-readable quote + payment reference.
4. Zero-dependency deployment on a stock Debian/Proxmox node: Python 3
   stdlib only, one systemd service, one nginx vhost.

## Components

```
website/                 static assets (index.html, css, js) — served by the API
api/
  app.py                 HTTP server: static file router + /api REST router
                         + watchdog thread that advances provisioning state
  proxmox.py             PVE JSON API client. dry_run mode simulates the node
  provisioning.py        LXC lifecycle: build params → create → poll task → IP
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
validate → rate-limit → hostname-uniqueness check
        │
        ├─ free slot left?  ──YES──► status = provisioning
        │                              start_provision() ──► PVE create LXC
        │                              (async task UPID returned)
        └─ NO ──► status = quote_required
                   quote = create_quote()  (USDC/USDT, amount, address,
                                            reference = agent id)
                          │
          customer sends stablecoin on-chain with memo=reference
                          │
        operator/webhook  ▼
   POST /api/agents/:id/confirm-payment   (X-Admin-Key)
        │  status = paid, status = provisioning, start_provision()
        ▼
watchdog thread polls PVE task every poll_interval_s
        │  GET /nodes/:node/tasks/:upid/status
        ├─ running  → keep polling
        └─ stopped/OK → fetch IP (pct exec "hostname -I") → status = running
```

Destroy (`POST /api/agents/:id/destroy`, admin key) deletes the LXC and
marks the record `destroyed`; a destroyed free home gives its slot back.

## Proxmox integration

Authentication is an **API token** (`PVEAPIToken=<user!id>=<secret>`), created
by `proxmox/setup.sh` with a privilege set that is **intersection-safe across
PVE 8 and 9** — PVE 9 removed `VM.Monitor`, so it is never requested; if
`pveum` rejects any privilege, the script retries with a reduced set
(verified by `tests/test_setup.sh` against a PVE-9-semantics fake). Calls used:

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

```
ostemplate: local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst
memory:     2048          # 2 GB RAM per agent
swap:       512
cores:      8             # 0 in config => all host cores, shared
cpuunits:   1024          # fair-share scheduling between agents
cpulimit:   (unset)       # optional per-agent cap
rootfs:     local-lvm:8   # ≤ 8 GB disk
net0:       name=eth0,bridge=vmbr0,firewall=0,ip=dhcp
unprivileged: 1           # security default (community-scripts style)
features:   nesting=1,keyctl=1
ssh-public-keys: <user's key + optional agency key>
tags:     agenc
onboot:   0  start: 1
```

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
  the same name also works). `Proxmox.net_exists()` preflights the target
  before each provision (via `/cluster/sdn/vnets` + `/nodes/:node/network`);
  `/api/health` exposes `net_bridge_ok`; `proxmox/setup.sh` detects or
  creates the vnet (`SDN_ZONE=<zone>`).

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
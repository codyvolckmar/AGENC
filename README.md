# AGENCY

**Homes for autonomous agents.** AGENCY is a small self-hosted hosting
provider: a website plus a provisioning API that spin up dedicated
container homes for AI agents on your own [Proxmox VE] node.

- First home **free** — no card, no signup ceremony.
- Every further web-provisioned home is **quoted in USDC/USDT** (stablecoin)
  with a unique payment reference.
- Agents get **2 GB RAM**, the **full shared CPU pool**, and **up to 8 GB
  disk** to start (all configurable).
- Runs with **zero third-party dependencies** — Python 3 standard library
  only. No npm, no pip, no database.

```
     browser / client                     Proxmox node (this repo)
  ┌──────────────┐   HTTP :80      ┌──────────────────────────────────┐
  │ AGENCY site  │───────────────► │ nginx ─► api/app.py (stdlib)      │
  │ landing page │                 │   │                               │
  │ request form │                 │   ├─ serves website (static)      │
  │ live status  │◄────────────────┘   ├─ /api/* REST endpoints        │
  └──────────────┘   JSON            │   ├─ watchdog: polls LXC tasks  │
                                     │   └─ proxmox.py ─► PVE API       │
             ┌───────────────────┐   │        (API token, port 8006)    │
             │ LXC agent homes   │◄──┘                                  │
             │ vmid 1000..1999   │       2GB RAM · 8GB disk · shared CPU│
             └───────────────────┘                                      │
                                                                         │
        ┌────────────────┐                                              │
        │ USDC/USDT      │   free slot: provisions instantly            │
        │ quote + memo   │   paid home: provisions after operator       │
        └────────────────┘              confirms the on-chain transfer  │
                                                                        │
  ┌──────────────────┐                                                  │
  │ Router: port-    │  forward ports 80/443 → node IP                    │
  │ forward web ports│                                                  │
  └──────────────────┘                                                  │
```

## Repo layout

| Path | What it is |
|---|---|
| `website/` | Static site: landing, request form, live status tracker |
| `api/app.py` | HTTP server — serves the website **and** the `/api` REST API |
| `api/proxmox.py` | Proxmox VE JSON API client (API-token auth, dry-run mode) |
| `api/provisioning.py` | LXC agent-home lifecycle (create → poll task → IP) |
| `api/quotes.py` | USDC/USDT quote engine |
| `api/store.py` | Tiny JSON persistence for agent records |
| `api/config.example.json` | All knobs: node, storage, limits, billing |
| `proxmox/setup.sh` | One-shot node setup: least-privilege API token + OS template |
| `deploy/deploy.sh` | Install as systemd service, generate secrets, wire nginx |
| `tests/test_api.py` | End-to-end tests against dry-run Proxmox |

## Quick start

### 1. On the Proxmox node — create the API token & template

```bash
git clone https://github.com/codyvolckmar/AGENC.git /opt/AGENC-src
cd /opt/AGENC-src
./proxmox/setup.sh                  # creates role agenc-bot@pve + token + ubuntu template
```

This writes the token secret to `/root/.agenc-token` (mode 600). Container
templates are the same unprivileged-LXC builds used by
[community-scripts.org](https://community-scripts.org/scripts) (see
[“Templates”](#templates)).

### 2. Configure (optional — usually nothing to do)

`deploy/deploy.sh` generates `/opt/agenc/api/config.json` with random
`site_secret`/`admin_key` and wires the Proxmox token from
`/root/.agenc-token` **automatically** — `setup.sh` verifies the token
against PVE on every run and rotates it if stale, so there is no manual
`cp`/`sed` of the config. To customize later, edit **`/opt/agenc/api/config.json`**
(the deployed copy, not the repo's) and `systemctl restart agenc-api`:

```json
// /opt/agenc/api/config.json
"proxmox":   { "node": "pve", "rootfs_storage": "local-lvm", ... }
"container": { "disk_gb": 8, "memory_mb": 2048, "cores": 0, ... }
"billing":   { "receive_addresses": { "USDC": "0x…", "USDT": "0x…" }, ... }
```

### 3. Deploy

```bash
./deploy/deploy.sh /opt/AGENC-src
```

Installs `/opt/agenc`, generates a random `site_secret` + `admin_key`,
starts the `agenc-api` systemd unit on `:8080`, and puts nginx in front on
`:80`. Point your router's port-forward rules for ports 80/443 at this
node's IP — that's the "forward the web ports to the server" part.

> **Config lives at `/opt/agenc/api/config.json`** — edit *that* file (it's
> the one the service reads; edits to the repo's `api/config.json` alone
> don't reach the running service). Re-running `deploy/deploy.sh` refreshes
> the Proxmox token secret from `/root/.agenc-token` in place and warns
> about leftover `REPLACE_*` / `0x0000…0000` placeholders (see
> `tests/test_deploy_config.sh`).

### 4. Try it without a real node first

```bash
python3 tests/test_api.py            # full flow against simulated Proxmox
python3 api/app.py /tmp/my-demo.json # dry_run: true → fake containers
```

### 5. Free vs paid

| Home | Flow |
|---|---|
| **Free** (`billing.free_tier_containers`, default 1) | Provisions **immediately** on request |
| **Paid** (every home after the free slot) | Returns a **USDC/USDT quote** (amount, chain, receive address, unique `reference`); stays `quote_required` until you confirm the transfer, then provisions |

> Set `billing.free_tier_containers` to **`0`** to provision **every** home for
> free — the "billing off / dev mode" switch (health reports
> `free_tier.unlimited: true` and the site badge changes to "all homes free").

Confirm a paid transfer (operator or payment webhook):

```bash
curl -X POST http://NODE/api/agents/ag_xxxxxxxxxx/confirm-payment \
     -H "X-Admin-Key: $ADMIN_KEY"
```

## REST API

Everything under `/api`. Public write calls need the header
`X-Agency-Secret: <site_secret>` (embedded in `website/js/config.js`);
admin calls need `X-Admin-Key: <admin_key>`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | — | node CPU/mem, live agent counts, free-tier usage |
| `POST` | `/api/agents` | site secret | request an agent home (free → auto-provision, later → quote) |
| `GET` | `/api/agents` | — | all agent summaries (no PII, no SSH keys) |
| `GET` | `/api/agents/:id` | — | one agent: status, IP, quote |
| `POST` | `/api/agents/:id/confirm-payment` | admin | mark USDC/USDT paid → provision |
| `POST` | `/api/agents/:id/retry` | admin | retry a failed provisioning (record keeps the real error) |
| `POST` | `/api/agents/:id/destroy` | admin | delete the LXC, reclaim disk |

### Example — request a home

```bash
curl -X POST http://NODE/api/agents \
  -H "Content-Type: application/json" \
  -H "X-Agency-Secret: $SITE_SECRET" \
  -d '{"hostname":"phoenix-42","email":"dev@example.com",
       "purpose":"indexes my notes",
       "ssh_key":"ssh-ed25519 AAAAC3… dev@example.com"}'
```

```json
{"ok": true, "created": true, "agent": {
  "id": "ag_c2dbfee463", "hostname": "phoenix-42",
  "status": "provisioning", "vmid": 1000,
  "cores": 8, "memory_mb": 2048, "disk_gb": 8, "free_tier": true }}
```

## Agent-home spec (defaults — all in `config.json`)

| Knob | Default | Meaning |
|---|---|---|
| `memory_mb` | `2048` | 2 GB RAM per agent |
| `swap_mb` | `512` | 512 MB swap |
| `disk_gb` | `8` | **max 8 GB rootfs** on `rootfs_storage` |
| `rootfs_storage` | `ssd120` | storage pool the agent disk lands on (`pvesm status` for your ids; example tuned for AGENCY's fast pool) |
| `cores` | `0` | `0` ⇒ use *all* host cores, shared: every agent can burst on any core (no pinning); `cpuunits` (1024) keeps the scheduler fair; set `cpulimit` to cap one agent |
| `unprivileged` | `true` | unprivileged LXC (the community-scripts default) |
| `net_bridge` | `sandbox` | the **SDN vnet** (or linux bridge) every agent NIC attaches to — `bridge=sandbox` in net0 |
| `net_preflight` | `true` | verify the NIC target exists before provisioning; set `false` if you manage a raw bridge device PVE's API can't list |
| `features` | `nesting=1` | non-root tokens may only set `nesting`; extras (`fuse`, `keyctl`, `cgroup2`, …) require a root@pam token |
| `vmid_start`/`vmid_end` | `1000`–`1999` | allocation range |

Every home gets `tags: agenc` and a `description` with the agent id so you
can see them in the Proxmox UI.

### Access & the root account

Templates are provisioned **key-only**: there is no root password. The
public key from the request form is installed for `root` (and an optional
`container.agency_ssh_key` is added too), so:

```bash
ssh -i <your-key> root@<container-ip>
```

If you must have a password (e.g. for a demo), set one from the host after
creation:

```bash
pct enter <vmid> -- chpasswd   # set root password interactively
# or: pct exec <vmid> -- bash -c "echo root:CHANGE-ME | chpasswd"
```

### Networking — the `sandbox` vnet

All provisioned NICs attach to the **`sandbox`** Proxmox SDN vnet (config:
`container.net_bridge`). In Proxmox SDN, a container joins a vnet by using
its name as the bridge: `net0: name=eth0,bridge=sandbox,...`. A plain linux
bridge named `sandbox` also works. Before each provision the API verifies
the target exists (`GET /cluster/sdn/vnets` + node network interfaces) and
fails fast with a clear message otherwise; `/api/health` reports
`net_bridge_ok` so you can catch it at deploy time (`proxmox/setup.sh` will
detect the vnet and can even create it: `SDN_ZONE=<zone> ./proxmox/setup.sh`).

`ip: dhcp` needs a DHCP server on the vnet — otherwise set
`container.ip` to a static: `static=10.0.0.5/24,gw=10.0.0.1`.

## Billing model

- **Free slot** — your first home is on us.
- **Paid homes** — `billing.price_usd_per_container_per_month` (default
  $5.00) in USDC or USDT, ERC-20, quotes valid `quote_valid_hours` (72h).
- Each quote carries a unique **`reference`** = the agent id. Ask customers
  to put it in the transfer memo, so an on-chain scan or a payment-provider
  webhook can match transfers to homes; confirm via
  `POST /api/agents/:id/confirm-payment`.

> This repo ships the quote + confirmation loop, not a live blockchain
> indexer. Production wiring (node API / provider webhook → confirm-payment)
> is a ~50 line addition; the endpoint contract is already there.

## Security notes

- PVE auth is a **least-privilege API token** (role `Agenc-Operator`, no
  root password auth). `verify_tls: false` is the default because PVE ships
  self-signed — flip it on once you control the CA.
- **PVE 8 & 9 compatible.** Proxmox VE 9 removed the `VM.Monitor` privilege
  (that's the `invalid privilege 'VM.Monitor'` error some guides produce).
  `proxmox/setup.sh` no longer requests it; if your pveum rejects any
  optional privilege it automatically falls back to a reduced set, so the
  script works on PVE 7.4 → 9.x.
- **SDN privileges included** (`SDN.Audit`, `SDN.Use` in `Agenc-Operator`):
  needed to inspect vnets and attach NICs to the `sandbox` vnet. Without
  them the API cannot tell "vnet missing" from "no permission" — the vnet
  check is tri-state (`true`/`false`/`null`), and only a verified-absent
  result blocks provisioning. `null` proceeds and surfaces the real
  create-time error.
- **Storage split (PVE 8.2+).** Creating container disks requires
  `Datastore.AllocateSpace` on the target storage (this is
  `Permission check failed (/storage/…, Datastore.AllocateSpace)`); older
  PVE used `Datastore.Allocate`. `proxmox/setup.sh`'s main role set
  includes both; on PVE < 8.2 the unknown privilege triggers the reduced-set
  fallback automatically.
- **PVE 9 token permissions model.** privsep tokens do *not* inherit their
  user's roles — `proxmox/setup.sh` grants a **token-level ACL**
  (`pveum aclmod / -token agenc-bot@pve!agenc -role Agenc-Operator`) and
  creates new tokens with `--privsep 0` (the automation standard). It
  probes permissions (`GET /nodes` requires `Sys.Audit`) after setup and
  auto-heals either failure mode, so `token OK` in its output means the
  token can actually drive the API.
- **Real errors are recorded.** A failed provisioning persists the actual
  reason on the agent record (and surfaces in the UI + `error` field) — no
  more misleading `provision task missing`. Fix the cause, then
  `POST /api/agents/:id/retry` (admin).
- **/api/health is your diagnose tool.** Returns `proxmox.reachable`,
  `proxmox.auth_ok`, `templates_available`, `net_bridge`, `net_bridge_ok`,
  free-tier usage. `deploy/deploy.sh` prints these on deploy and warns if
  `REPLACE_*` placeholders or zeroed `0x0000…0000` payment addresses are
  still in `config.json`.
- **Auto-IP is best-effort.** The API tries `pct exec` to read the
  container's IP after creation. Tokens are sometimes denied that call —
  the home still provisions; grab the IP from the Proxmox GUI in that case.
- `dry_run: true` in config = **no containers are created**; every Proxmox
  call is logged instead. Safe for demos and tests.
- Public provisioning is gated by `site_secret` (embedded client-side —
  treat it as a spam gate, not a hard auth boundary) plus a per-IP rate
  limit. Destroy and payment-confirm require the server-side `admin_key`.
- Containers are **unprivileged**; SSH keys are installed via
  `ssh-public-keys`, never stored in the browser-visible API response
  (`public_agent` strips `ssh_key` and `request_ip`).

## Templates

Base OS templates are the standard `pveam` images
(`ubuntu-24.04-standard…tar.zst`, Alpine, Debian…). [community-scripts.org]
maintains a large catalogue of ready-made container **application** builds
(Alpine/Ubuntu/Debian based) built on the same unprivileged-LXC foundation —
point `proxmox.os_template` at any template in your template storage, or
adapt `provisioning.build_params` to layer their scripts into the agent
home after creation.

## Troubleshooting

An agent stuck in `error` is almost always one of four things — all are
visible in `/api/health` and on the agent's `error` field:

```bash
curl http://localhost:8080/api/health | python3 -m json.tool
journalctl -u agenc-api -n 50
```

| Symptom in `error` / health | Cause | Fix |
|---|---|---|
| `auth` / 401 from PVE, health `proxmox.auth_ok=false` | bad token in `config.json` | token lives in `/root/.agenc-token`; re-run `proxmox/setup.sh`, then re-deploy (wires it automatically) |
| `403 … changing feature flags (except nesting) is only allowed for root@pam` | `container.features` contains a non-nesting flag (`keyctl`, `fuse`, …) | set `"features": "nesting=1"` in config.json — non-root tokens are limited to `nesting` |
| `403 Permission check failed … Sys.Audit` (health `auth_ok=null`) | PVE 9 privsep token without its own ACL | re-run `proxmox/setup.sh` — it grants the token-level ACL and re-verifies (`token permissions OK`) |
| ⚠ `bad or missing X-Agency-Secret` on the website | website secret ≠ API secret (a deploy regenerated it) | re-run `./deploy/deploy.sh .` — it now **reuses** the API's secret for the site |
| `net_bridge_ok=false` but your bridge/vnet exists | raw kernel bridge not defined in `/etc/network/interfaces` (PVE API can't list it) | define it in PVE (Node → Network → Create Linux Bridge / SDN), **or** set `container.net_preflight: false` in config.json if you manage that network yourself |
| `Connection refused` on :8080 from the internet (works on node/LAN) | port-forward, hairpin-NAT or firewall on your router/host | `ss -tlnp | grep 8080` on the node; test from another LAN device at `http://10.10.1.179:8080`; test from mobile data (WAN) — consumer routers usually don't hairpin; prefer nginx on :80 instead |
| `NIC target 'sandbox' not found` | vnet/bridge missing, or token lacks `SDN.Audit` | create the `sandbox` SDN vnet (setup.sh can: `SDN_ZONE=<zone>`), re-apply SDN, re-run setup.sh to refresh the role |
| `no template matching prefix` / health `templates_available=null` | OS template not downloaded | `./proxmox/setup.sh` (downloads it) or `pveam download local <template>` |
| `task failed` / task stuck | PVE side: check the node's task log | `/nodes/<node>/tasks` in the GUI; or `pct status <vmid>` |

Fix the cause, then retry the agent (admin):

```bash
curl -X POST http://localhost:8080/api/agents/ag_xxxxxxxxxx/retry \
     -H "X-Admin-Key: $ADMIN_KEY"
```

Deploy-time warnings (`REPLACE_*` placeholders, `0x0000…0000` payment
addresses, `net_bridge_ok=false`) are printed by `deploy/deploy.sh` and
surfaced again on every `/api/health`.

## Roadmap (natural next steps)

- [ ] On-chain USDC/USDT confirmation (RPC or payment-provider webhook → `confirm-payment`)
- [ ] Agent-initiated provisioning key (scoped per-agent token instead of one shared secret)
- [ ] Disk grow endpoint (`POST /api/agents/:id/resize`) for homes that outgrow 8 GB
- [ ] Admin dashboard: list homes, live `pct` metrics, per-agent bandwidth
- [ ] TLS via `certbot --nginx`

---

MIT — built on [Proxmox VE] and the [community-scripts.org] ecosystem.

[Proxmox VE]: https://www.proxmox.com/
[community-scripts.org]: https://community-scripts.org/scripts
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

### 2. Configure

```bash
cp api/config.example.json api/config.json
# edit api/config.json:
#   proxmox.host / api_token_id / api_token_secret
#   billing.receive_addresses.USDC / USDT   <- your wallet addresses
#   container.disk_gb (8), memory_mb (2048), cores (0 = share all host cores)
```

### 3. Deploy

```bash
./deploy/deploy.sh /opt/AGENC-src
```

Installs `/opt/agenc`, generates a random `site_secret` + `admin_key`,
starts the `agenc-api` systemd unit on `:8080`, and puts nginx in front on
`:80`. Point your router's port-forward rules for ports 80/443 at this
node's IP — that's the “forward the web ports to the server” part.

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
| `cores` | `0` | `0` ⇒ use *all* host cores, shared: every agent can burst on any core (no pinning); `cpuunits` (1024) keeps the scheduler fair; set `cpulimit` to cap one agent |
| `unprivileged` | `true` | unprivileged LXC (the community-scripts default) |
| `net_bridge` | `sandbox` | the **SDN vnet** (or linux bridge) every agent NIC attaches to — `bridge=sandbox` in net0 |
| `features` | `nesting=1,keyctl=1` | useful for agents running nested tooling |
| `vmid_start`/`vmid_end` | `1000`–`1999` | allocation range |

Every home gets `tags: agenc` and a `description` with the agent id so you
can see them in the Proxmox UI.

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
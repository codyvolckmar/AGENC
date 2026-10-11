#!/usr/bin/env bash
# Build an AGENCY agent-home base template (reproducible, not a one-off).
#
# Creates a throwaway LXC container from the stock distro template, installs
# a curated package set, scrubs per-machine state (SSH host keys, machine-id,
# logs, apt cache) and converts it to a vztmpl with `pct template`. The API
# then deploys every new home from this image via `proxmox.os_template`.
#
# Run as root ON the Proxmox node:
#   ./proxmox/build-agent-template.sh
#
# Env knobs (all optional):
#   VMID=2999  NAME=agenc-agent  OS=ubuntu-24.04-standard_24.04-2_amd64.tar.zst
#   MEMORY=1024  DISK=4  CORES=2  BRIDGE=vmbr0  IP=dhcp
#   PACKAGES="curl git python3 ..."   (override the curated set)
#   BUILD_STORAGE=                     (unset = auto-pick rootdir+vztmpl storage)
#   KEEP=1                             (leave the build container for inspection)
#   DRY_RUN=1                          (print steps, execute nothing destructive)
set -euo pipefail

VMID="${VMID:-2999}"
NAME="${NAME:-agenc-agent}"
OS="${OS:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
MEMORY="${MEMORY:-1024}"
DISK="${DISK:-4}"
CORES="${CORES:-2}"
BRIDGE="${BRIDGE:-vmbr0}"
IP="${IP:-dhcp}"
KEEP="${KEEP:-0}"
DRY_RUN="${DRY_RUN:-0}"
NODE="$(hostname)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${MANIFEST:-$SCRIPT_DIR/../api/stacks.json}"

run() {  # respects DRY_RUN for the destructive steps
  if [ "${DRY_RUN:-0}" = "1" ]; then echo "    [dry-run] $*"; else "$@"; fi
}
step() { echo; echo "==> $*"; }

vmid_free() {
  pvesh get /cluster/resources --output-format json 2>/dev/null \
    | grep -q "\"vmid\": *$VMID," && return 1 || return 0
}

pick_build_storage() {
  if [ -n "${BUILD_STORAGE:-}" ]; then echo "$BUILD_STORAGE"; return 0; fi
  python3 - <<'PY'
import json, subprocess, sys
out = subprocess.run(
    ["pvesm", "status", "--output-format", "json"], capture_output=True, text=True,
).stdout
try:
    storages = json.loads(out)
except json.JSONDecodeError:
    sys.stderr.write("could not parse pvesm status\n"); sys.exit(1)

def ok(s, want):
    return (s.get("status") == "active"
            and s.get("storage")
            and want <= set((s.get("content") or "").split(",")))

for combo in ({"rootdir", "vztmpl"}, {"rootdir"}):
    for s in storages:
        if ok(s, combo):
            print(s["storage"]); sys.exit(0)
sys.exit(1)
PY
}

newest_vztmpl() {  # echoes the newest vztmpl volid on the given storage
  python3 - "$1" <<'PY'
import json, subprocess, sys, socket
storage = sys.argv[1]
node = socket.gethostname()
out = subprocess.run(
    ["pvesh", "get", f"/nodes/{node}/storage/{storage}/content", "--output-format", "json"],
    capture_output=True, text=True,
).stdout
try:
    items = json.loads(out)
except json.JSONDecodeError:
    items = []
items = [i for i in items if i.get("content") == "vztmpl"]
items.sort(key=lambda i: i.get("ctime", 0), reverse=True)  # newest first
print(items[0]["volid"] if items else "")
PY
}

template_prefix() {  # agenc-agent-2999-20260101.tar.zst -> agenc-agent
  python3 - "$1" <<'PY'
import re, sys
base = sys.argv[1]
if base.endswith(".tar.zst"):
    base = base[:-len(".tar.zst")]
m = re.search(r"-\d", base)
print(base[: m.start()] if m else base)
PY
}

echo "==> AGENCY agent template build (node $NODE)"
[ "$(id -u)" = "0" ] || { echo "!! run as root on the Proxmox node" >&2; exit 1; }
for c in pct pveam pvesh pvesm; do
  command -v "$c" >/dev/null || { echo "!! missing $c" >&2; exit 1; }
done

BUILD_STORAGE="$(pick_build_storage)" || {
  echo "!! no storage with 'rootdir' content (needed for the build container)" >&2
  echo "   add/fix a storage in Datacenter -> Storage, or set BUILD_STORAGE=…" >&2
  exit 1
}
echo "    build storage : $BUILD_STORAGE"

vmid_free || { echo "!! vmid $VMID is in use — set VMID=…" >&2; exit 1; }

# ------------------------------------------------------------ stock image
step "1. Stock OS template '$OS'"
if pveam list local 2>/dev/null | grep -q "$OS"; then
  echo "    present"
else
  run pveam update >/dev/null 2>&1 || true
  run pveam download local "$OS"
fi
STOCK_VOLID="local:vztmpl/$OS"

# ------------------------------------------------------ build container
step "2. Create throwaway build container vmid=$VMID (${MEMORY}MB / ${DISK}GB on $BRIDGE, unprivileged)"
run pct create "$VMID" "$STOCK_VOLID" \
  --hostname "${NAME}-build" \
  --memory "$MEMORY" --swap 0 --cores "$CORES" \
  --rootfs "$BUILD_STORAGE:$DISK" \
  --net0 "name=eth0,bridge=$BRIDGE,firewall=0,ip=$IP" \
  --features nesting=1 --unprivileged 1 \
  --description "AGENCY agent-template build scratch (delete after build)"

step "3. Boot and wait for network"
run pct start "$VMID"
tries=0
while [ "$tries" -lt 60 ]; do
  if pct exec "$VMID" -- bash -c 'ip route get 1.1.1.1 >/dev/null 2>&1' 2>/dev/null; then
    echo "    network up"
    break
  fi
  tries=$((tries + 1)); sleep 2
  if [ "$tries" = "60" ]; then
    echo "!! build container never got network (check BRIDGE/IP/DHCP)" >&2
    exit 1
  fi
done

# ------------------------------------------------------------ packages
# Default = the 'baked' groups from api/stacks.json (the same manifest the
# website menu and the provisioning API use), unless PACKAGES is set.
if [ -z "${PACKAGES:-}" ]; then
  step "4. Install baked base packages (from stacks.json 'baked' groups)"
  PACKAGES="$(python3 - "$MANIFEST" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1]))
by_id = {g["id"]: g for g in manifest["groups"]}
pkgs = []
for gid in manifest.get("baked", []):
    for p in by_id.get(gid, {}).get("packages", []):
        if p not in pkgs:
            pkgs.append(p)
print(" ".join(pkgs))
PY
)"
else
  step "4. Install packages (PACKAGES override)"
fi
baked_ids() { python3 - "$MANIFEST" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1]))
print(", ".join(manifest.get("baked", [])))
PY
}
echo "    baked groups : $(baked_ids)"
echo "    package count: $(echo "$PACKAGES" | wc -w)"
run pct exec "$VMID" -- bash -c \
  "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq $PACKAGES && apt-get upgrade -y -qq"

step "5. Bake base config (motd, timezone, /opt/agency)"
pct exec "$VMID" -- bash -c "cat > /etc/motd <<'EOF'
Welcome home. This container was provisioned by AGENCY.

  owner ..: root (key-only access)
  managed : /opt/agenc          (see AGENC repo)
  rules ..: 2 GB RAM / 8 GB disk / shared CPU — be a good tenant.
EOF"
run pct exec "$VMID" -- bash -c "timedatectl set-timezone UTC 2>/dev/null || true; mkdir -p /opt/agency"

# ------------------------------------------------ scrub for templating
step "6. Scrub per-machine state (required before pct template)"
run pct exec "$VMID" -- bash -c '
  rm -f /etc/ssh/ssh_host_*                     # host keys are per-home secrets
  rm -f /etc/machine-id /var/lib/dbus/machine-id
  : > /root/.bash_history
  apt-get -y -qq autoremove --purge || true
  apt-get -y -qq clean || true
  rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
  find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
'

if [ "${KEEP:-0}" = "1" ]; then
  echo
  echo "!! KEEP=1 — build container left running as vmid=$VMID (no template made)."
  exit 0
fi

# ------------------------------------------------------------ templating
step "7. Stop and convert to a template"
run pct stop "$VMID"
run pct template "$VMID"

step "8. Locate the produced template"
NEW_VOLID="$(newest_vztmpl "$BUILD_STORAGE")"
if [ -z "$NEW_VOLID" ]; then
  echo "!! no vztmpl found on $BUILD_STORAGE after pct template —" >&2
  echo "   scan PVE: Datacenter -> Storage, or: pveam list \$STORAGE" >&2
  echo "   (a dir-based storage is required to hold the tarball)" >&2
  exit 1
fi
PREFIX="$(template_prefix "$(basename "$NEW_VOLID")")"

step "9. Clean up build container"
run pct destroy "$VMID"

echo
echo "==> Done. Template built: $NEW_VOLID"
echo
echo "Point AGENCY at it (edit /opt/agenc/api/config.json, then restart):"
echo "    \"proxmox\": {"
echo "        \"template_storage\": \"$BUILD_STORAGE\","
echo "        \"os_template\": \"$PREFIX\","
echo "    }"
echo
echo "os_template is a PREFIX — the API picks the newest build matching it,"
echo "so re-running this script periodically refreshes every new home."
echo "Keep a stock template (e.g. ubuntu-24.04) as an instant fallback."
[ "${DRY_RUN:-0}" = "1" ] && echo "(dry-run — nothing was changed)"
#!/usr/bin/env bash
# One-shot setup for the Proxmox node that will host AGENCY agent homes.
# Run as root ON the node. It creates a least-privilege API token for the
# service and downloads the base OS container template if missing.
#
#   Usage:  NODE=pve STORAGE=local ./proxmox/setup.sh
#
# The token secret is written (mode 600) to /root/.agenc-token — deploy.sh
# reads it from there when generating api/config.json.
set -euo pipefail

TOKEN_USER="${TOKEN_USER:-agenc-bot@pve}"
TOKEN_ID="${TOKEN_ID:-agenc}"
ROLE="${ROLE:-Agenc-Operator}"
STORAGE="${STORAGE:-local}"
OS_TEMPLATE="${OS_TEMPLATE:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
TOKEN_FILE="${TOKEN_FILE:-/root/.agenc-token}"

PRIVS="VM.Allocate VM.Audit VM.Clone VM.Config.CPU VM.Config.Disk VM.Config.Memory VM.Config.Network VM.Config.Options VM.Monitor VM.PowerMgmt Datastore.Allocate Datastore.AllocateTemplate Datastore.Audit Sys.Audit"

echo "==> AGENCY node setup on $(hostname)"

# --- 1. least-privilege role -------------------------------------------
echo "==> Ensuring role '${ROLE}'"
if pveum role list --output-format json 2>/dev/null | grep -q "\"${ROLE}\""; then
  pveum role modify "$ROLE" --privs "$PRIVS"
else
  pveum role add "$ROLE" --privs "$PRIVS"
fi

# --- 2. service user + ACL ---------------------------------------------
echo "==> Ensuring user '${TOKEN_USER}' with role on /"
pveum user add "$TOKEN_USER" 2>/dev/null || true
pveum aclmod / -user "$TOKEN_USER" -role "$ROLE"

# --- 3. API token (secret is only printed once) --------------------------
echo "==> Ensuring API token '${TOKEN_ID}'"
TMP_OUT="$(mktemp)"
if pveum user token add "$TOKEN_USER" "$TOKEN_ID" --privsep 1 >"$TMP_OUT" 2>&1; then
  # PVE 8 prints: key: <id>  secret: <hex>
  TOKEN_SECRET="$(grep -oP 'secret[=:]\s*\K[^\s"]+' "$TMP_OUT" | head -1 || true)"
  if [ -z "${TOKEN_SECRET:-}" ]; then
    echo "!! Could not parse the token secret from pveum output:" >&2
    cat "$TMP_OUT" >&2
    exit 1
  fi
  printf 'PVEAPIToken=%s!%s=%s\n' "$TOKEN_USER" "$TOKEN_ID" "$TOKEN_SECRET" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "    token written to $TOKEN_FILE"
else
  if [ -s "$TOKEN_FILE" ]; then
    echo "    token already exists — reusing $TOKEN_FILE"
  else
    echo "!! 'pveum user token add' failed and no saved token found:" >&2
    cat "$TMP_OUT" >&2
    exit 1
  fi
fi
rm -f "$TMP_OUT"

# --- 4. base template ----------------------------------------------------
echo "==> Ensuring OS template '${OS_TEMPLATE}' on '${STORAGE}'"
if pveam list "$STORAGE" 2>/dev/null | grep -q "$OS_TEMPLATE"; then
  echo "    template already present"
else
  pveam update >/dev/null 2>&1 || true
  pveam download "$STORAGE" "$OS_TEMPLATE" || {
    echo "!! Could not download '${OS_TEMPLATE}'. Available:" >&2
    pveam available | grep -E "ubuntu|debian|alpine" | head -20 >&2
    exit 1
  }
fi

# --- 5. sanity checks ------------------------------------------------------
NET_BRIDGE="${NET_BRIDGE:-sandbox}"
echo "==> Sanity checks"
if pvesh get /cluster/sdn/vnets --output-format json 2>/dev/null | grep -q "\"vnet\": \"$NET_BRIDGE\""; then
  echo "    network '$NET_BRIDGE': present (SDN vnet)"
elif ip link show "$NET_BRIDGE" >/dev/null 2>&1; then
  echo "    network '$NET_BRIDGE': present (linux bridge)"
elif [ -n "${SDN_ZONE:-}" ]; then
  echo "    creating SDN vnet '$NET_BRIDGE' in zone '$SDN_ZONE'"
  pvesh create /cluster/sdn/vnets --vnet "$NET_BRIDGE" --zone "$SDN_ZONE" \
    && { echo "    created — applying SDN:"; pvesh put /cluster/sdn/status >/dev/null 2>&1 || pvesh post /cluster/sdn/status >/dev/null 2>&1 || true; } \
    || echo "    !! could not create vnet — create it in the GUI (Datacenter -> SDN) or run: SDN_ZONE=<zone> $0"
else
  echo "    !! network '$NET_BRIDGE' not found. Every agent NIC will attach to"
  echo "       it, so create it first:"
  echo "         - SDN vnet:      Datacenter -> SDN (Proxmox VE SDN), or"
  echo "           SDN_ZONE=<your zone> .$0"
  echo "         - linux bridge:  # ip link add $NET_BRIDGE type bridge; ip link set $NET_BRIDGE up"
  echo "       then re-run this script. (VMs on the vnet need DHCP or static ip= in config.json)"
fi
ip link show vmbr0 >/dev/null 2>&1 && echo "    legacy vmbr0: present (unused by AGENCY)" || true
pvesh get /nodes/$(hostname)/status --output-format json \
  | grep -o '"cpus":[0-9]*' | head -1 | sed 's/^/    host cpus: /'

echo
echo "==> Done. Next:"
echo "    cp api/config.example.json api/config.json"
echo "    sed -i 's#REPLACE_FROM_/root/.agenc-token#$(cat "$TOKEN_FILE" | cut -d= -f3)#' api/config.json"
echo "    ./deploy/deploy.sh ."
echo "    then port-forward your web ports (80/443) to this node."
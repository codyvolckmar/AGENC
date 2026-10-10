#!/usr/bin/env bash
# One-shot setup for the Proxmox node that will host AGENCY agent homes.
# Run as root ON the node. It creates a least-privilege API token for the
# service and downloads the base OS container template if missing.
#
#   Usage:  NODE=pve STORAGE=local ./proxmox/setup.sh
#
# The token secret is written (mode 600) to /root/.agenc-token — deploy.sh
# reads it from there when generating api/config.json. Safe to re-run:
# it heals partially-failed previous runs (e.g. an orphaned token whose
# secret was never saved, because pveum only prints it once).
set -euo pipefail

TOKEN_USER="${TOKEN_USER:-agenc-bot@pve}"
TOKEN_ID="${TOKEN_ID:-agenc}"
ROLE="${ROLE:-Agenc-Operator}"
STORAGE="${STORAGE:-local}"
OS_TEMPLATE="${OS_TEMPLATE:-ubuntu-24.04-standard_24.04-2_amd64.tar.zst}"
TOKEN_FILE="${TOKEN_FILE:-/root/.agenc-token}"

# Privilege sets for the AGENCY service role.
#
# IMPORTANT (PVE 9): 'VM.Monitor' was REMOVED in Proxmox VE 9.0 (replaced by
# VM.GuestAgent.* privileges) — pveum rejects it with "invalid privilege
# 'VM.Monitor'". It is therefore NOT listed here. The script first tries the
# full intersection-safe set, and if pveum reports an unknown privilege for
# this PVE version, it falls back to a minimal set.
PRIVS_MAIN="VM.Allocate VM.Audit VM.Clone VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.PowerMgmt Datastore.Allocate Datastore.AllocateTemplate Datastore.Audit Sys.Audit"
PRIVS_MIN="VM.Allocate VM.Audit VM.Config.CPU VM.Config.Disk VM.Config.Memory VM.Config.Network VM.Config.Options Datastore.Allocate Datastore.AllocateTemplate Datastore.Audit Sys.Audit"

echo "==> AGENCY node setup on $(hostname)"

# --- 1. least-privilege role -------------------------------------------
# Tries each candidate set until one is accepted by this PVE's pveum.
ROLE_PRIVS_USED=""
ensure_role() {
  local role="$1"; shift
  local privs output
  ROLE_PRIVS_USED=""
  for privs in "$@"; do
    if pveum role list --output-format json 2>/dev/null | grep -q "\"${role}\""; then
      output="$(pveum role modify "$role" --privs "$privs" 2>&1)" && { ROLE_PRIVS_USED="$privs"; return 0; }
    else
      output="$(pveum role add "$role" --privs "$privs" 2>&1)" && { ROLE_PRIVS_USED="$privs"; return 0; }
    fi
  done
  echo "$output" >&2
  return 1
}
echo "==> Ensuring role '${ROLE}'"
if ensure_role "$ROLE" "$PRIVS_MAIN" "$PRIVS_MIN"; then
  if [ "$ROLE_PRIVS_USED" = "$PRIVS_MIN" ]; then
    echo "    ok (reduced privileges — this PVE version rejected optional privileges)"
  else
    echo "    ok"
  fi
else
  echo "!! could not create/modify role '${ROLE}' — last pveum error above" >&2
  exit 1
fi

# --- 2. service user + ACL ---------------------------------------------
echo "==> Ensuring user '${TOKEN_USER}' with role on /"
pveum user add "$TOKEN_USER" 2>/dev/null || true
pveum aclmod / -user "$TOKEN_USER" -role "$ROLE"

# --- 3. API token (secret is only printed once) --------------------------
# pveum prints the fresh secret in different shapes across versions:
#   PVE 7/8:  key: <id>  secret: <hex>
#   PVE 9:    a box table with a 'value' row, e.g.
#             │ value        │ 594f25c7-c9a5-4844-8dc4-e82a56369d2d │
# parse_token_secret() understands the table, key:value lines and JSON.
parse_token_secret() {
  python3 - "$1" <<'PY'
import json, re, sys
raw = open(sys.argv[1], encoding="utf-8").read() if sys.argv[1:] else ""
data = None
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    pass
if isinstance(data, list):
    for row in data:
        if isinstance(row, dict) and row.get("key") in ("value", "secret"):
            print(str(row.get("value") or row.get("secret") or "")); sys.exit(0)
elif isinstance(data, dict):
    if isinstance(data.get("value"), str) and data["value"]:
        print(data["value"]); sys.exit(0)
    for row in data.values():
        if isinstance(row, dict) and row.get("key") == "value":
            print(str(row.get("value") or "")); sys.exit(0)
# PVE 9 box table (uses │ U+2502 / ├┼ U+251C,256A). Split each line on the
# cell separators and read the row whose first cell is 'value':
#   │ value        │ 594f25c7-… │   →   cells == ['', ' value ', ' … ', '']
for line in raw.splitlines():
    cells = [c.strip() for c in re.split(r"[│|╪┼]", line)]
    if len(cells) >= 3 and cells[1] in ("value", "secret"):
        print(cells[2]); sys.exit(0)
# plain text: 'secret = xxx' or 'secret: xxx'
m = re.search(r"secret\s*[=:]\s*([^\s'\"|]+)", raw)
if m:
    print(m.group(1).strip()); sys.exit(0)
sys.exit(1)
PY
}

create_token() {
  local out="$1"
  if pveum user token add "$TOKEN_USER" "$TOKEN_ID" --privsep 1 >"$out" 2>&1; then
    TOKEN_SECRET="$(parse_token_secret "$out")" || TOKEN_SECRET=""
    if [ -z "$TOKEN_SECRET" ]; then
      echo "!! Could not parse the token secret from pveum output:" >&2
      cat "$out" >&2
      return 1
    fi
    printf 'PVEAPIToken=%s!%s=%s\n' "$TOKEN_USER" "$TOKEN_ID" "$TOKEN_SECRET" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "    token written to $TOKEN_FILE"
    return 0
  fi
  return 1
}

token_exists() {
  pveum user token list "$TOKEN_USER" 2>/dev/null | grep -qE "$TOKEN_ID"
}

echo "==> Ensuring API token '${TOKEN_ID}'"
TMP_OUT="$(mktemp)"
if create_token "$TMP_OUT"; then
  :
elif token_exists; then
  if [ -s "$TOKEN_FILE" ]; then
    echo "    token already exists — reusing $TOKEN_FILE"
  else
    echo "!! token '$TOKEN_ID' exists but its secret was never saved (pveum shows it once)."
    echo "   Deleting it so a fresh secret can be created…"
    if pvesh delete "/access/users/$TOKEN_USER/tokens/$TOKEN_ID" 2>/dev/null; then
      if create_token "$TMP_OUT"; then
        :
      else
        echo "!! recreate failed:" >&2
        cat "$TMP_OUT" >&2
        exit 1
      fi
    else
      echo "!! could not delete the orphaned token. Delete it yourself, then re-run:" >&2
      echo "   pvesh delete /access/users/$TOKEN_USER/tokens/$TOKEN_ID" >&2
      echo "   (or GUI: Datacenter -> Permissions -> API Tokens -> delete '$TOKEN_ID')" >&2
      exit 1
    fi
  fi
else
  echo "!! 'pveum user token add' failed and no saved token found:" >&2
  cat "$TMP_OUT" >&2
  exit 1
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
if pvesh get /cluster/sdn/vnets --output-format json 2>/dev/null | grep -qE '"vnet"[[:space:]]*:[[:space:]]*"'"$NET_BRIDGE"'"'; then
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
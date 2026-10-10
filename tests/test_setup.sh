#!/usr/bin/env bash
# Functional test for proxmox/setup.sh against FAKE pve tools.
#
# The fake pveum reproduces Proxmox VE 9 semantics: the VM.Monitor
# privilege no longer exists and is rejected exactly like the real error:
#   400 Parameter verification failed. privs: invalid format - invalid
#   privilege 'VM.Monitor'
#
# This test proves setup.sh now (a) installs the role without VM.Monitor
# and (b) falls back to a reduced set when a privilege is unknown.
#
# Run:  bash tests/test_setup.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
STATE="$WORK/state"
mkdir -p "$BIN" "$STATE"

# ------------------------------------------------ fake pveum (PVE 9 rules)
cat > "$BIN/pveum" <<'EOF'
#!/usr/bin/env bash
STATE="${FAKE_PVE_STATE:-/tmp/fake-pve-state}"
mkdir -p "$STATE"
roles_file="$STATE/roles"
applied_file="$STATE/roles-applied"
case "${1:-}" in
  role)
    case "${2:-}" in
      list)
        if [ -s "$roles_file" ]; then cat "$roles_file"; else echo '[]'; fi ;;
      add|modify)
        privs=""; prev=""
        for a in "$@"; do [ "$prev" = "--privs" ] && privs="$a"; prev="$a"; done
        echo "attempt ${2}:${3} :: ${privs}" >> "$STATE/roles-attempts"
        for p in $privs; do
          if [ "$p" = "VM.Monitor" ] || [ -n "${FAKE_BAD_PRIV:-}" ] && [ "$p" = "$FAKE_BAD_PRIV" ]; then
            echo "400 Parameter verification failed. privs: invalid format - invalid privilege '$p'" >&2
            exit 1
          fi
        done
        echo "${2}:${3} :: ${privs}" >> "$applied_file"
        printf '[{"roleid":"%s"}]\n' "$3" > "$roles_file"
        ;;
    esac
    ;;
  user)
    case "${2:-}" in
      add) exit 0 ;;
      token)
        case "${3:-}" in
          add)
            if [ -f "$STATE/token-added" ]; then
              # real pveum refuses to re-create an existing token (500)
              echo "token already exists" >&2
              exit 1
            fi
            : > "$STATE/token-added"
            echo "key: agenc  secret: $(tr -dc 'a-f0-9' </dev/urandom | head -c 32)" ;;
          list) exit 0 ;;
        esac
        ;;
    esac
    ;;
  aclmod) exit 0 ;;
esac
EOF

cat > "$BIN/pvesh" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  get)
    case "${2:-}" in
      /cluster/sdn/vnets) printf '[ { "vnet": "sandbox", "zone": "localnet" } ]\n' ;;
      /nodes/*/status) printf '{ "status": "online", "cpuinfo": { "cpus": 8 } }\n' ;;
    esac ;;
  create|put|post) exit 0 ;;
esac
EOF

cat > "$BIN/pveam" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  list) echo "local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst" ;;
  update|download) exit 0 ;;
  available) echo "ubuntu-24.04-standard_24.04-2_amd64.tar.zst" ;;
esac
EOF
chmod +x "$BIN/pveum" "$BIN/pvesh" "$BIN/pveam"

run_setup() {
  FAKE_PVE_STATE="$STATE" \
  TOKEN_FILE="$WORK/agenc-token" \
  PATH="$BIN:$PATH" \
  bash "$ROOT/proxmox/setup.sh" 2>&1
}

echo "==> test 1: install on PVE 9 (VM.Monitor rejected) — expect success + fallback-free role"
OUT="$(run_setup)"
echo "$OUT" | grep -q "role 'Agenc-Operator'" || { echo "FAIL: role line missing"; echo "$OUT"; exit 1; }
grep -q "add:Agenc-Operator" "$STATE/roles-applied" || { echo "FAIL: role was never created"; echo "$OUT"; exit 1; }
grep -q "VM.Monitor" "$STATE/roles-applied" && { echo "FAIL: VM.Monitor ended up in the role"; exit 1; }
[ -s "$WORK/agenc-token" ] || { echo "FAIL: token file not written"; exit 1; }
echo "$OUT" | grep -q "sandbox': present (SDN vnet)" || { echo "FAIL: vnet sanity check didn't report sandbox"; echo "$OUT"; exit 1; }
echo "    ok"

echo "==> test 2: privilege unknown to this PVE (VM.Config.HWType) — expect fallback to reduced set"
rm -f "$STATE/roles" "$STATE/roles-applied" "$STATE/roles-attempts" "$WORK/agenc-token" "$STATE/token-added"
OUT="$(FAKE_BAD_PRIV="VM.Config.HWType" run_setup)"
echo "$OUT" | grep -q "reduced privileges" || { echo "FAIL: no reduced-privileges fallback note"; echo "$OUT"; exit 1; }
LINES="$(grep -c '^attempt' "$STATE/roles-attempts")"
[ "$LINES" -ge 2 ] || { echo "FAIL: expected a retry on the reduced set"; echo "$OUT"; exit 1; }
LAST="$(tail -1 "$STATE/roles-attempts")"
echo "$LAST" | grep -q "VM.Config.HWType" && { echo "FAIL: bad privilege present in final role"; exit 1; }
[ -s "$WORK/agenc-token" ] || { echo "FAIL: token file not written"; exit 1; }
echo "    ok"

echo "==> test 3: rerun (role exists) is idempotent — modify path works"
OUT="$(run_setup)"
echo "$OUT" | grep -q "token already exists — reusing" || { echo "FAIL: rerun should reuse the token"; echo "$OUT"; exit 1; }
echo "    ok"

echo
echo "ALL SETUP TESTS PASSED"
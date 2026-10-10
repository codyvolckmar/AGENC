#!/usr/bin/env bash
# Functional test for proxmox/setup.sh against FAKE pve tools.
#
# The fakes reproduce real Proxmox VE 9 behavior that has bitten installs:
#   1. 'VM.Monitor' privilege no longer exists → role add rejects it
#   2. 'pveum user token add' prints the secret as a BOX TABLE, not
#      'key: … secret: …' — parsing must handle the table
#   3. a token whose secret was never saved is an orphan that must be
#      deleted before a fresh one can be created
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
attempts_file="$STATE/roles-attempts"
case "${1:-}" in
  role)
    case "${2:-}" in
      list)
        if [ -s "$roles_file" ]; then cat "$roles_file"; else echo '[]'; fi ;;
      add|modify)
        privs=""; prev=""
        for a in "$@"; do [ "$prev" = "--privs" ] && privs="$a"; prev="$a"; done
        echo "attempt ${2}:${3} :: ${privs}" >> "$attempts_file"
        for p in $privs; do
          if [ "$p" = "VM.Monitor" ] || { [ -n "${FAKE_BAD_PRIV:-}" ] && [ "$p" = "$FAKE_BAD_PRIV" ]; }; then
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
        cmd="${3:-}"; u="${4:-}"; t="${5:-}"
        case "$cmd" in
          add)
            if [ -f "$STATE/token-added" ]; then
              echo "pveum user token add failed: token already exists" >&2
              exit 1
            fi
            : > "$STATE/token-added"
            echo "$t" > "$STATE/token-id"
            SECRET="$(tr -dc 'a-f0-9' </dev/urandom | head -c 32)"
            echo "$SECRET" > "$STATE/token-secret"
            # PVE 9 prints the secret exactly like this box table
            printf '┌──────────────┬──────────────────────────────────────┐\n│ key          │ value                                │\n╞══════════════╪══════════════════════════════════════╡\n│ full-tokenid │ %s!%s                  │\n├──────────────┼──────────────────────────────────────┤\n│ info         │ {"privsep":"1"}                      │\n├──────────────┼──────────────────────────────────────┤\n│ value        │ %s │\n└──────────────┴──────────────────────────────────────┘\n' "$u" "$t" "$SECRET"
            ;;
          list)
            if [ -f "$STATE/token-added" ]; then
              TID="$(cat "$STATE/token-id" 2>/dev/null || echo 'agenc')"
              printf '┌─────────┬────────┐\n│ tokenid │ expire │\n╞═════════╪════════╡\n│ %s      │ 0      │\n└─────────┴────────┘\n' "$TID"
            fi
            ;;
        esac
        ;;
    esac
    ;;
  aclmod) echo "aclmod $*" >> "$STATE/aclmod-calls" ; exit 0 ;;
esac
EOF

cat > "$BIN/pvesh" <<'EOF'
#!/usr/bin/env bash
STATE="${FAKE_PVE_STATE:-/tmp/fake-pve-state}"
mkdir -p "$STATE"
case "${1:-}" in
  get)
    case "${2:-}" in
      /cluster/sdn/vnets) printf '[ { "vnet": "sandbox", "zone": "localnet" } ]\n' ;;
      /nodes/*/status) printf '{ "status": "online", "cpuinfo": { "cpus": 8 } }\n' ;;
    esac ;;
  delete)
    # pvesh delete /access/users/<u>/tokens/<t> — removes the orphan
    rm -f "$STATE/token-added" "$STATE/token-secret"
    exit 0 ;;
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

cat > "$BIN/curl" <<'EOF'
#!/usr/bin/env bash
# fake curl: simulates the PVE API probes used by setup.sh
STATE="${FAKE_PVE_STATE:-/tmp/fake-pve-state}"
mkdir -p "$STATE"
case " $* " in
  *"api2/json/nodes"*)
    # permission probe: optionally fail the first /nodes call
    if [ -n "${FAKE_CURL_NODES_FIRST:-}" ]; then
      n="$(cat "$STATE/curl-nodes-count" 2>/dev/null || echo 0)"; n=$((n + 1)); echo "$n" > "$STATE/curl-nodes-count"
      if [ "$n" -eq 1 ]; then echo "$FAKE_CURL_NODES_FIRST"; else echo "${FAKE_CURL_STATUS:-200}"; fi
    else
      echo "${FAKE_CURL_STATUS:-200}"
    fi
    ;;
  *"%{http_code}"*)
    if [ -n "${FAKE_CURL_FAIL_ONCE:-}" ] && [ ! -f "$STATE/curl-failed" ]; then
      : > "$STATE/curl-failed"
      case "$FAKE_CURL_FAIL_ONCE" in 1|401) echo 401 ;; *) echo "$FAKE_CURL_FAIL_ONCE" ;; esac
    else
      echo "${FAKE_CURL_STATUS:-200}"
    fi
    ;;
  *) echo '{"data":{"version":"fake 9.0"}}' ;;
esac
EOF
chmod +x "$BIN/pveum" "$BIN/pvesh" "$BIN/pveam" "$BIN/curl"

run_setup() {
  FAKE_PVE_STATE="$STATE" \
  TOKEN_FILE="$WORK/agenc-token" \
  PATH="$BIN:$PATH" \
  bash "$ROOT/proxmox/setup.sh" 2>&1
}

echo "==> test 1: fresh install, PVE 9 box-table token output — parse must succeed"
OUT="$(run_setup)"
echo "$OUT" | grep -q "token written to" || { echo "FAIL: token secret not parsed/written"; echo "$OUT"; exit 1; }
grep -q "add:Agenc-Operator" "$STATE/roles-applied" || { echo "FAIL: role never created"; echo "$OUT"; exit 1; }
grep -q "VM.Monitor" "$STATE/roles-applied" && { echo "FAIL: VM.Monitor in role"; exit 1; }
[ -s "$WORK/agenc-token" ] || { echo "FAIL: token file empty"; exit 1; }
grep -q "$(cat "$STATE/token-secret")" "$WORK/agenc-token" || { echo "FAIL: saved secret != table value"; exit 1; }
echo "$OUT" | grep -q "sandbox': present (SDN vnet)" || { echo "FAIL: vnet not detected"; echo "$OUT"; exit 1; }
echo "    ok"

echo "==> test 2: privilege unknown to this PVE (VM.Config.HWType) — expect fallback to reduced set"
rm -f "$STATE/roles" "$STATE/roles-applied" "$STATE/roles-attempts" "$WORK/agenc-token" "$STATE/token-added" "$STATE/token-secret"
OUT="$(FAKE_BAD_PRIV="VM.Config.HWType" run_setup)"
echo "$OUT" | grep -q "reduced privileges" || { echo "FAIL: no reduced-privileges note"; echo "$OUT"; exit 1; }
LINES="$(grep -c '^attempt' "$STATE/roles-attempts")"
[ "$LINES" -ge 2 ] || { echo "FAIL: expected a retry on the reduced set"; echo "$OUT"; exit 1; }
LAST="$(tail -1 "$STATE/roles-attempts")"
echo "$LAST" | grep -q "VM.Config.HWType" && { echo "FAIL: bad privilege in final role"; exit 1; }
[ -s "$WORK/agenc-token" ] || { echo "FAIL: token file not written"; exit 1; }
echo "    ok"

echo "==> test 3: rerun (token exists + saved file) — idempotent reuse"
OUT="$(run_setup)"
echo "$OUT" | grep -q "token already exists — reusing" || { echo "FAIL: rerun should reuse the token"; echo "$OUT"; exit 1; }
echo "    ok"

echo "==> test 4: orphaned token (exists, secret never saved) — delete + recreate"
rm -f "$STATE/token-added" "$STATE/token-secret" "$WORK/agenc-token"
: > "$STATE/token-added"     # simulate the user's failed first run: token exists, no saved secret
OUT="$(run_setup)"
echo "$OUT" | grep -q "secret was never saved" || { echo "FAIL: no orphan notice"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q "token written to" || { echo "FAIL: token not recreated"; echo "$OUT"; exit 1; }
[ -s "$WORK/agenc-token" ] || { echo "FAIL: token file missing after recovery"; exit 1; }
SECRET_AFTER="$(cat "$WORK/agenc-token" | cut -d= -f3)"
[ "$SECRET_AFTER" = "$(cat "$STATE/token-secret")" ] || { echo "FAIL: recreated secret mismatch"; exit 1; }
echo "    ok"

echo "==> test 5: stale token (PVE rejects the saved secret) — expect rotation"
# simulate: a token exists in PVE, a token file exists, but the secret is stale
: > "$STATE/token-added"
: > "$STATE/token-secret"
printf 'PVEAPIToken=agenc-bot@pve!agenc=deadbeef-dead-beef-dead-beefdeadbeef\n' > "$WORK/agenc-token"
rm -f "$STATE/curl-failed"
OUT="$(FAKE_CURL_FAIL_ONCE=1 run_setup)"
echo "$OUT" | grep -q "token rejected" || { echo "FAIL: no rejection detected"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q "token OK after rotation" || { echo "FAIL: rotation did not verify OK"; echo "$OUT"; exit 1; }
[ "$(cat "$WORK/agenc-token" | cut -d= -f3)" = "$(cat "$STATE/token-secret")" ] \
  || { echo "FAIL: file not updated with the fresh secret"; exit 1; }
echo "    ok"

echo "==> test 6: valid token but NO permissions (PVE 9 privsep) — expect token ACL grant"
# token exists and authenticates, but the /nodes permission probe 403s first
: > "$STATE/token-added"
printf '8f212fac-779f-4875-84cf-3365b0eca9ee' > "$STATE/token-secret"
printf 'PVEAPIToken=agenc-bot@pve!agenc=%s\n' "$(cat "$STATE/token-secret")" > "$WORK/agenc-token"
rm -f "$STATE/curl-failed" "$STATE/curl-nodes-count" "$STATE/aclmod-calls"
OUT="$(FAKE_CURL_NODES_FIRST=403 run_setup)"
echo "$OUT" | grep -q "token authenticates" || { echo "FAIL: token should authenticate"; echo "$OUT"; exit 1; }
echo "$OUT" | grep -q "token permissions OK after ACL grant" || { echo "FAIL: no ACL-grant recovery"; echo "$OUT"; exit 1; }
grep -q "aclmod / -token agenc-bot@pve!agenc -role Agenc-Operator" "$STATE/aclmod-calls" \
  || { echo "FAIL: token-level ACL not granted"; cat "$STATE/aclmod-calls"; exit 1; }
grep -q "aclmod / -user agenc-bot@pve -role Agenc-Operator" "$STATE/aclmod-calls" \
  || { echo "FAIL: user-level ACL missing"; exit 1; }
echo "    ok"

echo
echo "ALL SETUP TESTS PASSED"
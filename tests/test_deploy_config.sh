#!/usr/bin/env bash
# Tests deploy/refresh_token.py — the "deployed config already exists"
# path that previously left a placeholder token secret in place.
#
# Run:  bash tests/test_deploy_config.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
CFG="$WORK/config.json"
TOKEN="$WORK/token"
REFRESH="$ROOT/deploy/refresh_token.py"
SECRET="5fba1e2e-0389-4258-aa1f-a6c7ebb55177"

check_secret() {  # assert config.json proxmox.api_token_secret == "$SECRET"
  python3 - "$CFG" "$SECRET" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
val = cfg.get("proxmox", {}).get("api_token_secret")
if val != sys.argv[2]:
    print(f"FAIL: api_token_secret is {val!r}, expected {sys.argv[2]!r}")
    sys.exit(1)
PY
}

echo "==> test 1: placeholder secret + token file -> refreshed (exact match)"
printf 'PVEAPIToken=agenc-bot@pve!agenc=%s\n' "$SECRET" > "$TOKEN"
printf '{"site_secret":"keep-me","proxmox":{"api_token_secret":"REPLACE_FROM_/root/.agenc-token"}}\n' > "$CFG"
OUT="$(python3 "$REFRESH" "$CFG" "$TOKEN")"
echo "$OUT" | grep -q "refreshed from" || { echo "FAIL: expected refresh"; echo "$OUT"; exit 1; }
check_secret
grep -q '"site_secret": "keep-me"' "$CFG" || { echo "FAIL: operator settings clobbered"; exit 1; }
echo "    ok"

echo "==> test 2: CORRUPTED secret (id+secret from the split bug) -> corrected"
printf '{"site_secret":"keep-me","proxmox":{"api_token_secret":"agenc-bot@pve!agenc=%s"}}\n' "$SECRET" > "$CFG"
OUT="$(python3 "$REFRESH" "$CFG" "$TOKEN")"
echo "$OUT" | grep -q "refreshed from" || { echo "FAIL: expected refresh of corrupted value"; echo "$OUT"; exit 1; }
check_secret
echo "    ok"

echo "==> test 3: secret already matches -> kept, no rewrite churn"
M1="$(md5sum "$CFG" | cut -d' ' -f1)"
OUT="$(python3 "$REFRESH" "$CFG" "$TOKEN")"
echo "$OUT" | grep -q "matches — kept" || { echo "FAIL: expected 'kept'"; echo "$OUT"; exit 1; }
M2="$(md5sum "$CFG" | cut -d' ' -f1)"
[ "$M1" = "$M2" ] || { echo "FAIL: file rewritten unnecessarily"; exit 1; }
check_secret
echo "    ok"

echo "==> test 4: token file missing -> warns, leaves config untouched"
rm -f "$TOKEN"
OUT="$(python3 "$REFRESH" "$CFG" "$TOKEN")"
echo "$OUT" | grep -q "missing — leaving existing secret untouched" || { echo "FAIL: no warning"; echo "$OUT"; exit 1; }
check_secret
echo "    ok"

echo
echo "ALL DEPLOY-CONFIG TESTS PASSED"
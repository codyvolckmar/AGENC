#!/usr/bin/env bash
# Deploy the AGENCY website + provisioning API onto the Proxmox node.
#
#   ./deploy/deploy.sh [REPO_DIR]
#
# REPO_DIR defaults to the git checkout you're running from. Files land in
# /opt/agenc, config is generated from the example (secrets auto-created),
# and the API runs as a systemd service behind nginx (if installed).
#
# Then port-forward your web ports (80/443 — whatever the site listens on)
# from your router to THIS node's IP. The API itself listens on :8080 and
# nginx fronts it on :80.
set -euo pipefail

REPO_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
DEST="${DEST:-/opt/agenc}"
API_PORT="${API_PORT:-8080}"
API_HOST="${API_HOST:-0.0.0.0}"
INSTALL_NGINX="${INSTALL_NGINX:-1}"
TOKEN_FILE="${TOKEN_FILE:-/root/.agenc-token}"

PYTHON="${PYTHON:-$(command -v python3)}"
if [ -z "$PYTHON" ]; then echo "!! python3 is required on the node"; exit 1; fi

say() { printf '\n==> %s\n' "$*"; }

say "AGENCY deploy -> $DEST (from $REPO_DIR)"

# --- 1. copy code ---------------------------------------------------------
mkdir -p "$DEST"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'api/config.json' --exclude 'api/data' --exclude 'website/js/config.js' \
    "$REPO_DIR/" "$DEST/"
else
  cp -r "$REPO_DIR/api" "$REPO_DIR/website" "$REPO_DIR/deploy" "$REPO_DIR/docs" "$DEST/" 2>/dev/null || true
fi

# --- 2. generate secrets + config ------------------------------------------
SITE_SECRET="$(openssl rand -hex 24)"
ADMIN_KEY="$(openssl rand -hex 24)"
CFG="$DEST/api/config.json"

if [ ! -f "$CFG" ]; then
  say "Generating $CFG from example (dry_run=false)"
  "$PYTHON" - "$CFG" "$SITE_SECRET" "$ADMIN_KEY" "$API_HOST" "$API_PORT" "$DEST/api/config.example.json" "$TOKEN_FILE" <<'PY'
import json, sys, os
cfg_path, site, admin, host, port, example_path, token_file = sys.argv[1:]
cfg = json.load(open(example_path))
cfg["site_secret"] = site
cfg["admin_key"] = admin
cfg["listen_host"] = host
cfg["listen_port"] = int(port)
if os.path.exists(token_file):
    token = open(token_file).read().strip()
    # file format: PVEAPIToken=agenc-bot@pve!agenc=<secret>  (TWO '=' signs)
    cfg["proxmox"]["api_token_secret"] = token.rsplit("=", 1)[1].strip()
    print("    token secret wired from " + token_file)
else:
    print("    !! " + token_file + " missing — api_token_secret left as placeholder;")
    print("       run proxmox/setup.sh, then re-run this deploy to wire it")
json.dump(cfg, open(cfg_path, "w"), indent=2)
PY
  chmod 600 "$CFG"
else
  # Config already exists (probably from an earlier deploy): refresh the
  # token secret in place so a placeholder/old value never strands the API.
  say "$CFG exists — refreshing proxmox token secret in place"
  "$PYTHON" "$DEST/deploy/refresh_token.py" "$CFG" "$TOKEN_FILE"
  echo "    NOTE: edit $CFG directly (the deployed copy) — not the repo's api/config.json"
fi

# --- 3. website client config ------------------------------------------------
say "Writing $DEST/website/js/config.js"
printf 'window.AGENC_CONFIG = { apiBase: "", siteSecret: "%s" };\n' "$SITE_SECRET" \
  > "$DEST/website/js/config.js"
chmod 644 "$DEST/website/js/config.js"

# --- 4. systemd service ------------------------------------------------------
say "Installing systemd unit 'agenc-api' (:${API_PORT})"
cp "$DEST/deploy/agenc-api.service" /etc/systemd/system/agenc-api.service
sed -i "s|ExecStart=.*|ExecStart=/usr/bin/python3 $DEST/api/app.py $CFG --port $API_PORT|" \
  /etc/systemd/system/agenc-api.service
systemctl daemon-reload
systemctl enable agenc-api >/dev/null 2>&1 || true
# restart, not just enable: the running process caches config at startup,
# so a config/token refresh above must be picked up
echo "    restarting agenc-api…"
systemctl restart agenc-api
systemctl --no-pager --quiet is-active agenc-api \
  && echo "    agenc-api: running" \
  || { echo "!! agenc-api failed to start:"; journalctl -u agenc-api -n 20 --no-pager; exit 1; }

# --- 5. nginx reverse proxy (optional) -----------------------------------------
if [ "$INSTALL_NGINX" = "1" ] && command -v nginx >/dev/null 2>&1; then
  say "Installing nginx reverse proxy (port 80 -> :${API_PORT})"
  cp "$DEST/deploy/nginx-agenc.conf" /etc/nginx/sites-available/agenc.conf
  ln -sf /etc/nginx/sites-available/agenc.conf /etc/nginx/sites-enabled/agenc.conf
  sed -i "s|proxy_pass http://127.0.0.1:8080;|proxy_pass http://127.0.0.1:${API_PORT};|" \
    /etc/nginx/sites-enabled/agenc.conf
  nginx -t && systemctl reload nginx || echo "    !! nginx config failed (check 'nginx -t')"
  SITE_URL="http://$(hostname -I | awk '{print $1}')"
else
  SITE_URL="http://$(hostname -I | awk '{print $1}'):${API_PORT}"
  echo "    nginx not installed — exposing API port directly"
fi

# --- 6. summary ------------------------------------------------------------------
MY_IP="$(hostname -I | awk '{print $1}')"
say "Deployed. Summary:"
echo "    website + API : $SITE_URL"
echo "    api port      : $API_PORT"
echo "    data dir      : $DEST/api/data/agents.json"
echo "    admin key     : $DEST/api/config.json -> admin_key"
echo "    site secret   : $DEST/website/js/config.js"

# vnet preflight: every agent NIC attaches to this
sleep 1
HEALTH="$(curl -s "http://127.0.0.1:${API_PORT}/api/health" 2>/dev/null || true)"
if [ -z "$HEALTH" ]; then
  echo "    !! /api/health unreachable — is agenc-api running? (journalctl -u agenc-api)"
fi
NET_OK="$(printf '%s' "$HEALTH" | grep -o '"net_bridge_ok": *[a-z]*' | head -1 | sed 's/.*: *//')"
PX_OK="$(printf '%s' "$HEALTH" | grep -o '"auth_ok": *[a-z]*' | head -1 | sed 's/.*: *//')"
if [ "$NET_OK" = "true" ]; then
  echo "    vnet preflight : sandbox present — agent NICs will attach"
elif [ "$NET_OK" = "null" ]; then
  echo "    !! net_bridge_ok=null — token may lack SDN.Audit/SDN.Use; re-run proxmox/setup.sh"
else
  echo "    !! net_bridge_ok=false — create the 'sandbox' SDN vnet (or linux bridge) first"
fi
[ "$PX_OK" = "true" ] && echo "    pve auth        : token accepted by the API" \
  || echo "    !! pve auth      : token NOT accepted — check config.json proxmox.api_token_secret"
echo

# safety checks: placeholder values left in config.json?
if grep -q "REPLACE" "$CFG"; then
  echo "    WARNING: '$CFG' still contains REPLACE_* placeholders:"
  grep -n "REPLACE" "$CFG" | sed 's/^/      /'
fi
if grep -q "0x0000000000000000000000000000000000000000" "$CFG"; then
  echo "    WARNING: billing.receive_addresses are still zeroed 0x0000…0000 —"
  echo "      customers cannot pay until you set real USDC/USDT addresses in $CFG"
fi
echo
echo "    NEXT: point your router's port-forward rules at $MY_IP so the"
echo "    website's web ports (80/443) reach this node. Then set your"
echo "    USDC/USDT receive addresses in $CFG (billing.receive_addresses)"
echo "    and restart: systemctl restart agenc-api"
echo
echo "    Public provisioning is gated by site_secret / admin_key. Rotate"
echo "    them in $CFG any time (also rebuild website/js/config.js)."
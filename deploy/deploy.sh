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
import json, sys
cfg_path, site, admin, host, port, example_path, token_file = sys.argv[1:]
cfg = json.load(open(example_path))
cfg["site_secret"] = site
cfg["admin_key"] = admin
cfg["listen_host"] = host
cfg["listen_port"] = int(port)
# wire the Proxmox token secret from /root/.agenc-token if present
try:
    token = open(token_file).read().strip()
    cfg["proxmox"]["api_token_secret"] = token.split("=", 1)[1].strip()
except FileNotFoundError:
    pass
json.dump(cfg, open(cfg_path, "w"), indent=2)
PY
  chmod 600 "$CFG"
else
  say "$CFG already exists — keeping it (run with DEST and delete to regenerate)"
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
systemctl enable --now agenc-api
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
echo
echo "    NEXT: point your router's port-forward rules at $MY_IP so the"
echo "    website's web ports (80/443) reach this node. Then set your"
echo "    USDC/USDT receive addresses in $CFG (billing.receive_addresses)"
echo "    and restart: systemctl restart agenc-api"
echo
echo "    Public provisioning is gated by site_secret / admin_key. Rotate"
echo "    them in $CFG any time (also rebuild website/js/config.js)."
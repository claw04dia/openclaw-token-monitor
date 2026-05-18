#!/usr/bin/env bash
# install.sh — bootstrap the Telegram Token Monitor backend on a new OpenClaw.
#
# What this does (idempotent, safe to re-run):
#   1. Clones this repo to ~/.openclaw/cantiere/telegram-token-monitor/
#   2. Writes the bot token to ~/.openclaw/credentials/telegram-token-monitor.env
#   3. Installs a systemd user unit listening on 127.0.0.1:8899
#   4. Adds a cron entry to rebuild sessions.json every 5 minutes
#   5. Enables Tailscale Funnel on port 8899 (HTTPS public URL, free, auto-cert)
#   6. Calls Telegram setChatMenuButton so the bot's menu opens the central
#      frontend on GitHub Pages with ?api=<this-machine-funnel-url> baked in
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/claw04dia/openclaw-token-monitor/main/install.sh \
#     | bash -s -- --bot-token <BOT_TOKEN> --telegram-user <TELEGRAM_USER_ID>
#
# Or, after manual `git clone`:
#   bash install.sh --bot-token <BOT_TOKEN> --telegram-user <TELEGRAM_USER_ID>

set -euo pipefail

# ─── Configuration (rarely changes) ─────────────────────────────────────────
REPO_URL="https://github.com/claw04dia/openclaw-token-monitor.git"
REPO_BRANCH="main"
INSTALL_DIR="$HOME/.openclaw/cantiere/telegram-token-monitor"
CREDS_DIR="$HOME/.openclaw/credentials"
CREDS_FILE="$CREDS_DIR/telegram-token-monitor.env"
SYSTEMD_DIR="$HOME/.config/systemd/user"
UNIT_NAME="telegram-token-monitor-api.service"
FRONTEND_URL="https://claw04dia.github.io/openclaw-miniapp/"
ALLOW_ORIGIN="https://claw04dia.github.io"
PORT=8899

# ─── Helpers ────────────────────────────────────────────────────────────────
step() { printf "\n→ %s\n" "$*"; }
ok()   { printf "  ✓ %s\n" "$*"; }
warn() { printf "  ⚠ %s\n" "$*" >&2; }
die()  { printf "  ✗ %s\n" "$*" >&2; exit 1; }

# ─── Arg parsing ────────────────────────────────────────────────────────────
BOT_TOKEN=""
TELEGRAM_USER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bot-token)     BOT_TOKEN="${2:-}"; shift 2 ;;
    --telegram-user) TELEGRAM_USER="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0 ;;
    *)
      die "Unknown argument: $1" ;;
  esac
done
[[ -z "$BOT_TOKEN" ]] && die "--bot-token is required"
[[ -z "$TELEGRAM_USER" ]] && die "--telegram-user is required"
[[ "$EUID" -eq 0 ]] && die "do NOT run this as root — it must run as the user that owns ~/.openclaw"

# ─── 1. Prerequisites ───────────────────────────────────────────────────────
step "Checking prerequisites"
for cmd in git python3 curl jq crontab tailscale systemctl; do
  command -v "$cmd" >/dev/null || die "missing command: $cmd (install it with apt and re-run)"
done
ok "git, python3, curl, jq, crontab, tailscale, systemctl all present"

tailscale status >/dev/null 2>&1 || \
  die "tailscale is not logged in — run 'sudo tailscale up' first"
ok "tailscale logged in"

# ─── 2. Validate bot token ──────────────────────────────────────────────────
step "Validating Telegram bot token"
bot_info=$(curl -sf "https://api.telegram.org/bot${BOT_TOKEN}/getMe") || \
  die "bot token is invalid (Telegram getMe failed)"
bot_username=$(echo "$bot_info" | jq -r '.result.username')
[[ -z "$bot_username" || "$bot_username" == "null" ]] && die "could not extract bot username"
ok "bot @$bot_username"

# ─── 3. Clone / update repo ─────────────────────────────────────────────────
step "Installing repo to $INSTALL_DIR"
mkdir -p "$(dirname "$INSTALL_DIR")"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --quiet origin "$REPO_BRANCH"
  git -C "$INSTALL_DIR" checkout --quiet "$REPO_BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only --quiet
  ok "repo already cloned — pulled latest"
else
  git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  ok "repo cloned"
fi

# ─── 4. Credentials env file ────────────────────────────────────────────────
step "Writing credentials"
mkdir -p "$CREDS_DIR"
chmod 700 "$CREDS_DIR"
(umask 077; printf 'TELEGRAM_BOT_TOKEN=%s\n' "$BOT_TOKEN" > "$CREDS_FILE")
chmod 600 "$CREDS_FILE"
ok "$CREDS_FILE (mode 600)"

# ─── 5. systemd user unit ───────────────────────────────────────────────────
step "Installing systemd user unit"
mkdir -p "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/$UNIT_NAME" <<EOF
[Unit]
Description=Token Monitor API (initData-authenticated, bound to 127.0.0.1)
After=default.target

[Service]
Type=simple
EnvironmentFile=$CREDS_FILE
Environment=TELEGRAM_ALLOWED_USER=$TELEGRAM_USER
Environment=PORT=$PORT
Environment=BIND=127.0.0.1
Environment=ALLOW_ORIGIN=$ALLOW_ORIGIN
ExecStart=/usr/bin/python3 $INSTALL_DIR/src/api-server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
ok "$SYSTEMD_DIR/$UNIT_NAME"

# Enable lingering so the user service survives logout
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  if sudo -n true 2>/dev/null; then
    sudo loginctl enable-linger "$USER" && ok "user linger enabled"
  else
    warn "could not enable user linger (no passwordless sudo). Run manually:"
    warn "    sudo loginctl enable-linger $USER"
  fi
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
sleep 1
systemctl --user is-active --quiet "$UNIT_NAME" || {
  journalctl --user -u "$UNIT_NAME" -n 30 --no-pager >&2
  die "$UNIT_NAME failed to start (logs above)"
}
ok "service active on 127.0.0.1:$PORT"

curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || die "/health endpoint not responding"
ok "/health responding"

# ─── 6. Cron for sessions builder ───────────────────────────────────────────
step "Installing cron job for build-sessions.py"
cron_line="*/5 * * * * /usr/bin/python3 $INSTALL_DIR/build-sessions.py >/dev/null 2>&1"
(crontab -l 2>/dev/null | grep -v -F "$INSTALL_DIR/build-sessions.py"; echo "$cron_line") | crontab -
ok "*/5 * * * * build-sessions.py"

# Build once now so the API has data immediately
python3 "$INSTALL_DIR/build-sessions.py" >/dev/null 2>&1 || warn "initial build-sessions.py failed (cron will retry)"

# ─── 7. Tailscale Funnel ────────────────────────────────────────────────────
step "Configuring Tailscale Funnel on port $PORT"
if tailscale funnel status 2>/dev/null | grep -q "/ proxy http://127.0.0.1:$PORT"; then
  ok "funnel already proxying port $PORT"
else
  tailscale funnel --bg "$PORT" >/dev/null || \
    die "tailscale funnel failed — is Funnel enabled on the tailnet? https://tailscale.com/kb/1223/funnel"
  ok "funnel started"
fi

funnel_url=$(tailscale funnel status 2>/dev/null | grep -oE "https://[a-zA-Z0-9.-]+\.ts\.net" | head -1)
[[ -z "$funnel_url" ]] && die "could not determine funnel URL from 'tailscale funnel status'"
ok "public URL: $funnel_url"

# ─── 8. Set Telegram WebApp menu button ─────────────────────────────────────
step "Setting Telegram WebApp menu button"
webapp_url="${FRONTEND_URL}?api=${funnel_url}"
resp=$(curl -sf "https://api.telegram.org/bot${BOT_TOKEN}/setChatMenuButton" \
  -H "Content-Type: application/json" \
  -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"📊 Token Monitor\",\"web_app\":{\"url\":\"$webapp_url\"}}}")
[[ "$(echo "$resp" | jq -r '.ok')" == "true" ]] || die "setChatMenuButton failed: $resp"
ok "menu button → $webapp_url"

# ─── 9. End-to-end CORS check ───────────────────────────────────────────────
step "Verifying CORS preflight from outside"
http=$(curl -sf -o /dev/null -w "%{http_code}" -X OPTIONS \
  -H "Origin: $ALLOW_ORIGIN" \
  -H "Access-Control-Request-Method: GET" \
  "${funnel_url}/api/data" 2>/dev/null || true)
if [[ "$http" == "204" ]]; then
  ok "preflight OK (204)"
else
  warn "preflight returned $http (expected 204) — funnel can take ~10s to propagate; retry the check shortly"
fi

# ─── Done ───────────────────────────────────────────────────────────────────
cat <<EOF

✅ Install complete.

  Bot         https://t.me/$bot_username
  Public API  $funnel_url
  Frontend    $FRONTEND_URL
  Logs        journalctl --user -u $UNIT_NAME -f

Next: open https://t.me/$bot_username, tap the menu button at the bottom of
the chat — the mini-app should load showing data from this OpenClaw.
EOF

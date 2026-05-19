# OpenClaw Token Monitor — Backend

Per-OpenClaw JSON API that powers the **Token Monitor** Telegram Mini App.
Each OpenClaw machine runs its own instance and serves only its own data,
authenticated against its own Telegram bot.

The **frontend** (HTML/CSS/JS) is **not in this repo** — it lives at
[claw04dia/openclaw-miniapp](https://github.com/claw04dia/openclaw-miniapp)
and is served centrally from GitHub Pages
([claw04dia.github.io/openclaw-miniapp](https://claw04dia.github.io/openclaw-miniapp/)).
You do not deploy frontend code here.

## Architecture (one paragraph)

A user opens the bot in Telegram and taps the menu button. Telegram opens the
GitHub-Pages frontend URL with `?api=<this-machine-funnel-url>` baked in. The
frontend reads the `?api=` query string, then sends every request to that URL
with `Authorization: tma <initData>`. The backend on the OpenClaw validates the
HMAC of `initData` against `TELEGRAM_BOT_TOKEN`. Users in
`TELEGRAM_ADMIN_USER` see the full admin dashboard; anyone else who can
authenticate against this bot sees a static onboarding tutorial. Each OpenClaw has its own bot, its
own Funnel URL, and sees only its own trajectories — no cross-tenant exposure.

```
Telegram WebApp button
  └─ https://claw04dia.github.io/openclaw-miniapp/?api=https://<host>.<tailnet>.ts.net
        │  (GitHub Pages serves the static frontend)
        ▼
     fetch /api/data
        Authorization: tma <initData>
        │
        ▼ via Tailscale Funnel (HTTPS, free, auto-cert)
     127.0.0.1:8899  ← api-server.py on the OpenClaw
        validates HMAC with TELEGRAM_BOT_TOKEN
        returns this machine's sessions/spend/prices/cron
```

---

## Install on a new OpenClaw

### Step 1 — Two manual things on Telegram (~2 minutes)

These cannot be automated. The end-user must do them once.

1. **Create a Telegram bot.** Open [@BotFather](https://t.me/BotFather), send
   `/newbot`, follow the prompts, and **save the token** it returns. It looks
   like `1234567890:AAEhBP...`.
2. **Get your Telegram user ID.** Open [@userinfobot](https://t.me/userinfobot)
   and send any message. It will reply with your numeric ID (a 9–10 digit number).

### Step 2 — Prerequisites on the OpenClaw

These are usually pre-installed on an OpenClaw box, but verify:

| Tool         | Why                                                       |
|--------------|-----------------------------------------------------------|
| `tailscale`  | Logged in (`tailscale status` returns OK). Funnel feature enabled on the tailnet — one-time, [admin console → DNS → HTTPS Certificates](https://tailscale.com/kb/1223/funnel) |
| `git`        | Cloning this repo                                         |
| `python3`    | Running `api-server.py` and `build-sessions.py`           |
| `curl`, `jq` | Used by `install.sh` for API calls                        |
| `crontab`    | Scheduling the sessions builder                           |
| `systemctl --user` | Running the API as a user service              |

If anything is missing: `sudo apt install git python3 curl jq cron` (Tailscale
has its own install path — `curl -fsSL https://tailscale.com/install.sh | sh`).

### Step 3 — Run the install script

```bash
curl -fsSL https://raw.githubusercontent.com/claw04dia/openclaw-token-monitor/main/install.sh \
  | bash -s -- --bot-token <BOT_TOKEN_FROM_BOTFATHER> --telegram-user <USER_ID_FROM_USERINFOBOT>
```

The script is **idempotent** — safe to re-run after rotating a token, after a
`git pull`, or to recover from a partial install. It will:

1. Clone the repo to `~/.openclaw/cantiere/telegram-token-monitor/`
2. Write the bot token to `~/.openclaw/credentials/telegram-token-monitor.env` (mode 600)
3. Install systemd user unit `telegram-token-monitor-api.service` (enabled + started)
4. Add a `*/5 * * * *` cron entry to rebuild `~/.cache/token-monitor/sessions.json`
5. Enable Tailscale Funnel on port `8899` and read back the public HTTPS URL
6. Call Telegram `setChatMenuButton` so the bot's menu opens the central
   frontend with `?api=<funnel-url>` baked in
7. Verify the CORS preflight from the GitHub Pages origin returns 204

If any step fails, the script exits non-zero with a clear error. Re-run after
fixing.

### Step 4 — Verify

1. Open the bot in Telegram — `https://t.me/<bot-username>` (the script prints
   the URL at the end)
2. Tap the **📊 Token Monitor** menu button at the bottom of the chat
3. The Mini App opens and shows data from **this** OpenClaw

If the page is blank or shows "Backend non configurato", see
[Troubleshooting](#troubleshooting).

---

## Update an existing install

After backend changes are pushed to GitHub:

```bash
cd ~/.openclaw/cantiere/telegram-token-monitor && \
  git pull --ff-only && \
  systemctl --user restart telegram-token-monitor-api
```

Or simply re-run the install script with the same arguments — it will pull,
re-write the unit (in case env vars changed), and restart.

Frontend updates (HTML/CSS/JS) happen automatically when the maintainer
pushes to `claw04dia/openclaw-miniapp` — no action needed on this machine.

---

## Troubleshooting

### Mini-app shows "Backend non configurato"
The WebApp button URL is missing `?api=...`. Re-run `install.sh` — it will
re-set the menu button correctly.

### 401 on `/api/data`
`TELEGRAM_BOT_TOKEN` is wrong or the initData is expired. Inspect:
```bash
cat ~/.openclaw/credentials/telegram-token-monitor.env
systemctl --user show telegram-token-monitor-api -p Environment
```

### Tutorial shown instead of admin dashboard
Your Telegram user ID is not in `TELEGRAM_ADMIN_USER`. Add it to the systemd
unit's Environment line and `systemctl --user restart telegram-token-monitor-api`.
`TELEGRAM_ALLOWED_USER` is also honoured as a fallback for backward compat.

### Tailscale: "Funnel feature not available"
The tailnet's admin must enable Funnel:
[admin console → DNS → HTTPS Certificates](https://login.tailscale.com/admin/dns)
+ [Settings → Funnel](https://login.tailscale.com/admin/settings/funnel).
Free, one-time, applies tailnet-wide.

### Service won't start
```bash
journalctl --user -u telegram-token-monitor-api -n 50 --no-pager
```
Most common causes: missing env vars, missing `~/.cache/token-monitor/`
(create with `mkdir -p ~/.cache/token-monitor`), port 8899 already in use.

### Mini-app loads but shows "Caricamento…" forever
Check from a regular browser:
```bash
curl -sS -X OPTIONS \
  -H "Origin: https://claw04dia.github.io" \
  -H "Access-Control-Request-Method: GET" \
  https://<this-host>.<tailnet>.ts.net/api/data -I | head -10
```
Expected: `HTTP/2 204` + `Access-Control-Allow-Origin: https://claw04dia.github.io`.
If not, restart the service (`systemctl --user restart …`) and try again.

### Cleanup / uninstall
```bash
systemctl --user disable --now telegram-token-monitor-api
rm ~/.config/systemd/user/telegram-token-monitor-api.service
rm ~/.openclaw/credentials/telegram-token-monitor.env
crontab -l | grep -v build-sessions.py | crontab -
tailscale funnel --bg --serve-port=8899 off || tailscale funnel reset
rm -rf ~/.openclaw/cantiere/telegram-token-monitor ~/.cache/token-monitor
```

---

## Repo layout

```
install.sh           Bootstrap script (see above)
build-sessions.py    Trajectory → sessions.json builder (cron */5)
src/
  api-server.py      JSON API, gated by Telegram initData HMAC
  bot.py             CLI report tool (no HTTP, used by skill)
PLAN.md / PROGRESS.md   Process docs (cantiere)
README.md            This file
```

The API serves four endpoints. Data is admin-only (`TELEGRAM_ADMIN_USER`);
non-admin authenticated users get `{viewer, scope: "viewer"}` so the frontend
renders the static tutorial:
- `GET /health` — unauthenticated liveness probe
- `GET /api/data` — sessions, spend, OpenRouter reconciliation, agent breakdown
- `GET /api/cron` — OpenClaw `jobs.json` agents
- `GET /api/system-cron` — user crontab entries

---

## Security model

| Surface              | Protection                                                |
|----------------------|-----------------------------------------------------------|
| Funnel URL is public | Every `/api/*` request must carry valid Telegram `initData` HMAC signed by this bot's token |
| Bot token            | Stored in `~/.openclaw/credentials/telegram-token-monitor.env` (mode 600). Never in git, never in logs |
| Cross-origin abuse   | `ALLOW_ORIGIN=https://claw04dia.github.io` — browsers reject `/api/*` calls from other origins |
| Cross-tenant access  | Each OpenClaw has its own bot token → only that bot's users can read this backend |
| `auth_date` replay   | `AUTH_MAX_AGE_SECONDS=86400` (24h) max age on initData     |

The **only trust vector** is the central frontend repo
([openclaw-miniapp](https://github.com/claw04dia/openclaw-miniapp)). A
compromise of that repo could serve malicious JS to all installs. Mitigated
by repo MFA + branch protection on the maintainer's side.

---

## Maintainer notes

To ship a backend fix to all installs:
```bash
git commit -am "fix: ..." && git push
```
Each install will pick it up on next `git pull` (manually run or via cron if
the user added one).

To ship a frontend fix: push to `claw04dia/openclaw-miniapp` — propagates
instantly via GitHub Pages.

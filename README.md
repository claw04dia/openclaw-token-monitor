# OpenClaw Token Monitor

Telegram WebApp mini-app that surfaces OpenClaw token spend, session
history, and live multi-agent activity.

Backend is a small Python HTTP server validating Telegram `initData` HMAC
signatures; frontend is vanilla HTML/CSS/JS. Sessions are extracted from
OpenClaw agent trajectories (`~/.openclaw/agents/*/sessions/*.trajectory.jsonl`
and the raw `.jsonl`).

## Layout

```
build-sessions.py    Trajectory → sessions.json builder (cron: */5)
src/
  api-server.py      Auth-gated HTTP backend (systemd: telegram-token-monitor-api)
  bot.py             Telegram bot (sets WebApp button)
  index.html         Mini-app shell (Dashboard / Live / Sessioni / Prezzi)
  js/                app, db, dashboard, live, sessions, prices
  css/               style.css
  manifest.json
```

## Live view

Shows only the current session of each *main* agent (sessions whose
`sessionKey` is neither `:subagent:` nor `:cron:`, ended within 48h) and
nests under each the subagents spawned during that session.

Cost breakdown surfaces parent + subagents + total, so a session like
`Alessia $0.21 + Leo $0.37 + Sofia $0.02 = $0.61` is visible at a glance.

## Running locally

```
python3 build-sessions.py              # rebuild ~/.cache/token-monitor/sessions.json
systemctl --user start telegram-token-monitor-api.service
```

Required env on the systemd service: `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER`, `PORT` (default 8899).

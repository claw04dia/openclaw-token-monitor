# Progress — telegram-token-monitor

**Task:** Monitoraggio token/spesa LLM accessibile da mobile, senza HTTP API esposta
**Avviato:** 2026-05-11 23:50 UTC
**Pivot 1:** 2026-05-12 — mini-app HTML → bot Telegram standalone (long-poll)
**Pivot 2:** 2026-05-12 — bot standalone → skill CLI invocata da Alessia
  (il gateway OpenClaw possiede già il socket bot, Telegram permette un solo consumer)
**Pivot 3:** 2026-05-12 — utente chiede di tornare alla mini-app per UI a colpo d'occhio.
  Soluzione: mini-app statica + backend autenticato via Telegram initData HMAC, esposto
  via Tailscale Funnel. Modello "facade" read-only.
**Stato:** done — manca solo registrazione URL su @BotFather (lato utente)

## Architettura finale

```
┌──────────────────┐  HTTPS (initData HMAC auth)
│ Telegram Mini App│ ─────────────────────────────┐
└──────────────────┘                              │
                                                  ▼
┌────────────────────────────────────────────────────────┐
│ Tailscale Funnel: https://claw04.tail07bec2.ts.net/   │
│                          │                             │
│                          ▼                             │
│              127.0.0.1:8899 (api-server.py)            │
│              ├─ /              → index.html            │
│              ├─ /css/*, /js/*  → static assets         │
│              ├─ /api/data      → JSON (auth required)  │
│              └─ /health        → 200 OK                │
└────────────────────────────────────────────────────────┘
                          │
                          │ reads (read-only)
                          ▼
                  ~/.openclaw/workspace/memory/token-usage.json
                  /tmp/claw04-telegram-miniapp/history.json
```

## Deliverable

**Mini-app (UI):**
- `src/index.html` — bottom-nav 3-sezione, mobile-first
- `src/css/style.css` — dark+light auto, Tailscale-themed
- `src/js/db.js` — fetch autenticato con `Authorization: tma <initData>`
- `src/js/dashboard.js` — budget card + agent bars + bloat list + 7d trend
- `src/js/sessions.js` — lista filtrabile
- `src/js/prices.js` — listino read-only
- `src/js/app.js` — boot, nav, refresh

**Backend autenticato:**
- `src/api-server.py` — HMAC initData validation + allowlist user_id, serve frontend + API sulla stessa origine
- `~/.config/systemd/user/telegram-token-monitor-api.service` — user systemd unit
- Bound su `127.0.0.1:8899`, esposto via Tailscale Funnel

**Skill CLI (canale alternativo):**
- `src/bot.py` — CLI markdown-flavor
- `~/.openclaw/skills/token-monitor/SKILL.md` — trigger NL per Alessia
- Comandi: `today` `spend [N]` `agents` `bloat [N]` `sessions [N]` `models` `refresh` `help`

**Data layer:**
- `~/.openclaw/scripts/build-history.py` — esteso con `agentBreakdown` + `bloated` + agent attribution per session

## UX finale

**Via mini-app:** apre Telegram → tap sul menu del bot → mini-app si apre con
dashboard a colpo d'occhio (budget oggi, spesa per agente, top sessioni gonfie).
Tutti i dati arrivano via API autenticata sulla stessa origine.

**Via chat:** scrive in Telegram al bot → Alessia matcha la skill token-monitor →
output markdown formattato direttamente in chat. Stesso dato, surface diversa.

## Sicurezza

- Backend bind 127.0.0.1 (mai LAN/Internet diretto)
- Tailscale Funnel termina TLS pubblicamente, tunnel cifrato verso la macchina
- `Authorization: tma <initData>` validato con HMAC-SHA256 + bot token
- `auth_date` deve essere < 24h (no replay)
- `user.id` deve essere in allowlist (`TELEGRAM_ALLOWED_USER=529895213`)
- 5 scenari testati: valid → 200, no-auth/wrong-user/tampered-hash/expired → 401
- Bot token solo lato server (systemd env), mai nel frontend
- Path traversal bloccato (`_serve_static` resolve check)
- Verificato anche end-to-end via il dominio Funnel pubblico

## Cleanup eseguito
- ❌ `data-server.py` (era 0.0.0.0:8899 senza auth) → killed
- ❌ Cron `*/30 * * * *` che pushava `history.json` su GitHub pubblico → rimosso
- ❌ `telegram-token-monitor.service` (bot polling autonomo) → rimosso (conflict HTTP 409)
- ❌ `push-data.sh` ripulito → niente più git push a GH Pages
- ✅ `openclaw.json.pre-token-monitor` backup mantenuto

## TODO utente
- Aprire chat con @BotFather → `/setmenubutton` o `/newapp` su @clau04bot
- URL della mini-app: `https://claw04.tail07bec2.ts.net/`


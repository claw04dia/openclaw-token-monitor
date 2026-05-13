#!/usr/bin/env bash
# push-data.sh — rebuilds history.json, sync mini-app static files, push to GitHub Pages.
# No local HTTP server: tutto statico, fetch lato client da github.io.
set -e

HISTORY="/tmp/claw04-telegram-miniapp/history.json"
TOKEN_USAGE="/home/claw04/.openclaw/workspace/memory/token-usage.json"
REPO_DIR="/tmp/claw04-telegram-miniapp"
TARGET="$REPO_DIR/token-monitor"
SRC_DIR="/home/claw04/.openclaw/cantiere/telegram-token-monitor/src"

# 1. Rebuild history.json from daily notes
python3 /home/claw04/.openclaw/scripts/build-history.py

# 2. Combine into data.json consumed by the mini-app
python3 << 'PYEOF'
import json, os

history_path = "/tmp/claw04-telegram-miniapp/history.json"
token_path = "/home/claw04/.openclaw/workspace/memory/token-usage.json"
output_path = "/tmp/claw04-telegram-miniapp/token-monitor/data.json"

MODEL_MAP = {
    'deepseek/deepseek-v4-flash': 'DeepSeek V4 Flash',
    'deepseek/deepseek-v4-pro':   'DeepSeek V4 Pro',
    'moonshotai/kimi-k2.6':       'Kimi K2.6',
    'qwen/qwen3-coder-plus':      'Qwen3 Coder+',
    'google/gemma-4-31b-it':      'Gemma 4 31B',
    'xiaomi/mimo-v2-pro':         'Mimo V2 Pro',
}

data = {"updatedAt": None, "prices": [], "sessions": [],
        "agentBreakdown": [], "bloated": [], "stats": {}}

if os.path.exists(history_path):
    with open(history_path) as f:
        hist = json.load(f)
    data["updatedAt"] = hist.get("updatedAt")

    for mid, p in hist.get("modelPricing", {}).items():
        data["prices"].append({
            "model": MODEL_MAP.get(mid, mid),
            "id": mid,
            "inputPrice": p["in"],
            "outputPrice": p["out"],
        })

    for s in hist.get("sessions", []):
        if s.get("tokensIn", 0) < 200:
            continue
        data["sessions"].append({
            "date": s["date"],
            "time": s.get("time", ""),
            "agent": s.get("agent", "main"),
            "duration": s.get("duration", ""),
            "minutes": s.get("minutes", 0),
            "topic": s["topic"],
            "model": MODEL_MAP.get(s.get("model", ""), s.get("model", "DeepSeek V4 Flash")),
            "tokensIn": s["tokensIn"],
            "tokensOut": s["tokensOut"],
            "cost": round(s.get("costUsd", 0), 6),
        })

    data["agentBreakdown"] = hist.get("agentBreakdown", [])
    data["bloated"] = hist.get("bloated", [])

    stats = hist.get("dailyStats", [])
    data["stats"] = {
        "totalSessions": sum(s.get("sessions", 0) for s in stats),
        "totalTokensIn": sum(s.get("tokensIn", 0) for s in stats),
        "totalTokensOut": sum(s.get("tokensOut", 0) for s in stats),
        "totalCostUsd": round(sum(s.get("costUsd", 0) for s in stats), 4),
        "dailyStats": stats,
    }

if os.path.exists(token_path):
    with open(token_path) as f:
        usage = json.load(f)
    data["stats"]["dailyBudgetUsd"] = usage.get("dailyBudgetUsd", 1.0)
    today = usage.get("days", {})
    if today:
        # Pick most recent day from token-usage.json as todayUsage
        latest = sorted(today.items())[-1][1]
        data["stats"]["todayCostUsd"] = round(latest.get("costUsd", 0), 4)
        data["stats"]["todayTokensIn"] = latest.get("tokensIn", 0)
        data["stats"]["todayTokensOut"] = latest.get("tokensOut", 0)

with open(output_path, 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"✅ data.json: {len(data['sessions'])} sessioni · "
      f"{len(data['agentBreakdown'])} agenti · {len(data['bloated'])} bloated")
PYEOF

# 3. Output summary (data resta locale, viene servito via API autenticata)
python3 -c "
import json
with open('$TARGET/data.json') as f:
    d = json.load(f)
s = d['stats']
print()
print(f'📊  {s[\"totalSessions\"]} sessioni | {s[\"totalTokensIn\"]:,} token in | {s[\"totalTokensOut\"]:,} token out | \${s[\"totalCostUsd\"]:.4f} costo')
print(f'📅  Ultimo aggiornamento: {d.get(\"updatedAt\",\"?\")}')
print(f'💡  Mini-app: https://claw04.tail07bec2.ts.net/  (auth via Telegram initData)')
"

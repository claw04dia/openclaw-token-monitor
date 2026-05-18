#!/usr/bin/env python3
"""Token Monitor — deterministic spending/session reports.

Runs as a CLI: `python3 bot.py <command> [args...]`. Output is markdown-formatted
text designed to be relayed by an LLM agent to Telegram. No HTTP server, no
inbound port, no public publishing — all data stays local.

Commands: today, spend [N], agents, bloat [N], sessions [N], models, refresh, help
"""
import json
import os
import re
import sys
import logging
from collections import defaultdict
from datetime import datetime, timezone

import urllib.request

MEMORY_DIR = os.path.expanduser("~/.openclaw/workspace/memory")
HISTORY_PATH = os.path.expanduser("~/.cache/token-monitor/history.json")
TOKEN_USAGE_PATH = os.path.expanduser("~/.openclaw/workspace/memory/token-usage.json")
AUTH_PROFILES_PATH = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")

NOISE_PATTERNS = [
    r"idle-but-light", r"HEARTBEAT_OK", r"nessun contenuto",
    r"sistema (è|risulta) inattiv", r"^\s*\{\s*\"ok\"",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)

log = logging.getLogger("tmbot")


# ─────────────────────────── Data loaders ───────────────────────────

def load_history() -> dict:
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.warning("history.json unreadable: %s", e)
        return {}


def load_token_usage() -> dict:
    if not os.path.exists(TOKEN_USAGE_PATH):
        return {}
    try:
        with open(TOKEN_USAGE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_openrouter_spend() -> dict:
    """Live spend from OpenRouter /auth/key + /credits. {} on failure."""
    try:
        with open(AUTH_PROFILES_PATH) as f:
            key = json.load(f).get("profiles", {}).get("openrouter:default", {}).get("key", "")
        if not key:
            return {}
        out = {}
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ak = json.loads(r.read()).get("data", {})
        out["today"] = float(ak.get("usage_daily", 0) or 0)
        out["week"]  = float(ak.get("usage_weekly", 0) or 0)
        out["month"] = float(ak.get("usage_monthly", 0) or 0)
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/credits",
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                cr = json.loads(r.read()).get("data", {})
            out["lifetime"]  = float(cr.get("total_usage", 0) or 0)
            out["credits"]   = float(cr.get("total_credits", 0) or 0)
            out["remaining"] = out["credits"] - out["lifetime"]
        except Exception:
            out["lifetime"] = float(ak.get("usage", 0) or 0)
        return out
    except Exception as e:
        log.warning("openrouter fetch failed: %s", e)
        return {}


def parse_minutes(duration: str) -> int:
    m = re.match(r"(\d+)h(\d+)?m?", duration)
    if m:
        return int(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)
    m = re.match(r"(\d+)m", duration)
    return int(m.group(1)) if m else 0


def parse_daily_notes():
    """Yield (date, time, agent, duration_min, topic) from memory MD files."""
    if not os.path.isdir(MEMORY_DIR):
        return
    line_re = re.compile(
        r"^\s*-\s+\*\*(\d{2}:\d{2})\s+(\w+)\*\*\s+_\(([^)]+)\)_\s+[—–-]\s+(.+)$"
    )
    for fname in sorted(os.listdir(MEMORY_DIR)):
        if not re.match(r"\d{4}-\d{2}-\d{2}\.md$", fname):
            continue
        date = fname[:-3]
        path = os.path.join(MEMORY_DIR, fname)
        in_sessions = False
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("## Sessions"):
                    in_sessions = True
                    continue
                if in_sessions and line.startswith("## "):
                    break
                if not in_sessions:
                    continue
                m = line_re.match(line)
                if not m:
                    continue
                t, agent, dur, topic = m.groups()
                mins = parse_minutes(dur)
                topic = topic.strip()
                yield date, t, agent, mins, topic


def cost_for(model_id: str, tok_in: int, tok_out: int, pricing: dict) -> float:
    p = pricing.get(model_id) or {}
    pin = p.get("in") or p.get("inputPerM") or 0.40
    pout = p.get("out") or p.get("outputPerM") or 1.50
    return tok_in / 1e6 * pin + tok_out / 1e6 * pout


# ─────────────────────────── Commands ───────────────────────────

def fmt_usd(v: float) -> str:
    if v < 0.01:
        return f"${v:.4f}"
    if v < 1:
        return f"${v:.3f}"
    return f"${v:.2f}"


def fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def cmd_help(_args) -> str:
    return (
        "*Token Monitor* — comandi:\n\n"
        "📊 `/today` — spesa di oggi vs budget\n"
        "💰 `/spend [N]` — riepilogo ultimi N giorni (default 7)\n"
        "👥 `/agents` — chi spende cosa (main/leo/argo/sofia)\n"
        "🔥 `/bloat [N]` — top N sessioni gonfie (default 10)\n"
        "📋 `/sessions [N]` — ultime N sessioni con contenuto reale\n"
        "🏷 `/models` — listino prezzi correnti\n"
        "🔄 `/refresh` — rigenera history.json dai daily notes\n"
        "❓ `/help` — questo messaggio"
    )


def cmd_today(_args) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    real = fetch_openrouter_spend()
    usage = load_token_usage()
    budget = usage.get("dailyBudgetUsd", 1.0)

    if not real:
        return "⚠️ OpenRouter API non raggiungibile."

    cost = real["today"]
    pct = (cost / budget * 100) if budget else 0
    bar_len = 20
    filled = min(bar_len, int(round(pct / 100 * bar_len)))
    bar = "█" * filled + "░" * (bar_len - filled)
    icon = "🟢" if pct < 50 else "🟡" if pct < 90 else "🔴" if pct < 100 else "🚨"
    over = " *OLTRE BUDGET*" if pct >= 100 else ""

    rem_line = ""
    if "remaining" in real:
        rem_line = f"\nCredito residuo: *{fmt_usd(real['remaining'])}* di {fmt_usd(real['credits'])}"
    return (
        f"*Oggi {today}*{over}\n\n"
        f"{icon} `{bar}` {pct:.0f}%\n"
        f"Oggi: *{fmt_usd(cost)}* / budget {fmt_usd(budget)}\n"
        f"7gg: {fmt_usd(real['week'])} · 30gg: {fmt_usd(real['month'])}\n"
        f"Lifetime: {fmt_usd(real['lifetime'])}{rem_line}\n"
        f"_dati OpenRouter live_"
    )


def cmd_spend(args) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 7
    hist = load_history()
    stats = hist.get("dailyStats") or []
    if not stats:
        return "_Nessun dato in history.json. Lancia /refresh._"
    stats = sorted(stats, key=lambda s: s["date"])[-n:]
    total_in = sum(s.get("tokensIn", 0) for s in stats)
    total_out = sum(s.get("tokensOut", 0) for s in stats)
    total_cost = sum(s.get("costUsd", 0) for s in stats)
    total_sess = sum(s.get("sessions", 0) for s in stats)

    # max cost for bar scaling
    max_cost = max((s.get("costUsd", 0) for s in stats), default=0.001) or 0.001

    lines = [f"*Ultimi {len(stats)} giorni*\n"]
    for s in stats:
        c = s.get("costUsd", 0)
        bar = "▇" * max(1, int(round(c / max_cost * 12)))
        lines.append(
            f"`{s['date'][5:]}` {bar:<13} {fmt_usd(c):>7}  "
            f"({s.get('sessions', 0)}s · {fmt_num(s.get('tokensIn',0))} in)"
        )
    lines.append("")
    lines.append(
        f"*Totale*: {fmt_usd(total_cost)} · {total_sess} sessioni · "
        f"{fmt_num(total_in)} in / {fmt_num(total_out)} out"
    )
    return "\n".join(lines)


def cmd_agents(_args) -> str:
    """Aggregate sessions by agent name from daily notes."""
    agg = defaultdict(lambda: {"sessions": 0, "minutes": 0, "noise": 0})
    for _date, _t, agent, mins, topic in parse_daily_notes():
        a = agg[agent]
        if NOISE_RE.search(topic) or len(topic) < 8:
            a["noise"] += 1
            continue
        a["sessions"] += 1
        a["minutes"] += mins

    if not agg:
        return "_Nessuna daily note trovata._"

    # Estimate cost using per-agent primary model
    hist = load_history()
    pricing = hist.get("modelPricing", {})
    agent_to_model = {
        "main": "deepseek/deepseek-v4-flash",
        "argo": "deepseek/deepseek-v4-flash",
        "sofia": "deepseek/deepseek-v4-flash",
        "leo": "moonshotai/kimi-k2.6",
    }

    rows = []
    for agent, d in sorted(agg.items(), key=lambda x: -x[1]["minutes"]):
        model = agent_to_model.get(agent, "deepseek/deepseek-v4-flash")
        # 500 tok/min in, 50 tok/min out (matches build-history.py heuristic)
        tin = d["minutes"] * 500
        tout = d["minutes"] * 50
        cost = cost_for(model, tin, tout, pricing)
        rows.append((agent, d["sessions"], d["minutes"], d["noise"], cost, model))

    total_cost = sum(r[4] for r in rows) or 1e-6
    lines = ["*Spesa per agente* _(stima da daily notes)_\n"]
    for agent, sess, mins, noise, cost, model in rows:
        pct = cost / total_cost * 100
        bar = "▇" * max(1, int(round(pct / 100 * 15)))
        h = mins // 60
        m = mins % 60
        dur = f"{h}h{m:02d}m" if h else f"{m}m"
        short = model.split("/")[-1][:18]
        lines.append(
            f"*{agent}*  {fmt_usd(cost)} ({pct:.0f}%)\n"
            f"  `{bar}`\n"
            f"  {sess} sess · {dur} attivo · {noise} tick noise · _{short}_"
        )
    lines.append(f"\n_Totale stimato: {fmt_usd(total_cost)}_")
    return "\n".join(lines)


def cmd_bloat(args) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 10
    sessions = []
    for date, t, agent, mins, topic in parse_daily_notes():
        if NOISE_RE.search(topic) or len(topic) < 8:
            continue
        sessions.append((mins, date, t, agent, topic))
    if not sessions:
        return "_Nessuna sessione utile._"
    sessions.sort(reverse=True)
    lines = [f"*Top {n} sessioni gonfie* _(per durata)_\n"]
    for mins, date, t, agent, topic in sessions[:n]:
        h, m = mins // 60, mins % 60
        dur = f"{h}h{m:02d}" if h else f"{m}m"
        # Estimate cost (deepseek flash for non-leo, kimi for leo)
        model = "moonshotai/kimi-k2.6" if agent == "leo" else "deepseek/deepseek-v4-flash"
        cost = cost_for(model, mins * 500, mins * 50, load_history().get("modelPricing", {}))
        topic_short = topic[:70] + ("…" if len(topic) > 70 else "")
        lines.append(
            f"`{date[5:]} {t}` *{agent}* `{dur:>6}` {fmt_usd(cost)}\n"
            f"  _{topic_short}_"
        )
    return "\n".join(lines)


def cmd_sessions(args) -> str:
    n = int(args[0]) if args and args[0].isdigit() else 15
    hist = load_history()
    sessions = hist.get("sessions") or []
    if not sessions:
        return "_history.json vuoto. Lancia /refresh._"
    # Most recent first by date+time
    sessions = sorted(
        sessions,
        key=lambda s: (s.get("date", ""), s.get("time", "")),
        reverse=True,
    )[:n]
    lines = [f"*Ultime {n} sessioni*\n"]
    for s in sessions:
        topic = (s.get("topic") or "")[:80]
        cost = s.get("costUsd", 0)
        tin = s.get("tokensIn", 0)
        dur = s.get("duration", "")
        lines.append(
            f"`{s.get('date','')[5:]} {s.get('time','')}` "
            f"{dur:>5} · {fmt_num(tin)}in · {fmt_usd(cost)}\n"
            f"  _{topic}_"
        )
    return "\n".join(lines)


def cmd_models(_args) -> str:
    hist = load_history()
    pricing = hist.get("modelPricing") or {}
    if not pricing:
        usage = load_token_usage()
        pricing = {k: {"in": v.get("inputPerM"), "out": v.get("outputPerM")}
                   for k, v in usage.get("modelPricing", {}).items()}
    if not pricing:
        return "_Nessun pricing disponibile._"
    lines = ["*Pricing modelli* ($/M token)\n"]
    rows = sorted(pricing.items(), key=lambda x: (x[1].get("out") or 0))
    for mid, p in rows:
        pin = p.get("in") or p.get("inputPerM") or 0
        pout = p.get("out") or p.get("outputPerM") or 0
        short = mid.split("/")[-1]
        lines.append(f"`{short:<24}` in {pin:>5.2f}  out {pout:>5.2f}")
    return "\n".join(lines)


def cmd_refresh(_args) -> str:
    import subprocess
    try:
        r = subprocess.run(
            ["python3", os.path.expanduser("~/.openclaw/scripts/build-history.py")],
            capture_output=True, text=True, timeout=30,
        )
        out = (r.stdout or r.stderr).strip()
        return f"♻️ Refresh:\n```\n{out[:3000]}\n```"
    except Exception as e:
        return f"⚠️ Refresh fallito: {e}"


COMMANDS = {
    "help": cmd_help,
    "today": cmd_today,
    "spend": cmd_spend,
    "agents": cmd_agents,
    "bloat": cmd_bloat,
    "sessions": cmd_sessions,
    "models": cmd_models,
    "refresh": cmd_refresh,
}


def main():
    if len(sys.argv) < 2:
        print(cmd_help([]))
        return
    cmd = sys.argv[1].lstrip("/").lower()
    args = sys.argv[2:]
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"❓ Comando sconosciuto: `{cmd}`\n\n" + cmd_help([]))
        sys.exit(2)
    print(fn(args))


if __name__ == "__main__":
    main()

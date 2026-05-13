#!/usr/bin/env python3
"""Build mini-app sessions.json from actual agent trajectories.

Replaces the duration-based estimation in build-history.py with real token
usage extracted from `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`.

Output: ~/.cache/token-monitor/sessions.json
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = Path.home() / ".openclaw" / "agents"
OUT_PATH = Path.home() / ".cache" / "token-monitor" / "sessions.json"

# OpenRouter pricing in $/M tokens (input / output / cacheRead)
MODEL_PRICING = {
    "deepseek/deepseek-v4-flash":          {"in": 0.14, "out": 0.28, "cache": 0.003},
    "deepseek/deepseek-v4-pro":            {"in": 0.44, "out": 0.87, "cache": 0.004},
    "moonshotai/kimi-k2.6":                {"in": 0.74, "out": 3.49, "cache": 0.14},
    "qwen/qwen3-coder-plus":               {"in": 0.65, "out": 3.25, "cache": 0.13},
    "google/gemma-4-31b-it":               {"in": 0.13, "out": 0.38, "cache": 0.0},
    "google/gemma-4-31b-it:free":          {"in": 0.0,  "out": 0.0,  "cache": 0.0},
    "xiaomi/mimo-v2-pro":                  {"in": 1.0,  "out": 3.0,  "cache": 0.2},
    "z-ai/glm-4.7":                        {"in": 0.38, "out": 1.74, "cache": 0.0},
    "z-ai/glm-4.7-flash":                  {"in": 0.06, "out": 0.40, "cache": 0.01},
    "z-ai/glm-4.5-air":                    {"in": 0.13, "out": 0.85, "cache": 0.025},
    "openai/gpt-4o-mini":                  {"in": 0.15, "out": 0.60, "cache": 0.075},
    "qwen/qwen3.6-35b-a3b":                {"in": 0.30, "out": 0.90, "cache": 0.06},
    "qwen/qwen3-235b-a22b-thinking-2507":  {"in": 0.30, "out": 1.20, "cache": 0.06},
}
FALLBACK_PRICE = {"in": 0.30, "out": 1.0, "cache": 0.0}

# Context window per model in tokens (provider-advertised max).
MODEL_CONTEXT = {
    "deepseek/deepseek-v4-flash":          1_000_000,
    "deepseek/deepseek-v4-pro":            1_000_000,
    "moonshotai/kimi-k2.6":                256_000,
    "qwen/qwen3-coder-plus":               1_000_000,
    "google/gemma-4-31b-it":               131_072,
    "google/gemma-4-31b-it:free":          131_072,
    "xiaomi/mimo-v2-pro":                  200_000,
    "z-ai/glm-4.7":                        200_000,
    "z-ai/glm-4.7-flash":                  128_000,
    "z-ai/glm-4.5-air":                    128_000,
    "openai/gpt-4o-mini":                  128_000,
    "qwen/qwen3.6-35b-a3b":                256_000,
    "qwen/qwen3-235b-a22b-thinking-2507":  256_000,
}

MODEL_DISPLAY = {
    "deepseek/deepseek-v4-flash":    "DeepSeek V4 Flash",
    "deepseek/deepseek-v4-pro":      "DeepSeek V4 Pro",
    "moonshotai/kimi-k2.6":          "Kimi K2.6",
    "qwen/qwen3-coder-plus":         "Qwen3 Coder+",
    "google/gemma-4-31b-it":         "Gemma 4 31B",
    "google/gemma-4-31b-it:free":    "Gemma 4 31B (free)",
    "xiaomi/mimo-v2-pro":            "Mimo V2 Pro",
    "z-ai/glm-4.7":                  "GLM 4.7",
    "z-ai/glm-4.7-flash":            "GLM 4.7 Flash",
    "z-ai/glm-4.5-air":              "GLM 4.5 Air",
    "openai/gpt-4o-mini":            "GPT-4o mini",
    "qwen/qwen3.6-35b-a3b":          "Qwen3.6 35B A3B",
    "qwen/qwen3-235b-a22b-thinking-2507": "Qwen3 235B thinking",
}

# Patterns identifying noise / non-conversational sessions
NOISE_TRIGGERS = (
    "[cron:session-rotator",
    "[OpenClaw heartbeat",
    "[cron:archivista-system-backups",
    "[cron:daily-notes",
)


def cost_for(model: str, usage: dict) -> float:
    """OpenClaw schema: total = input + output + cacheRead (disjoint).
    input = non-cached prompt tokens, cacheRead = cached prompt tokens.
    """
    p = MODEL_PRICING.get(model, FALLBACK_PRICE)
    new_input = int(usage.get("input", 0) or 0)
    output = int(usage.get("output", 0) or 0)
    cache_read = int(usage.get("cacheRead", 0) or 0)
    return new_input / 1e6 * p["in"] + output / 1e6 * p["out"] + cache_read / 1e6 * p["cache"]


TASK_MARKERS = ("## TASK SPECIFICO", "## Your Role", "## YOUR ROLE")


def parse_raw_spawn_events(traj_path: str) -> list[dict]:
    """Scan the canonical `.jsonl` (not the trajectory) for `sessions_spawn`
    toolCall→toolResult pairs.

    The `.trajectory.jsonl` only stores `model.completed` snapshots which truncate
    `messagesSnapshot` to the last ~65 messages — long-running sessions lose old
    spawn events. The raw `.jsonl` keeps every event, so we read it directly.
    """
    raw_path = traj_path.replace(".trajectory.jsonl", ".jsonl")
    if not os.path.exists(raw_path):
        return []
    spawn_calls: dict[str, dict] = {}
    spawn_links: list[dict] = []
    try:
        with open(raw_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "message":
                    continue
                msg = d.get("message") or {}
                role = msg.get("role")
                ts = d.get("timestamp") or ""
                content = msg.get("content")
                if role == "assistant" and isinstance(content, list):
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        if c.get("type") == "toolCall" and c.get("name") == "sessions_spawn":
                            cid = c.get("id")
                            if not cid or cid in spawn_calls:
                                continue
                            args = c.get("arguments") or {}
                            spawn_calls[cid] = {
                                "ts": ts,
                                "task": _real_task(args.get("task") or ""),
                                "model": args.get("model") or "",
                                "runtime": args.get("runtime") or "",
                                "context": args.get("context") or "",
                                "agentId": args.get("agentId") or "",
                                "label": args.get("label") or "",
                            }
                elif role == "toolResult" and msg.get("toolName") == "sessions_spawn":
                    # Raw `.jsonl` stores toolResult on the message itself
                    cid = msg.get("toolCallId") or ""
                    if cid not in spawn_calls:
                        continue
                    det = msg.get("details") or {}
                    if not det:
                        # Fallback: parse the JSON in content[0].text
                        blocks = msg.get("content") or []
                        if isinstance(blocks, list) and blocks:
                            txt = ""
                            for b in blocks:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    txt = b.get("text") or ""
                                    break
                            try:
                                det = json.loads(txt) if txt else {}
                            except Exception:
                                det = {}
                    child_key = det.get("childSessionKey") or ""
                    if not child_key:
                        continue
                    call = spawn_calls[cid]
                    spawn_links.append({
                        "childSessionKey": child_key,
                        "runId": det.get("runId") or "",
                        "status": det.get("status") or "",
                        "ts": call.get("ts") or ts,
                        "task": call.get("task", ""),
                        "modelRequested": call.get("model", ""),
                        "runtime": call.get("runtime", ""),
                        "context": call.get("context", ""),
                        "agentIdRequested": call.get("agentId", ""),
                        "label": call.get("label", ""),
                    })
    except Exception:
        return spawn_links
    return spawn_links


def parse_trajectory(path: str) -> dict | None:
    """Sum usage across ALL model.completed events.

    Each event = one turn (one or more API calls). OpenRouter bills per API call;
    `usage.input/output/cacheRead` in `model.completed` is the cumulative for that
    turn. Summing across all turns = total billable tokens for the session.

    Tracks `usage_per_model` to handle sessions that switched between models
    (fallback chain) — each chunk priced at its own model rate.

    Also captures the session's `sessionKey` (used to classify subagent vs main vs
    cron) and any `sessions_spawn` toolCall→toolResult pairs so we can link a
    parent session to the subagents it triggered.
    """
    started = ended = model = None
    session_key = ""
    per_model_usage: dict[str, dict[str, int]] = {}
    final_prompt = ""
    assistant_text = ""
    subagent_task = ""
    user_prompts: list[str] = []
    turns = 0
    timeline: list[dict] = []  # per-turn breakdown for "live" view
    last_input_max = 0  # peak input tokens seen (≈ peak context window usage)
    # Spawn extraction: collect call args (toolCallId → {ts, task, model, runtime, context})
    # and merge with the matching toolResult (which holds childSessionKey/runId).
    spawn_calls: dict[str, dict] = {}
    spawn_links: list[dict] = []  # finalized {childSessionKey, runId, ts, task, model, ...}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type", "")
                ts = d.get("ts")
                if not session_key and d.get("sessionKey"):
                    session_key = d.get("sessionKey")
                # Track first-ever start and last-ever activity (trajectory spans
                # many rotation rounds; each has its own session.started/ended).
                if ts:
                    if ended is None or ts > ended:
                        ended = ts
                if t == "session.started":
                    if started is None or (ts and ts < started):
                        started = ts
                    if not model:
                        model = d.get("modelId")
                elif t == "model.completed":
                    data = d.get("data", {}) or {}
                    u = data.get("usage") or {}
                    # Walk messagesSnapshot to find sessions_spawn pairs.
                    # Assistant emits a toolCall {name:sessions_spawn, id, arguments}
                    # followed by a toolResult {toolCallId, details.childSessionKey}.
                    ms = data.get("messagesSnapshot") or []
                    for msg in ms:
                        role = msg.get("role")
                        if role == "assistant":
                            content = msg.get("content")
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("name") == "sessions_spawn":
                                        cid = c.get("id")
                                        if cid and cid not in spawn_calls:
                                            args = c.get("arguments") or {}
                                            spawn_calls[cid] = {
                                                "ts": msg.get("timestamp") or ts,
                                                "task": _real_task(args.get("task") or ""),
                                                "model": args.get("model") or "",
                                                "runtime": args.get("runtime") or "",
                                                "context": args.get("context") or "",
                                                "agentId": args.get("agentId") or "",
                                            }
                        elif role == "toolResult" and msg.get("toolName") == "sessions_spawn":
                            cid = msg.get("toolCallId")
                            det = msg.get("details") or {}
                            child_key = det.get("childSessionKey") or ""
                            if not child_key:
                                continue
                            call = spawn_calls.get(cid, {})
                            spawn_links.append({
                                "childSessionKey": child_key,
                                "runId": det.get("runId") or "",
                                "status": det.get("status") or "",
                                "ts": call.get("ts") or msg.get("timestamp") or ts,
                                "task": call.get("task", ""),
                                "modelRequested": call.get("model", ""),
                                "runtime": call.get("runtime", ""),
                                "context": call.get("context", ""),
                                "agentIdRequested": call.get("agentId", ""),
                            })
                    if u.get("input", 0) > 0 or u.get("output", 0) > 0:
                        m = d.get("modelId") or model or "unknown"
                        agg = per_model_usage.setdefault(m, {"input": 0, "output": 0, "cacheRead": 0})
                        agg["input"]     += int(u.get("input", 0) or 0)
                        agg["output"]    += int(u.get("output", 0) or 0)
                        agg["cacheRead"] += int(u.get("cacheRead", 0) or 0)
                        turns += 1
                        # promptCache.lastCallUsage = the LAST individual API call
                        # for this turn — meaningful "single-call context size".
                        lcu = (data.get("promptCache") or {}).get("lastCallUsage") or {}
                        single_call = int(lcu.get("input", 0) or 0) + int(lcu.get("cacheRead", 0) or 0)
                        if single_call > last_input_max:
                            last_input_max = single_call
                    # Always keep the latest non-empty prompt/text for topic extraction
                    if data.get("finalPromptText"):
                        final_prompt = data["finalPromptText"]
                    texts = data.get("assistantTexts") or []
                    if texts and texts[0]:
                        assistant_text = texts[0]
                    # Per-turn record for the "live" view
                    if u.get("input", 0) > 0 or u.get("output", 0) > 0:
                        first_assistant = (texts or [""])[0]
                        timeline.append({
                            "ts": d.get("ts"),
                            "model": d.get("modelId") or model,
                            "tokensIn": int(u.get("input", 0) or 0),
                            "tokensOut": int(u.get("output", 0) or 0),
                            "cacheRead": int(u.get("cacheRead", 0) or 0),
                            "promptPreview": (data.get("finalPromptText") or "")[:400],
                            "replyPreview": (first_assistant or "")[:400],
                        })
                elif t == "trace.artifacts":
                    data = d.get("data", {}) or {}
                    if not final_prompt and data.get("finalPromptText"):
                        final_prompt = data["finalPromptText"]
                    texts = data.get("assistantTexts") or []
                    if not assistant_text and texts and texts[0]:
                        assistant_text = texts[0]
                elif t == "prompt.submitted":
                    p = (d.get("data", {}) or {}).get("text") or ""
                    if p:
                        user_prompts.append(p)
                elif t == "context.compiled" and not subagent_task:
                    sp = (d.get("data", {}) or {}).get("systemPrompt") or ""
                    for marker in TASK_MARKERS:
                        idx = sp.find(marker)
                        if idx >= 0:
                            # take 1–2 paragraphs after the marker
                            chunk = sp[idx + len(marker):].strip()
                            # stop at next ## section or code block
                            stop = len(chunk)
                            for s in ("\n## ", "\n```", "\n---"):
                                k = chunk.find(s)
                                if k > 0 and k < stop:
                                    stop = k
                            subagent_task = chunk[:stop].strip()
                            break
    except Exception:
        return None
    if not started or not model:
        return None

    # Flat totals (across all models used in this session)
    total = {"input": 0, "output": 0, "cacheRead": 0}
    for u in per_model_usage.values():
        total["input"]     += u["input"]
        total["output"]    += u["output"]
        total["cacheRead"] += u["cacheRead"]

    return {
        "started": started,
        "ended": ended or started,
        "model": model,
        "usage": total,
        "perModelUsage": per_model_usage,
        "turns": turns,
        "peakContextTokens": last_input_max,
        "timeline": timeline,
        "finalPrompt": final_prompt,
        "assistantText": assistant_text,
        "subagentTask": subagent_task,
        "userPrompts": user_prompts,
        "path": path,
        "sessionKey": session_key,
        # Merge trajectory-derived links with raw-jsonl ones (the latter catches
        # spawns that fell off the truncated messagesSnapshot in long sessions).
        "spawnLinks": _merge_spawn_links(spawn_links, parse_raw_spawn_events(path)),
    }


def _merge_spawn_links(a: list[dict], b: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for link in (a or []) + (b or []):
        key = link.get("childSessionKey") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(link)
    return out


def _real_task(task: str) -> str:
    """Strip the canonical persona-prefix (Sofia/Leo/etc. role doc) and return
    the actual task line. Alessia prepends the persona before her real ask;
    we want to show the ask, not the boilerplate.
    """
    if not task:
        return ""
    # Persona blocks start with "# LEO" / "# SOFIA" / "# CHIARA" etc. and end at the
    # "## TASK SPECIFICO" or "---" marker (last separator before the real ask).
    for marker in ("\n## TASK SPECIFICO", "\n## TASK", "\n## Compito", "\n## Compito specifico"):
        idx = task.find(marker)
        if idx >= 0:
            tail = task[idx + len(marker):].lstrip("\n :")
            first = tail.split("\n", 1)[0].strip()
            if len(first) >= 4:
                return tail[:600].strip()
    # Fallback: take everything after the last `---` separator, since persona
    # docs end with `---` before the real task.
    parts = task.rsplit("\n---", 1)
    if len(parts) == 2 and len(parts[1].strip()) >= 10:
        return parts[1].strip()[:600]
    return task[:600]


def is_substantial(s: dict) -> bool:
    fp = s.get("finalPrompt") or ""
    at = (s.get("assistantText") or "").lower()
    if any(fp.startswith(prefix) for prefix in NOISE_TRIGGERS):
        return False
    if any(k in at for k in ("idle-but-light", "heartbeat_ok", "nessun contenuto")):
        return False
    out_tok = (s.get("usage") or {}).get("output", 0) or 0
    if out_tok < 50:
        return False
    return True


PLACEHOLDER_RE = re.compile(r"<[^>]{3,}>")  # detect <descrizione del task...> template stubs
TELEGRAM_PREFIX_RE = re.compile(
    r"^\[Telegram\s+[^\]]+id:\d+[^\]]*\]\s*", re.MULTILINE
)


def extract_topic(s: dict) -> str:
    """Try multiple sources in order of fidelity to the user's actual ask."""

    # Priority 1: subagent task (Leo/Sofia spawned with explicit "## TASK SPECIFICO")
    task = (s.get("subagentTask") or "").strip()
    if task and not PLACEHOLDER_RE.match(task):
        for line in task.split("\n"):
            line = line.strip()
            if line and not PLACEHOLDER_RE.match(line) and len(line) >= 8:
                return line[:200]

    # Priority 2: explicit user prompts (some sessions log them as events)
    for p in (s.get("userPrompts") or []):
        first = p.strip().split("\n")[0]
        first = re.sub(r"^[\[\(].*?[\]\)]\s*", "", first)
        if len(first) >= 4:
            return first[:200]

    # Priority 3: finalPromptText with Telegram prefix — extract user's actual message
    fp = (s.get("finalPrompt") or "").strip()
    if fp:
        # Strip Telegram-headers prefix; take first content line
        body = TELEGRAM_PREFIX_RE.sub("", fp)
        # Skip [media attached: ...] / [Forwarded ...] / <media:.../> / <file ...>
        for line in body.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                continue
            if line.startswith("<") and line.endswith(">"):
                continue
            if line.startswith("<<<") or line.startswith(">>>"):
                continue
            if line.startswith("# ") or line.startswith("## "):
                # Markdown heading is often the topic itself
                return line.lstrip("# ").strip()[:200]
            if len(line) >= 8:
                return line[:200]

    # Priority 4: last assistant text
    at = (s.get("assistantText") or "").strip().split("\n")[0]
    if len(at) >= 8:
        return at[:200]

    return "session " + (s.get("started") or "?")[:16]


def duration_minutes(started: str, ended: str) -> int:
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds() / 60))
    except Exception:
        return 0


def fmt_duration(minutes: int) -> str:
    if minutes >= 60:
        return f"{minutes // 60}h{minutes % 60:02d}m"
    return f"{minutes}m"


def clean_excerpt(text: str, max_len: int = 220) -> str:
    """Strip Telegram metadata + media markup from a prompt excerpt."""
    if not text:
        return ""
    body = TELEGRAM_PREFIX_RE.sub("", text)
    out_lines = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if line.startswith("<") and line.endswith(">"):
            continue
        if line.startswith("<<<") or line.startswith(">>>"):
            continue
        out_lines.append(line)
    joined = " ".join(out_lines)
    return joined[:max_len]


def build() -> dict:
    pattern = str(AGENTS_DIR / "*" / "sessions" / "*.trajectory.jsonl")
    raw = []
    for path in glob.glob(pattern):
        parts = path.split(os.sep)
        try:
            agent = parts[parts.index("agents") + 1]
        except ValueError:
            continue
        s = parse_trajectory(path)
        if not s:
            continue
        s["agent"] = agent
        raw.append(s)

    subs = [s for s in raw if is_substantial(s)]

    # Index every parsed session (substantial or not) by sessionKey so we can
    # resolve `sessions_spawn` childSessionKeys to the actual child trajectory.
    # Subagents are often short-lived and would be filtered by is_substantial,
    # but we still want to display them when nested under their parent.
    sessions_by_key: dict[str, dict] = {}
    for s in raw:
        sk = s.get("sessionKey") or ""
        if not sk:
            continue
        existing = sessions_by_key.get(sk)
        # Prefer the most recently-ended one (rotation may produce duplicates).
        if not existing or (s.get("ended") or "") > (existing.get("ended") or ""):
            sessions_by_key[sk] = s

    # Build session records — cost summed across per-model chunks
    records = []
    for s in subs:
        mins = duration_minutes(s["started"], s["ended"])
        u = s.get("usage") or {}
        tin = int(u.get("input", 0) or 0)
        tout = int(u.get("output", 0) or 0)
        tcache = int(u.get("cacheRead", 0) or 0)
        # Cost per model (handles fallback chain)
        c = sum(cost_for(model_id, pu) for model_id, pu in (s.get("perModelUsage") or {}).items())
        # Date attribution: use ENDED date so a session straddling midnight
        # lands on the day where the bulk of the work actually happened
        # (matches user perception of "what I spent today").
        records.append({
            "id": os.path.basename(s["path"]).split(".")[0],
            "agent": s["agent"],
            "model": s["model"],
            "modelDisplay": MODEL_DISPLAY.get(s["model"], s["model"]),
            "date": s["ended"][:10],
            "startedAt": s["started"],
            "endedAt": s["ended"],
            "time": s["started"][11:16],
            "endTime": s["ended"][11:16],
            "duration": fmt_duration(mins),
            "minutes": mins,
            "turns": s.get("turns", 0),
            "tokensIn": tin,
            "tokensOut": tout,
            "cacheRead": tcache,
            "totalTokens": tin + tout,
            "cost": round(c, 6),
            "topic": extract_topic(s),
        })

    # Sort newest first by actual completion time (endedAt), not by start time.
    # A session 23:50→00:42 has start-time 23:50 but ended this morning;
    # sorting by start would put it ahead of today's 14:00 sessions.
    records.sort(key=lambda r: r.get("endedAt", r["date"] + "T" + r["time"]), reverse=True)

    # Daily aggregation (from real records, not estimates)
    daily = defaultdict(lambda: {"sessions": 0, "tokensIn": 0, "tokensOut": 0,
                                 "cacheRead": 0, "cost": 0.0})
    for r in records:
        d = daily[r["date"]]
        d["sessions"] += 1
        d["tokensIn"] += r["tokensIn"]
        d["tokensOut"] += r["tokensOut"]
        d["cacheRead"] += r["cacheRead"]
        d["cost"] += r["cost"]
    daily_list = [{"date": k, **{kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                  for kk, vv in v.items()}}
                  for k, v in sorted(daily.items())]

    # Agent breakdown (real tokens, real cost)
    agg = defaultdict(lambda: {"sessions": 0, "minutes": 0, "tokensIn": 0,
                               "tokensOut": 0, "cacheRead": 0, "cost": 0.0,
                               "models": defaultdict(int)})
    for r in records:
        a = agg[r["agent"]]
        a["sessions"] += 1
        a["minutes"] += r["minutes"]
        a["tokensIn"] += r["tokensIn"]
        a["tokensOut"] += r["tokensOut"]
        a["cacheRead"] += r["cacheRead"]
        a["cost"] += r["cost"]
        a["models"][r["model"]] += 1
    agent_breakdown = []
    for agent, v in sorted(agg.items(), key=lambda x: -x[1]["cost"]):
        top_model = max(v["models"].items(), key=lambda x: x[1])[0] if v["models"] else None
        agent_breakdown.append({
            "agent": agent,
            "sessions": v["sessions"],
            "minutes": v["minutes"],
            "tokensIn": v["tokensIn"],
            "tokensOut": v["tokensOut"],
            "cacheRead": v["cacheRead"],
            "cost": round(v["cost"], 6),
            "topModel": top_model,
            "topModelDisplay": MODEL_DISPLAY.get(top_model, top_model) if top_model else None,
        })

    # Bloated sessions: top by cost (more informative than by duration with real data)
    bloated = sorted(records, key=lambda r: -r["cost"])[:30]

    # "Live" view: only the current session of each *main* agent — sessions
    # whose sessionKey is neither a subagent nor a cron run. The subagents
    # that the main session spawned are nested inside as `subagents: [...]`.
    def _session_kind(sk: str) -> str:
        if ":subagent:" in sk:
            return "subagent"
        if ":cron:" in sk:
            return "cron"
        return "main"

    def _subagent_card(child: dict, link: dict) -> dict:
        """Build a compact card for a child session referenced by a spawn link."""
        mins = duration_minutes(child["started"], child["ended"])
        c = sum(cost_for(m, u) for m, u in (child.get("perModelUsage") or {}).items())
        u = child.get("usage") or {}
        # Parent agent + child id from sessionKey: agent:<parent>:subagent:<uuid>
        sk = child.get("sessionKey") or ""
        parts = sk.split(":")
        parent_agent = parts[1] if len(parts) > 2 else child.get("agent", "")
        child_id_short = parts[-1][:8] if parts else ""
        return {
            "agent": child.get("agent", parent_agent),
            "sessionId": os.path.basename(child["path"]).split(".")[0],
            "sessionKey": sk,
            "childIdShort": child_id_short,
            "model": child["model"],
            "modelDisplay": MODEL_DISPLAY.get(child["model"], child["model"]),
            "started": child["started"],
            "ended": child["ended"],
            "duration": fmt_duration(mins),
            "turns": child.get("turns", 0),
            "tokensIn": int(u.get("input", 0) or 0),
            "tokensOut": int(u.get("output", 0) or 0),
            "cacheRead": int(u.get("cacheRead", 0) or 0),
            "cost": round(c, 6),
            "task": link.get("task", ""),
            "topic": extract_topic(child),
            "spawnedAt": link.get("ts"),
            "status": link.get("status", ""),
        }

    # Pick the latest *main* session per agent (one card per agent), and only
    # include it if it ended within the last 48h — otherwise it's not really
    # "current" and just adds noise (e.g. an old direct-mode session for an
    # agent that's been demoted to subagent-only).
    LIVE_CUTOFF_HOURS = 48
    cutoff_iso = (datetime.now(timezone.utc).timestamp() - LIVE_CUTOFF_HOURS * 3600)
    def _ts_to_epoch(iso: str) -> float:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    latest_main_by_agent: dict[str, dict] = {}
    for s in subs:
        if _session_kind(s.get("sessionKey") or "") != "main":
            continue
        if _ts_to_epoch(s.get("ended") or "") < cutoff_iso:
            continue
        existing = latest_main_by_agent.get(s["agent"])
        if not existing or s["started"] > existing["started"]:
            latest_main_by_agent[s["agent"]] = s

    current_by_agent = []
    for agent, s in latest_main_by_agent.items():
        mins = duration_minutes(s["started"], s["ended"])
        # Cost
        c = sum(cost_for(m, u) for m, u in (s.get("perModelUsage") or {}).items())
        # Context window + peak single-call context (max of promptCache.lastCallUsage
        # across turns — meaningful single-API-call size, unlike per-turn cumulative).
        ctx_window = MODEL_CONTEXT.get(s["model"], 0)
        peak_single = s.get("peakContextTokens", 0)
        # Total session tokens (cumulative input+output+cache across all turns).
        # Matches what OpenClaw's `/context` command reports.
        total_session = int((s.get("usage") or {}).get("input", 0)) \
                      + int((s.get("usage") or {}).get("output", 0)) \
                      + int((s.get("usage") or {}).get("cacheRead", 0))
        # Per-turn summary (last 10 turns)
        turns_view = []
        for tn in (s.get("timeline") or [])[-10:]:
            turns_view.append({
                "ts": tn["ts"],
                "model": tn["model"],
                "modelDisplay": MODEL_DISPLAY.get(tn["model"], tn["model"]),
                "tokensIn": tn["tokensIn"],
                "tokensOut": tn["tokensOut"],
                "cacheRead": tn["cacheRead"],
                "promptPreview": clean_excerpt(tn["promptPreview"], 200),
                "replyPreview": tn["replyPreview"][:200],
            })
        # Resolve subagent spawn links → child session details.
        sub_cards = []
        seen_child_keys = set()
        for link in (s.get("spawnLinks") or []):
            ck = link.get("childSessionKey") or ""
            if not ck or ck in seen_child_keys:
                continue
            seen_child_keys.add(ck)
            child = sessions_by_key.get(ck)
            if child:
                sub_cards.append(_subagent_card(child, link))
            else:
                # Spawn happened but child trajectory not (yet) found — show stub.
                parts = ck.split(":")
                sub_cards.append({
                    "agent": parts[1] if len(parts) > 1 else "?",
                    "sessionId": "",
                    "sessionKey": ck,
                    "childIdShort": parts[-1][:8] if parts else "",
                    "model": link.get("modelRequested") or "",
                    "modelDisplay": MODEL_DISPLAY.get(link.get("modelRequested") or "", link.get("modelRequested") or ""),
                    "started": link.get("ts"),
                    "ended": link.get("ts"),
                    "duration": "—",
                    "turns": 0, "tokensIn": 0, "tokensOut": 0, "cacheRead": 0,
                    "cost": 0,
                    "task": link.get("task", ""),
                    "topic": (link.get("task", "") or "").split("\n")[0][:200] or "(in attesa)",
                    "spawnedAt": link.get("ts"),
                    "status": link.get("status") or "pending",
                })
        sub_cards.sort(key=lambda x: x.get("spawnedAt") or "", reverse=True)

        sub_cost = sum(sc.get("cost", 0) or 0 for sc in sub_cards)
        sub_tokens_in = sum(sc.get("tokensIn", 0) or 0 for sc in sub_cards)
        sub_tokens_out = sum(sc.get("tokensOut", 0) or 0 for sc in sub_cards)
        sub_cache = sum(sc.get("cacheRead", 0) or 0 for sc in sub_cards)
        sub_turns = sum(sc.get("turns", 0) or 0 for sc in sub_cards)

        current_by_agent.append({
            "agent": agent,
            "sessionId": os.path.basename(s["path"]).split(".")[0],
            "sessionKey": s.get("sessionKey") or "",
            "model": s["model"],
            "modelDisplay": MODEL_DISPLAY.get(s["model"], s["model"]),
            "contextWindow": ctx_window,
            "peakSingleCall": peak_single,
            "sessionTokens": total_session,
            "started": s["started"],
            "ended": s["ended"],
            "duration": fmt_duration(mins),
            "minutes": mins,
            "turns": s.get("turns", 0),
            "tokensIn": int((s.get("usage") or {}).get("input", 0)),
            "tokensOut": int((s.get("usage") or {}).get("output", 0)),
            "cacheRead": int((s.get("usage") or {}).get("cacheRead", 0)),
            "cost": round(c, 6),
            # Cost breakdown: parent (direct) vs subagents (spawned children).
            # The header card uses `totalCost` so the user sees the real spend
            # of "this session and everything it triggered" at a glance.
            "parentCost": round(c, 6),
            "subagentsCost": round(sub_cost, 6),
            "totalCost": round(c + sub_cost, 6),
            "subagentsTurns": sub_turns,
            "subagentsTokensIn": sub_tokens_in,
            "subagentsTokensOut": sub_tokens_out,
            "subagentsCacheRead": sub_cache,
            "topic": extract_topic(s),
            "lastTurns": turns_view,
            "subagents": sub_cards,
        })

    # Sort: most recent first
    current_by_agent.sort(key=lambda x: x["started"], reverse=True)

    return {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessionsCount": len(records),
        "totalCost": round(sum(r["cost"] for r in records), 4),
        "totalTokensIn": sum(r["tokensIn"] for r in records),
        "totalTokensOut": sum(r["tokensOut"] for r in records),
        "totalCacheRead": sum(r["cacheRead"] for r in records),
        "sessions": records,
        "daily": daily_list,
        "agentBreakdown": agent_breakdown,
        "bloated": bloated,
        "currentByAgent": current_by_agent,
        "pricing": MODEL_PRICING,
    }


def main():
    t0 = time.time()
    payload = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    elapsed = time.time() - t0
    print(f"✅ sessions.json: {payload['sessionsCount']} sessions · "
          f"${payload['totalCost']:.4f} cost · "
          f"{len(payload['daily'])} days · "
          f"{elapsed:.2f}s")


if __name__ == "__main__":
    main()

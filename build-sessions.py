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
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = Path.home() / ".openclaw" / "agents"
OUT_PATH = Path.home() / ".cache" / "token-monitor" / "sessions.json"

# OpenRouter pricing in $/M tokens (input / output / cacheRead).
# These are the authoritative fallback values — at runtime we ALSO load the
# live OpenRouter price catalog from ~/.openclaw/cache/openrouter-models.json
# (populated by the gateway) and prefer those, so prices stay in sync with
# what OpenRouter actually bills.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "deepseek/deepseek-v4-flash":          {"in": 0.126, "out": 0.252, "cache": 0.0252},
    "deepseek/deepseek-v4-pro":            {"in": 0.435, "out": 0.87,  "cache": 0.003625},
    "moonshotai/kimi-k2.6":                {"in": 0.73,  "out": 3.49,  "cache": 0.25},
    "qwen/qwen3-coder-plus":               {"in": 0.65,  "out": 3.25,  "cache": 0.13},
    "google/gemma-4-31b-it":               {"in": 0.12,  "out": 0.37,  "cache": 0.0},
    "google/gemma-4-31b-it:free":          {"in": 0.0,   "out": 0.0,   "cache": 0.0},
    "google/gemma-4-26b-a4b-it":           {"in": 0.06,  "out": 0.33,  "cache": 0.0},
    "xiaomi/mimo-v2-pro":                  {"in": 1.0,   "out": 3.0,   "cache": 0.2},
    "xiaomi/mimo-v2.5-pro":                {"in": 1.0,   "out": 3.0,   "cache": 0.2},
    "z-ai/glm-4.7":                        {"in": 0.40,  "out": 1.75,  "cache": 0.08},
    "z-ai/glm-4.7-flash":                  {"in": 0.06,  "out": 0.40,  "cache": 0.01},
    "z-ai/glm-4.5-air":                    {"in": 0.13,  "out": 0.85,  "cache": 0.025},
    "openai/gpt-4o-mini":                  {"in": 0.15,  "out": 0.60,  "cache": 0.075},
    "qwen/qwen3.6-35b-a3b":                {"in": 0.15,  "out": 1.0,   "cache": 0.05},
    "qwen/qwen3-30b-a3b":                  {"in": 0.09,  "out": 0.45,  "cache": 0.0},
    "qwen/qwen3-235b-a22b-thinking-2507":  {"in": 0.30,  "out": 1.20,  "cache": 0.06},
    "anthropic/claude-sonnet-4.6":         {"in": 3.0,   "out": 15.0,  "cache": 0.30},
    "anthropic/claude-opus-4-7":           {"in": 15.0,  "out": 75.0,  "cache": 1.50},
    "anthropic/claude-haiku-4.5":          {"in": 1.0,   "out": 5.0,   "cache": 0.10},
}
FALLBACK_PRICE = {"in": 0.30, "out": 1.0, "cache": 0.0}

OPENROUTER_MODELS_CACHE = Path.home() / ".openclaw" / "cache" / "openrouter-models.json"


def _load_openrouter_prices() -> None:
    """Merge prices from OpenClaw's openrouter-models.json cache (refreshed by
    the gateway) into MODEL_PRICING so we track the same numbers OpenRouter
    actually charges. The cached file has shape {models: {id: {cost: {input,
    output, cacheRead, cacheWrite}}}}.

    OpenRouter uses sentinel models (openrouter/auto, pareto-code, …) priced
    at -1_000_000 to mark "no fixed price". Skip those — they'd make every
    aggregate sum into a giant negative number."""
    try:
        with open(OPENROUTER_MODELS_CACHE) as f:
            doc = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    models = doc.get("models") or {}
    for mid, info in models.items():
        cost = (info or {}).get("cost") or {}
        if "input" not in cost or "output" not in cost:
            continue
        try:
            pin   = float(cost.get("input",     0) or 0)
            pout  = float(cost.get("output",    0) or 0)
            pcache = float(cost.get("cacheRead", 0) or 0)
        except (TypeError, ValueError):
            continue
        # Skip sentinel/invalid entries (negative prices, absurd values).
        if pin < 0 or pout < 0 or pcache < 0:
            continue
        if pin > 1000 or pout > 1000:  # safety bound, real models stay under $1k/M
            continue
        MODEL_PRICING[mid] = {"in": pin, "out": pout, "cache": pcache}


_load_openrouter_prices()

# Models that don't actually hit OpenRouter (local inference, gateway internals).
# Charged at zero so they don't pollute the OpenRouter reconciliation.
LOCAL_MODELS = ("hf:", "gateway-injected", "local:")

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


def _normalize_model(model: str) -> str:
    """OpenClaw stores some models with an `openrouter/` prefix (the gateway
    namespace). Pricing lookups use the bare provider/model form, so strip it.
    """
    if not model:
        return ""
    if model.startswith("openrouter/"):
        return model[len("openrouter/"):]
    return model


def _price_for_model(model: str) -> dict:
    """Resolve pricing for a model id. Returns FALLBACK_PRICE when unknown so
    callers can keep summing, but the model is logged so we notice gaps."""
    if not model:
        return FALLBACK_PRICE
    if any(model.startswith(p) for p in LOCAL_MODELS):
        return {"in": 0.0, "out": 0.0, "cache": 0.0}
    norm = _normalize_model(model)
    if norm in MODEL_PRICING:
        return MODEL_PRICING[norm]
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    _MISSING_MODELS.add(model)
    return FALLBACK_PRICE


_MISSING_MODELS: set[str] = set()


def cost_for(model: str, usage: dict) -> float:
    """OpenClaw schema: total = input + output + cacheRead (disjoint).
    input = non-cached prompt tokens, cacheRead = cached prompt tokens.
    """
    p = _price_for_model(model)
    new_input = int(usage.get("input", 0) or 0)
    output = int(usage.get("output", 0) or 0)
    cache_read = int(usage.get("cacheRead", 0) or 0)
    return new_input / 1e6 * p["in"] + output / 1e6 * p["out"] + cache_read / 1e6 * p["cache"]


def model_breakdown(per_model_usage: dict, primary_model: str) -> tuple[list[dict], float, bool]:
    """Convert perModelUsage map → sorted list of {model, tokensIn, tokensOut,
    cacheRead, cost, isPrimary} entries, plus the cost attributed to NON-primary
    models (fallback spend) and whether any fallback actually happened.

    A session is "fallbackUsed" only if a non-primary model actually billed
    tokens — single-model sessions never count even when the primary is
    formally an alias for a chained model.
    """
    if not per_model_usage:
        return [], 0.0, False
    entries: list[dict] = []
    fallback_cost = 0.0
    for mid, u in per_model_usage.items():
        c = cost_for(mid, u)
        is_primary = (mid == primary_model)
        if not is_primary:
            fallback_cost += c
        entries.append({
            "model": mid,
            "modelDisplay": MODEL_DISPLAY.get(mid, mid),
            "tokensIn": int(u.get("input", 0) or 0),
            "tokensOut": int(u.get("output", 0) or 0),
            "cacheRead": int(u.get("cacheRead", 0) or 0),
            "cost": round(c, 6),
            "isPrimary": is_primary,
        })
    entries.sort(key=lambda e: -e["cost"])
    used = sum(1 for e in entries if e["tokensIn"] or e["tokensOut"])
    return entries, round(fallback_cost, 6), used > 1


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
                            "promptPreview": strip_runtime_prefix(data.get("finalPromptText") or "").strip()[:400],
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
    """Display-only filter: returns True when a session is worth showing in
    the Sessions list. Noise sessions (cron heartbeats, daily-notes, rotators)
    still get tracked for cost reconciliation against OpenRouter — this only
    controls visibility in the UI."""
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


def _classify_noise(s: dict) -> str:
    """Return a label for *why* a session is noise (empty if substantial).
    Used to attribute hidden spend in the UI ("$6 from cron rotator")."""
    fp = s.get("finalPrompt") or ""
    at = (s.get("assistantText") or "").lower()
    for prefix in NOISE_TRIGGERS:
        if fp.startswith(prefix):
            return prefix.strip("[]:").split(":", 1)[-1] or "system"
    for k in ("idle-but-light", "heartbeat_ok", "nessun contenuto"):
        if k in at:
            return "idle"
    out_tok = (s.get("usage") or {}).get("output", 0) or 0
    if out_tok < 50:
        return "tiny"
    return ""


PLACEHOLDER_RE = re.compile(r"<[^>]{3,}>")  # detect <descrizione del task...> template stubs
TELEGRAM_PREFIX_RE = re.compile(
    r"^\[Telegram\s+[^\]]+id:\d+[^\]]*\]\s*", re.MULTILINE
)
# Strip OpenClaw runtime-context blocks: `<Name> (untrusted metadata):` headers
# followed by a fenced ```json … ``` body. Multiple blocks (Conversation info,
# Sender, …) usually stack at the top of a Telegram prompt.
UNTRUSTED_META_BLOCK_RE = re.compile(
    r"^[^\n]*\(untrusted metadata\):[ \t]*\n```(?:json)?\n[\s\S]*?\n```[ \t]*\n?",
    re.MULTILINE,
)


def strip_runtime_prefix(text: str) -> str:
    """Remove Telegram/runtime metadata wrappers from a user prompt."""
    if not text:
        return text
    text = TELEGRAM_PREFIX_RE.sub("", text)
    text = UNTRUSTED_META_BLOCK_RE.sub("", text)
    return text


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
        body = strip_runtime_prefix(p).strip()
        for line in body.split("\n"):
            first = line.strip()
            first = re.sub(r"^[\[\(].*?[\]\)]\s*", "", first)
            if len(first) >= 4:
                return first[:200]

    # Priority 3: finalPromptText with Telegram prefix — extract user's actual message
    fp = (s.get("finalPrompt") or "").strip()
    if fp:
        # Strip Telegram-headers prefix; take first content line
        body = strip_runtime_prefix(fp)
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
    body = strip_runtime_prefix(text)
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


# Rough per-call estimates for cost approximation of embedded runs that
# don't write trajectory files. We don't know the actual tokens (the journal
# omits them), so each call counts as "an attempt against this model with
# this typical request size". The figures match the orders of magnitude we
# saw in the gateway logs (max_tokens 4k/16k/32k, prompt ~1-2k tokens).
GATEWAY_CALL_PROFILE = {
    # source : { "tokensIn": int, "tokensOut": int }
    "active-memory":  {"tokensIn": 1500, "tokensOut": 200},
    "session-embed":  {"tokensIn": 1500, "tokensOut": 300},
    "announce":       {"tokensIn": 800,  "tokensOut": 200},
}

_GATEWAY_RUN_RE = re.compile(
    r"runId=(?P<runId>\S+)\s+isError=(?P<isError>\w+)\s+model=(?P<model>\S+)"
)
_GATEWAY_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})T")


_GATEWAY_END_RE = re.compile(
    r"runId=(?P<runId>\S+)\s+isError=(?P<isError>\w+)\s+model=(?P<model>\S+)\s+provider=(?P<provider>\S+)"
)
_GATEWAY_CTXOVF_RE = re.compile(
    r"requested about (?P<requested>\d+) tokens \((?P<text>\d+) of text input"
)
_GATEWAY_RATE_RE = re.compile(r"rate limit|429|RATE.LIMIT", re.IGNORECASE)


def parse_error_spend(days_back: int = 14) -> dict:
    """Per-day attribution of EVERY errored embedded run from the gateway journal.

    The journal logs `embedded run agent end isError=true model=X provider=Y
    error=<msg> rawError=<code>` for every failed API call. These calls send
    input tokens to OpenRouter; whether or not OpenRouter actually bills depends
    on the upstream provider's response, but conservatively we treat every
    failed call as a real cost driver and surface it.

    For each error we estimate input tokens from two sources, in order:
      1. context-overflow diag — when error contains "requested about N tokens",
         that N is the exact prompt size the gateway shipped.
      2. The successful sibling (same runId, isError=false) when it exists —
         the failing attempts in the same turn were carrying similar context.
      3. A model-specific fallback estimate (active-memory ~1500 tokens, regular
         session ~25_000 tokens — based on what we observed today).

    Output shape per day:
      {
        "<date>": {
          "totalCost": float,
          "calls": int,
          "byCategory": {
            "active-memory": {"calls": N, "models": {m: count}, "cost": $},
            "session":       {"calls": N, "models": {m: count}, "cost": $},
            "announce":      {"calls": N, "models": {m: count}, "cost": $},
          },
          "byErrorKind": {"500": N, "429": M, "context-overflow": K, "other": …},
          "topRuns": [{"runId", "calls", "model", "estCost", "errorSample"}],
        }
      }
    """
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "openclaw-gateway",
             "--since", f"{days_back} days ago", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}

    # First pass: gather per-runId estimated input tokens from any
    # successful sibling AND from context-overflow diagnostics.
    sibling_input: dict[str, int] = {}
    runid_overflow: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if "embedded run agent end" in line:
            em = _GATEWAY_END_RE.search(line)
            if em and em.group("isError") == "false":
                # We don't see token counts in the journal, but a non-error
                # end at least confirms which model carried the turn — used
                # by the cost estimator as a model hint, not a token count.
                sibling_input.setdefault(em.group("runId"), 0)
        if "requested about" in line:
            ov = _GATEWAY_CTXOVF_RE.search(line)
            if ov:
                # Extract the runId from the same line if present
                em = _GATEWAY_END_RE.search(line)
                rid = em.group("runId") if em else None
                if rid:
                    runid_overflow[rid] = max(runid_overflow.get(rid, 0),
                                              int(ov.group("requested")))

    # Model-specific default input estimate when nothing better is available.
    # active-memory queries are short (one user message + tail of conversation),
    # whereas session retries carry the full agent context.
    DEFAULT_INPUT = {"active-memory": 2000, "announce": 1000, "session": 25000}
    DEFAULT_OUTPUT = {"active-memory": 0, "announce": 100, "session": 100}

    days: dict[str, dict] = defaultdict(lambda: {
        "totalCost": 0.0,
        "calls": 0,
        "byCategory": {
            "active-memory": {"calls": 0, "models": defaultdict(int), "cost": 0.0},
            "session":       {"calls": 0, "models": defaultdict(int), "cost": 0.0},
            "announce":      {"calls": 0, "models": defaultdict(int), "cost": 0.0},
        },
        "byErrorKind": defaultdict(int),
        "runs": defaultdict(lambda: {"calls": 0, "model": "", "estCost": 0.0,
                                     "errorSample": "", "category": ""}),
    })

    # Per OpenRouter's billing rules, requests rejected BEFORE reaching the
    # upstream provider are not charged. We only count error kinds that
    # historically result in real charges; the rest get counted (visibility)
    # but assigned $0 estimated cost.
    BILLABLE_ERROR_KINDS = {"500", "timeout"}  # upstream gave us a stream that died
    for line in r.stdout.splitlines():
        if "embedded run agent end" not in line:
            continue
        em = _GATEWAY_END_RE.search(line)
        if not em or em.group("isError") != "true":
            continue
        ts_m = _GATEWAY_TS_RE.search(line)
        if not ts_m:
            continue
        date = ts_m.group(1)
        run_id = em.group("runId")
        model = em.group("model")
        # Categorise by runId prefix
        if run_id.startswith("active-memory"):
            cat = "active-memory"
        elif run_id.startswith("announce"):
            cat = "announce"
        else:
            cat = "session"
        # Estimate this call's input tokens
        if run_id in runid_overflow:
            est_in = runid_overflow[run_id]
        else:
            est_in = DEFAULT_INPUT.get(cat, 5000)
        est_out = DEFAULT_OUTPUT.get(cat, 100)
        # Classify error (the result determines whether we estimate any cost)
        if "context length" in line or "Context overflow" in line:
            err_kind = "context-overflow"
        elif "500 Internal Server Error" in line or "HTTP 500" in line:
            err_kind = "500"
        elif _GATEWAY_RATE_RE.search(line):
            err_kind = "429"
        elif "timeout" in line:
            err_kind = "timeout"
        else:
            err_kind = "other"
        # Only attribute a cost when the error kind is one OpenRouter actually
        # bills for. Other kinds still show in the count breakdown so the user
        # sees them.
        call_cost = (cost_for(model, {"input": est_in, "output": est_out, "cacheRead": 0})
                     if err_kind in BILLABLE_ERROR_KINDS else 0.0)
        # Pull a short error sample for the top-runs view
        err_sample = ""
        idx = line.find("error=")
        if idx >= 0:
            err_sample = line[idx + len("error="):idx + len("error=") + 160]
        day = days[date]
        day["totalCost"] += call_cost
        day["calls"] += 1
        bcat = day["byCategory"][cat]
        bcat["calls"] += 1
        bcat["models"][model] += 1
        bcat["cost"] += call_cost
        day["byErrorKind"][err_kind] += 1
        run = day["runs"][run_id]
        run["calls"] += 1
        run["model"] = model
        run["estCost"] += call_cost
        run["category"] = cat
        if not run["errorSample"]:
            run["errorSample"] = err_sample

    out: dict[str, dict] = {}
    for date, day in days.items():
        # Pick top-5 most expensive runs to surface in the UI
        top_runs = sorted(day["runs"].items(), key=lambda kv: -kv[1]["estCost"])[:5]
        out[date] = {
            "totalCost": round(day["totalCost"], 6),
            "calls": day["calls"],
            "byCategory": {
                cat: {
                    "calls": b["calls"],
                    "models": dict(b["models"]),
                    "cost": round(b["cost"], 6),
                } for cat, b in day["byCategory"].items()
            },
            "byErrorKind": dict(day["byErrorKind"]),
            "topRuns": [
                {
                    "runId": rid,
                    "calls": rv["calls"],
                    "model": rv["model"],
                    "modelDisplay": MODEL_DISPLAY.get(rv["model"], rv["model"]),
                    "category": rv["category"],
                    "estCost": round(rv["estCost"], 6),
                    "errorSample": rv["errorSample"],
                } for rid, rv in top_runs
            ],
        }
    return out


OPENROUTER_ACTIVITY_CACHE = Path.home() / ".cache" / "token-monitor" / "openrouter-activity.json"


def load_openrouter_activity() -> dict:
    """Read the per-day per-model billed totals that api-server.py refreshes
    from OpenRouter's /api/v1/activity. Shape:
      {fetchedAt: int, days: {date: {model: {requests, promptTokens,
       completionTokens, reasoningTokens, cost, providers}}}}
    """
    try:
        with open(OPENROUTER_ACTIVITY_CACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"days": {}, "fetchedAt": 0}


def reconcile_with_openrouter(records: list[dict], activity: dict,
                              error_spend: dict) -> dict:
    """Per-day reconciliation: OpenRouter says X, our local trajectories cover
    Y, the gateway journal explains Z worth of errored/non-trajectory calls,
    and the residual is the still-unexplained gap (could be plugin sub-agent
    successes that don't write transcripts, or upstream billing oddities).

    Output:
      {
        "<date>": {
          "openrouterCost": float,
          "openrouterRequests": int,
          "trackedCost": float,           # from session trajectories
          "errorCost": float,             # estimated from error journal
          "untrackedCost": float,         # OR - tracked - errors
          "byModel": [
            {model, modelDisplay, openrouterCost, trackedCost, gap,
             openrouterRequests, openrouterPromptTokens, openrouterCompletionTokens}
          ],
          "perModelCoverage": float,      # tracked / openrouter (cost ratio)
        }
      }
    """
    # Local cost per (date, model) from trajectory records
    local: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        date = r["date"]
        for mb in r.get("modelBreakdown") or []:
            local[date][mb["model"]] += mb["cost"]
        # Fallback: if a record has no breakdown (older format), use the flat cost
        if not r.get("modelBreakdown"):
            local[date][r["model"]] += r["cost"]

    out: dict[str, dict] = {}
    per_model_days = (activity or {}).get("perModel") or (activity or {}).get("days") or {}
    day_totals = (activity or {}).get("dayTotals") or {}
    all_dates = set(per_model_days.keys()) | set(day_totals.keys()) | set(local.keys())
    for date in sorted(all_dates):
        or_models = per_model_days.get(date) or {}
        # Prefer per-model sum (most authoritative) but fall back to /auth/key
        # day total when per-model isn't available (inference key, 403 on /activity).
        if or_models:
            or_total = sum(s["cost"] for s in or_models.values())
            or_total_source = "openrouter.activity"
        else:
            or_total = float((day_totals.get(date) or {}).get("totalCost", 0.0))
            or_total_source = (day_totals.get(date) or {}).get("source", "openrouter.authKey")
        or_requests = sum(int(s.get("requests", 0) or 0) for s in or_models.values())
        tracked = sum(local.get(date, {}).values())
        errs = (error_spend or {}).get(date, {}).get("totalCost", 0.0)
        per_model = []
        all_models = set(or_models.keys()) | set(local.get(date, {}).keys())
        for model in all_models:
            o = or_models.get(model) or {}
            or_cost = o.get("cost", 0.0)
            tr_cost = local.get(date, {}).get(model, 0.0)
            per_model.append({
                "model": model,
                "modelDisplay": MODEL_DISPLAY.get(model, model),
                "openrouterCost": round(or_cost, 6),
                "trackedCost": round(tr_cost, 6),
                "gap": round(or_cost - tr_cost, 6),
                "openrouterRequests": int(o.get("requests", 0) or 0),
                "openrouterPromptTokens": int(o.get("promptTokens", 0) or 0),
                "openrouterCompletionTokens": int(o.get("completionTokens", 0) or 0),
                "openrouterReasoningTokens": int(o.get("reasoningTokens", 0) or 0),
                "providers": o.get("providers") or {},
                # When OR side is missing (inference key can't see per-model
                # truth), `gap` becomes meaningless — flag the row.
                "openrouterUnknown": not bool(or_models),
            })
        # Largest absolute gap first — that's what the user wants to see
        per_model.sort(key=lambda x: -abs(x["gap"]) if not x["openrouterUnknown"] else -x["trackedCost"])
        coverage = (tracked / or_total) if or_total > 0 else None
        out[date] = {
            "openrouterCost": round(or_total, 6),
            "openrouterRequests": or_requests,
            "openrouterCostSource": or_total_source,
            "openrouterPerModelKnown": bool(or_models),
            "trackedCost": round(tracked, 6),
            "errorCost": round(errs, 6),
            "untrackedCost": round(max(0.0, or_total - tracked - errs), 6),
            "byModel": per_model,
            "perModelCoverage": round(coverage, 4) if coverage is not None else None,
        }
    return out


def parse_gateway_journal(days_back: int = 14) -> dict:
    """Scan `openclaw-gateway`'s systemd journal for embedded LLM calls — the
    invisible cost drivers that don't write trajectory files (active-memory
    plugin hook, fallback retries, etc.). Returns a per-date breakdown the UI
    can surface so the user sees where each cent really goes.

    Output shape:
      {
        "2026-05-15": {
          "activeMemory": {"calls": 27, "errors": 5, "models": {...}, "estCost": 0.18},
          "session":       {"calls": 12, "errors": 0, "models": {...}, "estCost": 0.04},
          "announce":      {"calls": 1,  "errors": 0, "models": {...}, "estCost": 0.001},
          "fallbacks":     34,
        },
        ...
      }
    """
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "openclaw-gateway",
             "--since", f"{days_back} days ago", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}

    days: dict[str, dict] = defaultdict(lambda: {
        "activeMemory": {"calls": 0, "errors": 0, "models": defaultdict(int)},
        "session":      {"calls": 0, "errors": 0, "models": defaultdict(int)},
        "announce":     {"calls": 0, "errors": 0, "models": defaultdict(int)},
        "fallbacks":    0,
    })

    for line in r.stdout.splitlines():
        m = _GATEWAY_TS_RE.search(line)
        if not m:
            continue
        date = m.group(1)
        if "model fallback decision" in line:
            days[date]["fallbacks"] += 1
            continue
        if "embedded run agent end" not in line:
            continue
        em = _GATEWAY_RUN_RE.search(line)
        if not em:
            continue
        run_id = em.group("runId")
        is_error = em.group("isError") == "true"
        model = em.group("model")
        if run_id.startswith("active-memory"):
            bucket = days[date]["activeMemory"]
        elif run_id.startswith("announce"):
            bucket = days[date]["announce"]
        else:
            bucket = days[date]["session"]
        bucket["calls"] += 1
        if is_error:
            bucket["errors"] += 1
        bucket["models"][model] += 1

    out: dict[str, dict] = {}
    for date, day in days.items():
        entry: dict = {"fallbacks": day["fallbacks"]}
        for src_key, profile_key in (("activeMemory", "active-memory"),
                                      ("session", "session-embed"),
                                      ("announce", "announce")):
            b = day[src_key]
            models = dict(b["models"])
            profile = GATEWAY_CALL_PROFILE.get(profile_key, {"tokensIn": 1000, "tokensOut": 200})
            est = 0.0
            for model, n in models.items():
                u = {"input": profile["tokensIn"] * n, "output": profile["tokensOut"] * n, "cacheRead": 0}
                est += cost_for(model, u)
            entry[src_key] = {
                "calls": b["calls"],
                "errors": b["errors"],
                "models": models,
                "estCost": round(est, 4),
            }
        out[date] = entry
    return out


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

    # Keep ALL sessions in the record set — even cron-rotator / heartbeat /
    # daily-notes that we used to drop. They still cost real money on
    # OpenRouter, so dropping them made the local total diverge from the
    # provider total by ~$6/day. Visibility in the UI is controlled by the
    # `visible` and `noiseKind` flags emitted per record below.
    subs = list(raw)

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

    # Reverse map: child sessionKey → parent sessionKey, built from spawnLinks
    # captured on each parent's trajectory. Used to attribute subagents to a
    # specific main session in the flat records list.
    child_to_parent_key: dict[str, str] = {}
    spawn_link_by_child: dict[str, dict] = {}
    raw_by_key: dict[str, dict] = {}
    for r_s in raw:
        rk = r_s.get("sessionKey") or ""
        if rk:
            raw_by_key[rk] = r_s
        for link in (r_s.get("spawnLinks") or []):
            ck = link.get("childSessionKey") or ""
            if ck and ck not in child_to_parent_key:
                child_to_parent_key[ck] = rk
                spawn_link_by_child[ck] = link

    def _session_kind(sk: str) -> str:
        if ":subagent:" in sk:
            return "subagent"
        if ":cron:" in sk:
            return "cron"
        return "main"

    def _extract_owner(sk: str) -> dict:
        """Pull owner info from sessionKey suffix. Formats observed in the wild:
        - agent:<a>:telegram:direct:<chatId>     → telegram DM
        - agent:<a>:telegram:group:<chatId>      → telegram group
        - agent:<a>:direct:<chatId>              → telegram DM (newer, no "telegram:" prefix)
        - agent:<a>:cli:<...>                    → CLI invocation
        - agent:<a>:main[:...]                   → local interactive shell on the host
        - agent:<a>:dashboard:<uuid>             → web dashboard / mini-app trigger
        - agent:<a>:subagent:<uuid>              → spawned by another session
        - agent:<a>:cron:<id>[:run:<uuid>]       → scheduled job
        - <uuid>                                 → legacy key, no attribution info
        """
        parts = sk.split(":")
        if len(parts) >= 5 and parts[2] == "telegram":
            chat_id = parts[4]
            return {
                "ownerKey": chat_id,
                "ownerKind": "telegram",
                "ownerLabel": f"📨 {chat_id}",
                "ownerChannel": parts[3],
            }
        # Newer Telegram-direct format: agent:<a>:direct:<numericChatId>
        if len(parts) >= 4 and parts[2] == "direct" and parts[3].lstrip("-").isdigit():
            chat_id = parts[3]
            return {
                "ownerKey": chat_id,
                "ownerKind": "telegram",
                "ownerLabel": f"📨 {chat_id}",
                "ownerChannel": "direct",
            }
        if len(parts) >= 3 and parts[2] == "cli":
            return {"ownerKey": "cli", "ownerKind": "cli", "ownerLabel": "💻 CLI"}
        if len(parts) >= 3 and parts[2] == "cron":
            return {"ownerKey": "cron", "ownerKind": "cron", "ownerLabel": "⏰ cron"}
        if len(parts) >= 3 and parts[2] == "subagent":
            return {"ownerKey": "subagent", "ownerKind": "subagent", "ownerLabel": "↳ subagent"}
        # Local interactive shell on the host. The "main" channel suffix means
        # whoever sits at the host's terminal — for a single-user box this is
        # always the admin/operator.
        if len(parts) >= 3 and parts[2] == "main":
            return {"ownerKey": "local", "ownerKind": "local", "ownerLabel": "💻 Locale"}
        if len(parts) >= 3 and parts[2] == "dashboard":
            return {"ownerKey": "dashboard", "ownerKind": "dashboard", "ownerLabel": "🖥 Dashboard"}
        # Legacy: bare UUID with no agent: prefix, or any other shape we don't
        # recognise. Distinguish from a true unknown by labelling as "legacy"
        # so users see it's a pre-attribution session, not a bug.
        if sk and ":" not in sk:
            return {"ownerKey": "legacy", "ownerKind": "legacy", "ownerLabel": "🗂 legacy"}
        return {"ownerKey": "?", "ownerKind": "unknown", "ownerLabel": "❓ sconosciuto"}

    def _agent_from_session_key(sk: str) -> str | None:
        # sessionKey like agent:<owner_agent>:<scope>:<id> — parts[1] is the
        # agent that OWNS this session, not the spawner. For subagents we look
        # up the actual parent via the spawn-link map below.
        parts = sk.split(":")
        if len(parts) >= 2 and parts[0] == "agent":
            return parts[1]
        return None

    def _parent_for_subagent(sk: str) -> tuple[str | None, str | None]:
        """Return (parent_session_key, parent_agent) by following the spawn map."""
        parent_key = child_to_parent_key.get(sk)
        if not parent_key:
            return None, None
        return parent_key, _agent_from_session_key(parent_key)

    # Build session records — cost summed across per-model chunks
    records = []
    for s in subs:
        mins = duration_minutes(s["started"], s["ended"])
        u = s.get("usage") or {}
        tin = int(u.get("input", 0) or 0)
        tout = int(u.get("output", 0) or 0)
        tcache = int(u.get("cacheRead", 0) or 0)
        # Cost per model (handles fallback chain) — keep the per-model breakdown
        # on the record so the UI can show the *real* model that billed tokens
        # instead of pinning a session to the primary model from session.started.
        breakdown, fb_cost, fb_used = model_breakdown(
            s.get("perModelUsage") or {}, s["model"]
        )
        c = sum(e["cost"] for e in breakdown)
        # Date attribution: use ENDED date so a session straddling midnight
        # lands on the day where the bulk of the work actually happened
        # (matches user perception of "what I spent today").
        sk = s.get("sessionKey") or ""
        kind = _session_kind(sk)
        if kind == "subagent":
            parent_key, parent_agent = _parent_for_subagent(sk)
        else:
            parent_key, parent_agent = None, None
        noise_kind = _classify_noise(s)
        visible = is_substantial(s)
        records.append({
            "id": os.path.basename(s["path"]).split(".")[0],
            "agent": s["agent"],
            "model": s["model"],
            "modelDisplay": MODEL_DISPLAY.get(s["model"], s["model"]),
            "modelBreakdown": breakdown,
            "fallbackUsed": fb_used,
            "fallbackCost": fb_cost,
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
            "sessionKey": sk,
            "kind": kind,
            "visible": visible,
            "noiseKind": noise_kind,
            "parentAgent": parent_agent,
            "parentSessionKey": parent_key,
            "owner": _extract_owner(sk),
        })

    # Sort newest first by actual completion time (endedAt), not by start time.
    # A session 23:50→00:42 has start-time 23:50 but ended this morning;
    # sorting by start would put it ahead of today's 14:00 sessions.
    records.sort(key=lambda r: r.get("endedAt", r["date"] + "T" + r["time"]), reverse=True)

    # Daily aggregation (from real records, not estimates)
    daily = defaultdict(lambda: {"sessions": 0, "tokensIn": 0, "tokensOut": 0,
                                 "cacheRead": 0, "cost": 0.0, "fallbackCost": 0.0})
    for r in records:
        d = daily[r["date"]]
        d["sessions"] += 1
        d["tokensIn"] += r["tokensIn"]
        d["tokensOut"] += r["tokensOut"]
        d["cacheRead"] += r["cacheRead"]
        d["cost"] += r["cost"]
        d["fallbackCost"] += r.get("fallbackCost", 0) or 0
    daily_list = [{"date": k, **{kk: (round(vv, 4) if isinstance(vv, float) else vv)
                                  for kk, vv in v.items()}}
                  for k, v in sorted(daily.items())]

    # Fallback summary: per-day breakdown of spend on models OTHER than the
    # session's primary. This surfaces "invisible" costs like today's GLM 4.7
    # bill from DeepSeek failing over — the session is labelled DeepSeek but
    # the dollars went to GLM.
    fallback_summary: dict[str, dict] = {}
    for r in records:
        if not r.get("fallbackUsed"):
            continue
        date = r["date"]
        entry = fallback_summary.setdefault(date, {
            "cost": 0.0,
            "sessions": 0,
            "byModel": {},
            "fromPrimary": {},
        })
        entry["cost"] += r.get("fallbackCost", 0) or 0
        entry["sessions"] += 1
        primary = r.get("model") or "?"
        entry["fromPrimary"][primary] = entry["fromPrimary"].get(primary, 0) + 1
        for mb in r.get("modelBreakdown") or []:
            if mb.get("isPrimary"):
                continue
            mid = mb["model"]
            slot = entry["byModel"].setdefault(mid, {
                "modelDisplay": mb["modelDisplay"],
                "cost": 0.0,
                "tokensIn": 0,
                "tokensOut": 0,
                "cacheRead": 0,
                "sessions": 0,
            })
            slot["cost"] += mb["cost"]
            slot["tokensIn"] += mb["tokensIn"]
            slot["tokensOut"] += mb["tokensOut"]
            slot["cacheRead"] += mb["cacheRead"]
            slot["sessions"] += 1
    for date, e in fallback_summary.items():
        e["cost"] = round(e["cost"], 6)
        for slot in e["byModel"].values():
            slot["cost"] = round(slot["cost"], 6)

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

    # Live sessions: every *main* session whose last event landed within
    # LIVE_CUTOFF_MINUTES. We do NOT deduplicate by agent — two users chatting
    # to the same agent (e.g. `main` via two different Telegram chats) must
    # appear as two separate live cards, each with its own owner chip.
    # 4h window: long enough to span a single working session with pauses,
    # short enough to exclude yesterday's traffic.
    # A session is "live" only if it had a turn in the last LIVE_CUTOFF_MINUTES.
    # 4 hours was way too generous — most sessions that paused that long are
    # dormant, not running. Tightening to 10 min matches user perception of
    # "is this conversation currently active in front of someone".
    LIVE_CUTOFF_MINUTES = 10
    cutoff_iso = (datetime.now(timezone.utc).timestamp() - LIVE_CUTOFF_MINUTES * 60)

    def _ts_to_epoch(iso: str) -> float:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    live_mains: list[dict] = []
    for s in subs:
        if _session_kind(s.get("sessionKey") or "") != "main":
            continue
        # Exclude noise: argo heartbeats, session-rotator pings etc. all use
        # sessionKey shapes that classify as "main" but are not real
        # conversations. is_substantial() catches them via their prompt/text.
        if not is_substantial(s):
            continue
        if _ts_to_epoch(s.get("ended") or "") < cutoff_iso:
            continue
        live_mains.append(s)
    # Most recently active first
    live_mains.sort(key=lambda s: s.get("ended") or "", reverse=True)

    current_by_agent = []
    for s in live_mains:
        agent = s["agent"]
        mins = duration_minutes(s["started"], s["ended"])
        # Per-model breakdown for the live card too — same shape as flat records
        live_breakdown, live_fb_cost, live_fb_used = model_breakdown(
            s.get("perModelUsage") or {}, s["model"]
        )
        c = sum(e["cost"] for e in live_breakdown)
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

        owner = _extract_owner(s.get("sessionKey") or "")
        current_by_agent.append({
            "agent": agent,
            "sessionId": os.path.basename(s["path"]).split(".")[0],
            "sessionKey": s.get("sessionKey") or "",
            "owner": owner,
            "model": s["model"],
            "modelDisplay": MODEL_DISPLAY.get(s["model"], s["model"]),
            "modelBreakdown": live_breakdown,
            "fallbackUsed": live_fb_used,
            "fallbackCost": live_fb_cost,
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

    # Sort: most recently active first (by last event timestamp).
    current_by_agent.sort(key=lambda x: x.get("ended") or "", reverse=True)

    # Attach subagent cards to every MAIN session record (not just live ones)
    # so the Sessions tab can nest children under their parent. Reuses the same
    # spawn-link resolution path as currentByAgent.
    live_session_ids = {c["sessionId"] for c in current_by_agent if c.get("sessionId")}
    for r in records:
        r["isLive"] = r["id"] in live_session_ids
        if r.get("kind") != "main":
            r["subagents"] = []
            continue
        parent_raw = raw_by_key.get(r.get("sessionKey") or "")
        if not parent_raw:
            r["subagents"] = []
            continue
        sub_cards: list[dict] = []
        seen: set[str] = set()
        for link in (parent_raw.get("spawnLinks") or []):
            ck = link.get("childSessionKey") or ""
            if not ck or ck in seen:
                continue
            seen.add(ck)
            child = sessions_by_key.get(ck)
            if child:
                sub_cards.append(_subagent_card(child, link))
            else:
                parts = ck.split(":")
                sub_cards.append({
                    "agent": parts[1] if len(parts) > 1 else "?",
                    "sessionId": "",
                    "sessionKey": ck,
                    "childIdShort": parts[-1][:8] if parts else "",
                    "model": link.get("modelRequested") or "",
                    "modelDisplay": MODEL_DISPLAY.get(link.get("modelRequested") or "",
                                                      link.get("modelRequested") or ""),
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
        r["subagents"] = sub_cards
        r["subagentsCost"] = round(sum(sc.get("cost", 0) or 0 for sc in sub_cards), 6)
        r["subagentsCount"] = len(sub_cards)

    # Surface diagnostic info so the UI can warn when local totals drift from
    # OpenRouter. `missingModels` lists ids that hit FALLBACK_PRICE during
    # cost computation; `noiseSummary` quantifies hidden (system) spend.
    noise_summary = {}
    for r in records:
        nk = r.get("noiseKind") or ""
        if not nk:
            continue
        b = noise_summary.setdefault(nk, {"sessions": 0, "cost": 0.0})
        b["sessions"] += 1
        b["cost"] += r["cost"]
    for v in noise_summary.values():
        v["cost"] = round(v["cost"], 4)

    gateway_activity = parse_gateway_journal(days_back=14)
    error_spend = parse_error_spend(days_back=14)
    or_activity = load_openrouter_activity()
    reconciliation = reconcile_with_openrouter(records, or_activity, error_spend)

    return {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessionsCount": len(records),
        "sessionsVisibleCount": sum(1 for r in records if r.get("visible")),
        "totalCost": round(sum(r["cost"] for r in records), 4),
        "totalCostVisible": round(sum(r["cost"] for r in records if r.get("visible")), 4),
        "totalTokensIn": sum(r["tokensIn"] for r in records),
        "totalTokensOut": sum(r["tokensOut"] for r in records),
        "totalCacheRead": sum(r["cacheRead"] for r in records),
        "sessions": records,
        "daily": daily_list,
        "agentBreakdown": agent_breakdown,
        "bloated": bloated,
        "currentByAgent": current_by_agent,
        "pricing": MODEL_PRICING,
        "missingModels": sorted(_MISSING_MODELS),
        "noiseSummary": noise_summary,
        "gatewayActivity": gateway_activity,
        "fallbackSummary": fallback_summary,
        "errorSpend": error_spend,
        "openrouterActivity": or_activity.get("days", {}),
        "openrouterFetchedAt": or_activity.get("fetchedAt", 0),
        "reconciliation": reconciliation,
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

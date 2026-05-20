#!/usr/bin/env python3
"""Live status of OpenClaw subagent runs.

Reads `~/.openclaw/subagents/runs.json` for the run registry, then tails
each active run's `agents/<id>/sessions/<sid>.trajectory.jsonl` and
session.jsonl to derive a human-readable status:

    🧠 thinking (12s)
    🔧 tool:web_search (3s)
    ♻️ fallback openrouter/google/gemma-4-31b-it (timeout primary)
    🐢 thinking (78s) — slow
    🚨 stuck (190s) — likely timeout
    ✅ done
    ❌ error: <reason>

Usage:
    python3 live_agents.py              # one-shot snapshot, text
    python3 live_agents.py --json       # machine-readable
    python3 live_agents.py --watch 2    # refresh every 2s (CLI tail)
    python3 live_agents.py --include-ended 5   # also show 5 most recent ended

Designed to be import-safe so api-server.py can call
`live_agents.snapshot()` from a `/api/live-agents` route.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

OPENCLAW = Path.home() / ".openclaw"
RUNS_JSON = OPENCLAW / "subagents" / "runs.json"
AGENTS_DIR = OPENCLAW / "agents"

# Thresholds (seconds) for status escalation
THINKING_SLOW = 30
THINKING_STUCK = 120
TOOL_SLOW = 20
TOOL_STUCK = 90


@dataclass
class AgentStatus:
    run_id: str
    agent_id: str
    agent_name: str
    session_id: str
    model: str
    started_at: float            # epoch seconds
    last_event_at: float         # epoch seconds — most recent activity
    age_seconds: float           # now - last_event_at
    state: str                   # thinking | tool | fallback | idle | done | error | starting
    detail: str                  # human label, e.g. "tool:web_search" or "thinking"
    icon: str                    # emoji icon for the state
    task_excerpt: str            # first line of the task (≤80 chars)
    ended: bool
    elapsed_seconds: float = 0.0           # now - startedAt (run-level elapsed)
    tool_call_count: int = 0               # how many tool calls have happened so far
    recent_events: list[dict] | None = None  # only when detail=True
    last_assistant_text: str = ""          # last assistant text (truncated) — only when detail=True
    last_tool_name: str = ""               # name of last tool invocation — only when detail=True
    last_tool_result: str = ""             # last tool result text (truncated) — only when detail=True
    current_tool: str = ""                 # pending tool the assistant is currently waiting on
    full_task: str = ""                    # complete task — only when detail=True


def _read_jsonl(path: Path, tail: int = 200) -> list[dict[str, Any]]:
    """Return up to `tail` last events from a JSONL file. Tolerant of partial writes."""
    if not path.exists():
        return []
    # Cheap tail: read whole file if small, otherwise seek
    size = path.stat().st_size
    if size < 256_000:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        with path.open("rb") as fh:
            fh.seek(max(0, size - 256_000))
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()[1:]  # drop possibly-truncated first line
    out: list[dict[str, Any]] = []
    for ln in lines[-tail:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _agent_display_name(agent_id: str) -> str:
    return {"main": "Alessia", "leo": "Leo", "vera": "Vera",
            "sofia": "Sofia", "argo": "Argo"}.get(agent_id, agent_id.capitalize())


def _last_assistant_tool_name(session_file: Path) -> str | None:
    """If the last assistant message has pending tool_calls, return the first tool name."""
    events = _read_jsonl(session_file, tail=30)
    for ev in reversed(events):
        if ev.get("type") != "message":
            continue
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        tcs = msg.get("tool_calls") or []
        if not tcs:
            return None
        fn = (tcs[0].get("function") or {}).get("name")
        return fn or None
    return None


def _investigate_session(session_file: Path) -> dict:
    """Scan the session jsonl and return debugging metadata.

    Returns:
      tool_call_count: total assistant->tool invocations seen
      last_assistant_text: last assistant text content (truncated)
      last_tool_name: name of the most recent tool call (any status)
      last_tool_result: stringified result of the most recent tool (truncated)
      current_tool: name of the most recent assistant tool_call WITHOUT a
                    matching tool result yet (= the one currently running).
    """
    events = _read_jsonl(session_file, tail=400)
    out = {
        "tool_call_count": 0,
        "last_assistant_text": "",
        "last_tool_name": "",
        "last_tool_result": "",
        "current_tool": "",
    }
    pending: dict[str, str] = {}   # tool_call_id -> tool name (no result yet)
    for ev in events:
        if ev.get("type") != "message":
            continue
        msg = ev.get("message") or {}
        role = msg.get("role")
        if role == "assistant":
            txt = msg.get("content") or ""
            if isinstance(txt, list):
                # OpenAI content-parts shape
                txt = " ".join(p.get("text", "") for p in txt if isinstance(p, dict))
            if txt:
                out["last_assistant_text"] = str(txt)[:1200]
            for tc in msg.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name", "")
                tcid = tc.get("id") or ""
                if fn:
                    out["tool_call_count"] += 1
                    out["last_tool_name"] = fn
                    if tcid:
                        pending[tcid] = fn
        elif role == "tool":
            tcid = msg.get("tool_call_id") or ""
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content:
                out["last_tool_result"] = str(content)[:1500]
            pending.pop(tcid, None)
    if pending:
        # Most recently issued tool with no matching result yet
        out["current_tool"] = list(pending.values())[-1]
    return out


def _recent_trajectory_summary(events: list[dict], n: int = 12) -> list[dict]:
    """Compact view of the last N trajectory events for debugging."""
    out = []
    now = time.time()
    for ev in events[-n:]:
        ts = _parse_ts(ev.get("ts"))
        out.append({
            "type": ev.get("type", ""),
            "ts": ev.get("ts", ""),
            "age_seconds": max(0.0, now - ts) if ts else None,
            "seq": ev.get("seq"),
        })
    return out


def _classify(traj_events: list[dict[str, Any]],
              session_file: Path,
              now: float,
              run_ended: bool) -> tuple[str, str, str, float]:
    """Return (state, detail, icon, last_event_epoch)."""
    if run_ended:
        # Outcome decided at the runs.json layer; here just say ended.
        if traj_events:
            last = traj_events[-1]
            ts = _parse_ts(last.get("ts"))
            return "done", "ended", "✅", ts
        return "done", "ended", "✅", now

    if not traj_events:
        return "starting", "spawning", "⏳", now

    last = traj_events[-1]
    last_ts = _parse_ts(last.get("ts")) or now
    age = now - last_ts
    typ = last.get("type", "")

    if typ == "session.started":
        return "starting", "initializing", "⏳", last_ts

    if typ == "model.fallback_step":
        # primary timed out; trying fallback model
        data = last.get("data", "")
        return "fallback", f"fallback model (age {int(age)}s)", "♻️", last_ts

    if typ == "prompt.submitted":
        # Waiting for the model to reply
        if age > THINKING_STUCK:
            return "stuck", f"thinking {int(age)}s — likely stuck", "🚨", last_ts
        if age > THINKING_SLOW:
            return "thinking_slow", f"thinking {int(age)}s — slow", "🐢", last_ts
        return "thinking", f"thinking {int(age)}s", "🧠", last_ts

    if typ == "model.completed":
        # Model just returned. Did it ask for a tool?
        tool = _last_assistant_tool_name(session_file)
        if tool:
            if age > TOOL_STUCK:
                return "stuck", f"tool:{tool} {int(age)}s — likely stuck", "🚨", last_ts
            if age > TOOL_SLOW:
                return "tool_slow", f"tool:{tool} {int(age)}s — slow", "🐢", last_ts
            return "tool", f"tool:{tool} {int(age)}s", "🔧", last_ts
        # No pending tool — either run is about to end or it's between turns
        return "idle", f"idle {int(age)}s", "💤", last_ts

    if typ == "session.ended":
        return "done", "ended", "✅", last_ts

    return "unknown", typ, "❓", last_ts


def _trajectory_path(agent_id: str, session_id: str) -> Path:
    return AGENTS_DIR / agent_id / "sessions" / f"{session_id}.trajectory.jsonl"


def _session_path(agent_id: str, session_id: str) -> Path:
    return AGENTS_DIR / agent_id / "sessions" / f"{session_id}.jsonl"


def _agent_id_from_session_key(key: str) -> str | None:
    # "agent:<id>:subagent:<uuid>" or "agent:<id>:direct:<chat>"
    parts = key.split(":")
    if len(parts) >= 2 and parts[0] == "agent":
        return parts[1]
    return None


def _session_id_from_session_key(key: str) -> str | None:
    parts = key.split(":")
    if len(parts) >= 4:
        return parts[3]
    return None


def snapshot(include_ended: int = 0, detail: bool = False,
             only_run_id: str | None = None) -> list[AgentStatus]:
    """Build a snapshot of active subagent runs (and optionally the last `include_ended` ended ones).

    Returns newest-first. With `detail=True`, each entry also carries recent
    events, last assistant text, and last tool result — for the "investigate
    stuck subagent" debugging view.
    """
    now = time.time()
    if not RUNS_JSON.exists():
        return []
    try:
        raw = json.loads(RUNS_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    runs = raw.get("runs", {})

    active: list[AgentStatus] = []
    ended: list[AgentStatus] = []

    for run_id, run in runs.items():
        if only_run_id and run_id != only_run_id:
            continue
        child_key = run.get("childSessionKey", "")
        agent_id = _agent_id_from_session_key(child_key) or "?"
        session_id = _session_id_from_session_key(child_key) or "?"
        traj = _trajectory_path(agent_id, session_id)
        sess = _session_path(agent_id, session_id)
        events = _read_jsonl(traj, tail=50)
        is_ended = bool(run.get("endedAt"))
        state, detail_label, icon, last_ev_ts = _classify(events, sess, now, is_ended)
        if is_ended:
            outcome = run.get("outcome") or {}
            if outcome.get("status") == "error":
                state, icon = "error", "❌"
                detail_label = f"error: {(outcome.get('error') or '')[:80]}"

        # Persona prepend ends at a "## TASK …" header; the real task follows.
        full_task = run.get("task") or ""
        marker_idx = -1
        for m in ("## TASK SPECIFICO", "## TASK", "## Task"):
            i = full_task.find(m)
            if i >= 0:
                marker_idx = i + len(m)
                break
        tail = full_task[marker_idx:] if marker_idx >= 0 else full_task
        task_excerpt = ""
        for line in tail.splitlines():
            ln = line.strip().lstrip("#> -*").strip()
            if not ln:
                continue
            task_excerpt = ln[:80]
            break
        if not task_excerpt and full_task:
            task_excerpt = full_task.strip().splitlines()[0][:80]

        started_epoch = (run.get("startedAt") or 0) / 1000
        # If ended, elapsed = endedAt - startedAt; otherwise = now - startedAt
        if is_ended and run.get("endedAt"):
            elapsed = (run["endedAt"] / 1000) - started_epoch
        else:
            elapsed = now - started_epoch if started_epoch else 0.0

        invest = _investigate_session(sess) if detail else {
            "tool_call_count": 0, "last_assistant_text": "",
            "last_tool_name": "", "last_tool_result": "", "current_tool": "",
        }
        # Always compute cheap tool count for the list view
        if not detail:
            cheap = _investigate_session(sess) if events else {}
            invest["tool_call_count"] = cheap.get("tool_call_count", 0)
            invest["current_tool"] = cheap.get("current_tool", "")

        status = AgentStatus(
            run_id=run_id,
            agent_id=agent_id,
            agent_name=_agent_display_name(agent_id),
            session_id=session_id,
            model=run.get("model", ""),
            started_at=started_epoch,
            last_event_at=last_ev_ts,
            age_seconds=max(0.0, now - last_ev_ts),
            state=state,
            detail=detail_label,
            icon=icon,
            task_excerpt=task_excerpt,
            ended=is_ended,
            elapsed_seconds=max(0.0, elapsed),
            tool_call_count=invest["tool_call_count"],
            current_tool=invest.get("current_tool", ""),
            recent_events=_recent_trajectory_summary(events) if detail else None,
            last_assistant_text=invest["last_assistant_text"] if detail else "",
            last_tool_name=invest["last_tool_name"] if detail else "",
            last_tool_result=invest["last_tool_result"] if detail else "",
            full_task=full_task if detail else "",
        )
        (ended if is_ended else active).append(status)

    active.sort(key=lambda s: s.started_at, reverse=True)
    ended.sort(key=lambda s: s.started_at, reverse=True)
    return active + ended[:include_ended]


def render_text(snap: Iterable[AgentStatus]) -> str:
    snap = list(snap)
    if not snap:
        return "(no subagent runs)"
    lines = []
    for s in snap:
        age = f"{int(s.age_seconds)}s ago" if s.age_seconds < 600 else f"{int(s.age_seconds/60)}m ago"
        lines.append(
            f"{s.icon} {s.agent_name:<8} {s.state:<13} {s.detail:<32} "
            f"[{s.model.split('/')[-1][:18]:<18}] {s.task_excerpt}"
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--watch", type=float, default=0.0, help="refresh every N seconds")
    p.add_argument("--include-ended", type=int, default=0, help="also show N most recent ended runs")
    args = p.parse_args()

    def once():
        snap = snapshot(include_ended=args.include_ended)
        if args.json:
            print(json.dumps([asdict(s) for s in snap], indent=2))
        else:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"== OpenClaw subagents — {ts} UTC ==")
            print(render_text(snap))

    if args.watch <= 0:
        once()
        return 0
    try:
        while True:
            os.system("clear")
            once()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

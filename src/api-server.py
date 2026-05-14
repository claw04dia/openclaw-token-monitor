#!/usr/bin/env python3
"""Token Monitor — authenticated mini-app backend.

Serves the same data exposed by the CLI skill, but gated by Telegram WebApp
`initData` HMAC validation. Replaces the unauthenticated data-server.py.

Env vars:
  TELEGRAM_BOT_TOKEN    bot token used to validate initData signatures
  TELEGRAM_ALLOWED_USER comma-separated Telegram user IDs allowed to fetch
  PORT                  listen port (default 8899)
  BIND                  listen address (default 127.0.0.1)
  AUTH_MAX_AGE_SECONDS  max age of initData auth_date (default 86400 = 24h)
  ALLOW_ORIGIN          CORS allow-origin header value (default *)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USER", "529895213").split(",") if x.strip()
}
PORT = int(os.environ.get("PORT", "8899"))
BIND = os.environ.get("BIND", "127.0.0.1")
MAX_AGE = int(os.environ.get("AUTH_MAX_AGE_SECONDS", "86400"))
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")

HISTORY_PATH = "/tmp/claw04-telegram-miniapp/history.json"
TOKEN_USAGE_PATH = os.path.expanduser("~/.openclaw/workspace/memory/token-usage.json")
AUTH_PROFILES_PATH = os.path.expanduser("~/.openclaw/agents/main/agent/auth-profiles.json")
CRON_JOBS_PATH = os.path.expanduser("~/.openclaw/cron/jobs.json")
SESSIONS_CACHE = Path.home() / ".cache" / "token-monitor" / "sessions.json"
BUILD_SESSIONS_SCRIPT = Path(__file__).resolve().parent.parent / "build-sessions.py"
SESSIONS_TTL = 300         # forced rebuild every 5 min even if nothing changed
SESSIONS_COOLDOWN = 15     # min seconds between rebuilds (rate-limit)
TRAJECTORIES_GLOB = "agents/*/sessions/*.trajectory.jsonl"
STATIC_DIR = Path(__file__).resolve().parent
STATIC_FILES = {"/", "/index.html", "/manifest.json"}
STATIC_DIRS = ("/css/", "/js/")

_OR_CACHE = {"data": None, "ts": 0}
_OR_TTL = 60  # seconds


def _read_openrouter_key() -> str:
    try:
        with open(AUTH_PROFILES_PATH) as f:
            profiles = json.load(f).get("profiles", {})
        return profiles.get("openrouter:default", {}).get("key", "")
    except Exception as e:
        log.warning("openrouter key unreadable: %s", e)
        return ""


def fetch_openrouter_spend() -> dict:
    """Returns {today, week, month, keyLifetime, accountLifetime, credits, asOf}.

    Cached for _OR_TTL seconds. `keyLifetime` is the spend for this specific
    API key; `accountLifetime` is the spend across the whole OpenRouter account.
    """
    if _OR_CACHE["data"] and (time.time() - _OR_CACHE["ts"]) < _OR_TTL:
        return _OR_CACHE["data"]
    key = _read_openrouter_key()
    if not key:
        return {"error": "no_key"}
    out = {"asOf": int(time.time())}
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            ak = json.loads(r.read()).get("data", {})
        out["today"]       = round(float(ak.get("usage_daily", 0) or 0), 4)
        out["week"]        = round(float(ak.get("usage_weekly", 0) or 0), 4)
        out["month"]       = round(float(ak.get("usage_monthly", 0) or 0), 4)
        out["keyLifetime"] = round(float(ak.get("usage", 0) or 0), 4)
        out["limit"]       = ak.get("limit")
    except Exception as e:
        log.warning("openrouter /auth/key failed: %s", e)
        out["error"] = str(e)
        return out
    # Account-wide lifetime + remaining credits
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            cr = json.loads(r.read()).get("data", {})
        out["accountLifetime"] = round(float(cr.get("total_usage", 0) or 0), 4)
        out["credits"]         = round(float(cr.get("total_credits", 0) or 0), 4)
        out["remaining"]       = round(out["credits"] - out["accountLifetime"], 4)
    except Exception as e:
        log.warning("openrouter /credits failed: %s", e)
    _OR_CACHE["data"] = out
    _OR_CACHE["ts"] = time.time()
    return out

MODEL_MAP = {
    "deepseek/deepseek-v4-flash": "DeepSeek V4 Flash",
    "deepseek/deepseek-v4-pro":   "DeepSeek V4 Pro",
    "moonshotai/kimi-k2.6":       "Kimi K2.6",
    "qwen/qwen3-coder-plus":      "Qwen3 Coder+",
    "google/gemma-4-31b-it":      "Gemma 4 31B",
    "xiaomi/mimo-v2-pro":         "Mimo V2 Pro",
}

log = logging.getLogger("tmapi")


# ─────────────────────────── Auth ───────────────────────────

def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str, bot_token: str, max_age: int = MAX_AGE) -> dict | None:
    """Returns parsed user dict if signature/age OK, else None.

    Per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    flat = {k: v[0] for k, v in parsed.items()}
    received_hash = flat.pop("hash", None)
    if not received_hash:
        return None
    auth_date = int(flat.get("auth_date", "0"))
    if auth_date == 0 or (time.time() - auth_date) > max_age:
        return None
    data_check = "\n".join(f"{k}={flat[k]}" for k in sorted(flat))
    expected = hmac.new(_secret_key(bot_token), data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    user_raw = flat.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None


# ─────────────────────────── Data assembly ───────────────────────────

def _newest_trajectory_mtime() -> float:
    """Return mtime of the most recently modified trajectory file (0 if none)."""
    root = Path.home() / ".openclaw"
    newest = 0.0
    try:
        for p in root.glob(TRAJECTORIES_GLOB):
            try:
                m = p.stat().st_mtime
                if m > newest:
                    newest = m
            except OSError:
                continue
    except Exception as e:
        log.warning("trajectory scan failed: %s", e)
    return newest


def _maybe_rebuild_sessions():
    """Re-run build-sessions.py if a trajectory is newer than the cache, or
    the cache is older than SESSIONS_TTL. Rate-limited by SESSIONS_COOLDOWN."""
    now = time.time()
    if not SESSIONS_CACHE.exists():
        needs_rebuild = True
    else:
        cache_mtime = SESSIONS_CACHE.stat().st_mtime
        age = now - cache_mtime
        if age < SESSIONS_COOLDOWN:
            return  # too soon since last rebuild
        traj_mtime = _newest_trajectory_mtime()
        needs_rebuild = age > SESSIONS_TTL or traj_mtime > cache_mtime
    if not needs_rebuild:
        return
    try:
        import subprocess
        subprocess.run(["python3", str(BUILD_SESSIONS_SCRIPT)],
                       check=True, capture_output=True, timeout=30)
    except Exception as e:
        log.warning("build-sessions.py failed: %s", e)


def build_payload() -> dict:
    _maybe_rebuild_sessions()

    data = {"updatedAt": None, "prices": [], "sessions": [],
            "agentBreakdown": [], "bloated": [], "daily": [], "stats": {}}

    # Sessions from real trajectories (primary data source)
    if SESSIONS_CACHE.exists():
        with open(SESSIONS_CACHE) as f:
            sess = json.load(f)
        data["updatedAt"] = sess.get("updatedAt")
        data["sessions"] = sess.get("sessions", [])
        data["agentBreakdown"] = sess.get("agentBreakdown", [])
        data["bloated"] = sess.get("bloated", [])
        data["daily"] = sess.get("daily", [])
        data["currentByAgent"] = sess.get("currentByAgent", [])
        data["stats"] = {
            "totalSessions": sess.get("sessionsCount", 0),
            "totalTokensIn": sess.get("totalTokensIn", 0),
            "totalTokensOut": sess.get("totalTokensOut", 0),
            "totalCacheRead": sess.get("totalCacheRead", 0),
            "totalCostFromSessionsUsd": sess.get("totalCost", 0),
        }
        pricing = sess.get("pricing", {})
        for mid, p in pricing.items():
            data["prices"].append({
                "model": MODEL_MAP.get(mid, mid), "id": mid,
                "inputPrice": p["in"], "outputPrice": p["out"],
                "cachePrice": p.get("cache", 0),
            })

    if os.path.exists(TOKEN_USAGE_PATH):
        with open(TOKEN_USAGE_PATH) as f:
            usage = json.load(f)
        data["stats"]["dailyBudgetUsd"] = usage.get("dailyBudgetUsd", 1.0)

    # Real spend from OpenRouter (cached 60s)
    real = fetch_openrouter_spend()
    data["realSpend"] = real
    if "error" not in real:
        data["stats"]["todayCostUsd"] = real["today"]
        data["stats"]["realWeekCostUsd"] = real["week"]
        data["stats"]["realMonthCostUsd"] = real["month"]
        data["stats"]["realLifetimeCostUsd"] = real.get("accountLifetime", real.get("keyLifetime", 0))
        if "remaining" in real:
            data["stats"]["remainingCreditsUsd"] = real["remaining"]

    return data


_CRON_SHORTCUTS = {
    "@reboot":   None,
    "@yearly":   "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly":  "0 0 1 * *",
    "@weekly":   "0 0 * * 0",
    "@daily":    "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly":   "0 * * * *",
}


def _parse_crontab_line(line: str) -> dict | None:
    """Parse a single crontab entry. Returns None for blanks/comments/env vars."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    # env var: NAME=value (no schedule field starting at column 0)
    if "=" in s.split()[0] and not s[0] in "@*0123456789":
        return None
    parts = s.split(None, 5)
    if not parts:
        return None
    if parts[0].startswith("@"):
        if len(parts) < 2:
            return None
        return {
            "expr": parts[0],
            "exprStandard": _CRON_SHORTCUTS.get(parts[0]),
            "command": " ".join(parts[1:]),
        }
    if len(parts) < 6:
        return None
    expr = " ".join(parts[:5])
    return {"expr": expr, "exprStandard": expr, "command": parts[5]}


def build_cron_payload() -> dict:
    """Read ~/.openclaw/cron/jobs.json and return a slim, UI-friendly shape.

    The raw payload.message can be huge (kilobytes of Italian briefing prompts);
    we forward it untouched so the mini-app can show it on demand, but include
    a short preview so the list renders fast.
    """
    out: dict = {"jobs": [], "updatedAt": None, "path": CRON_JOBS_PATH}
    try:
        st = os.stat(CRON_JOBS_PATH)
        out["updatedAt"] = int(st.st_mtime)
        with open(CRON_JOBS_PATH) as f:
            raw = json.load(f)
    except FileNotFoundError:
        out["error"] = "cron jobs.json non trovato"
        return out
    except Exception as e:
        out["error"] = f"jobs.json: {e}"
        return out

    for j in raw.get("jobs", []) or []:
        sched = j.get("schedule") or {}
        payload = j.get("payload") or {}
        delivery = j.get("delivery") or {}
        msg = payload.get("message") or ""
        preview = msg.strip().split("\n", 1)[0][:160]
        out["jobs"].append({
            "id": j.get("id"),
            "name": j.get("name"),
            "description": j.get("description") or "",
            "enabled": bool(j.get("enabled", True)),
            "createdAtMs": j.get("createdAtMs"),
            "schedule": {
                "kind": sched.get("kind"),
                "expr": sched.get("expr"),
                "tz": sched.get("tz") or "UTC",
            },
            "sessionTarget": j.get("sessionTarget"),
            "wakeMode": j.get("wakeMode"),
            "payload": {
                "kind": payload.get("kind"),
                "thinking": payload.get("thinking"),
                "model": payload.get("model"),
                "toolsAllow": payload.get("toolsAllow") or [],
                "messagePreview": preview,
                "messageLength": len(msg),
                "message": msg,
            },
            "delivery": {
                "mode": delivery.get("mode"),
                "channel": delivery.get("channel"),
                "to": delivery.get("to"),
            },
        })
    return out


def build_system_cron_payload() -> dict:
    """Return parsed entries from the user's crontab (`crontab -l`).

    OS-level drop-ins (/etc/cron.d, /etc/cron.daily/...) are intentionally
    excluded — they're apt/sysstat/logrotate boilerplate, not user-relevant.
    """
    import subprocess
    out: dict = {"entries": [], "source": "crontab -l"}
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        out["error"] = "crontab non disponibile"
        return out
    except Exception as e:
        out["error"] = f"crontab error: {e}"
        return out
    if r.returncode != 0:
        # No crontab installed is a normal state — exit code 1 with "no crontab for X"
        msg = (r.stderr or "").strip().lower()
        if "no crontab" in msg:
            return out
        out["error"] = f"crontab rc={r.returncode}: {r.stderr.strip()}"
        return out
    for idx, line in enumerate(r.stdout.splitlines(), start=1):
        parsed = _parse_crontab_line(line)
        if not parsed:
            continue
        parsed["line"] = idx
        parsed["raw"] = line
        out["entries"].append(parsed)
    return out


# ─────────────────────────── HTTP handler ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "TokenMonitorAPI/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "3600")

    def _json(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _auth(self) -> dict | None:
        header = self.headers.get("Authorization", "")
        init_data = ""
        if header.startswith("tma "):
            init_data = header[4:]
        elif header.startswith("Bearer "):
            init_data = header[7:]
        if not init_data:
            # also accept ?initData= query for debugging
            q = urllib.parse.urlparse(self.path).query
            init_data = urllib.parse.parse_qs(q).get("initData", [""])[0]
        user = validate_init_data(init_data, BOT_TOKEN)
        if user is None:
            log.info("auth failed from %s path=%s", self.client_address[0], self.path)
            return None
        if ALLOWED_USERS and user.get("id") not in ALLOWED_USERS:
            log.info("user %s not in allowlist", user.get("id"))
            return None
        return user

    def log_message(self, fmt, *args):
        log.info("%s - " + fmt, self.client_address[0], *args)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _serve_static(self, rel: str):
        if rel in ("", "/"):
            rel = "index.html"
        else:
            rel = rel.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        # Block path traversal
        if not str(target).startswith(str(STATIC_DIR)):
            self._json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._json(404, {"error": "not_found"})
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "ts": int(time.time())})
            return
        if path == "/api/data":
            user = self._auth()
            if not user:
                self._json(401, {"error": "unauthorized"})
                return
            try:
                payload = build_payload()
            except Exception as e:
                log.exception("payload build failed")
                self._json(500, {"error": "internal", "detail": str(e)})
                return
            payload["user"] = {"id": user.get("id"), "first_name": user.get("first_name")}
            self._json(200, payload)
            return
        if path == "/api/cron":
            user = self._auth()
            if not user:
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._json(200, build_cron_payload())
            except Exception as e:
                log.exception("cron payload build failed")
                self._json(500, {"error": "internal", "detail": str(e)})
            return
        if path == "/api/system-cron":
            user = self._auth()
            if not user:
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._json(200, build_system_cron_payload())
            except Exception as e:
                log.exception("system cron payload build failed")
                self._json(500, {"error": "internal", "detail": str(e)})
            return
        # Static frontend
        if path in STATIC_FILES or path.startswith(STATIC_DIRS):
            self._serve_static(path)
            return
        self._json(404, {"error": "not_found"})


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var required")
    if not ALLOWED_USERS:
        raise SystemExit("TELEGRAM_ALLOWED_USER env var required")
    log.info("listening on %s:%d (allowed users=%s)", BIND, PORT, sorted(ALLOWED_USERS))
    HTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

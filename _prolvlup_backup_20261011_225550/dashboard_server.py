# -*- coding: utf-8 -*-
"""
FreeFire Level Up Bot - ARAFAT CODEX Dashboard
Embedded Async Web Server (aiohttp)

Everything on this server is behind a login:
  * "/" serves templates/login.html when there is no session, and
    templates/index.html once authenticated.
  * every /api/* route requires a valid session (401 otherwise).
  * /api/admin/* additionally requires the built-in admin account.

Account ownership: every entry written to accounts.json carries an "owner"
field with the username that added it. Users only see and control their own
accounts and are limited to the quota the admin gave them; the admin sees
everything.
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from aiohttp import web, ClientSession, ClientTimeout

import auth_system

try:
    import daily_exp_tracker
except Exception:
    daily_exp_tracker = None

# ==================== ACCOUNT CAP ====================
MAX_ACCOUNTS = int(os.environ.get("MAX_ACCOUNTS", "10000"))

# ==================== MATCH MODE (BR / LW / AUTO) ====================
# Defined here rather than in main.py: this module is imported BY main.py, so
# it can never import main.py back. The web layer needs to validate a mode
# before the bot half of the program is even loaded.
#
# BR = Battle Royale, LW = Lone Wolf, AUTO = level-based switch: BR while the
# account is still below MODE_SWITCH_LEVEL, then Lone Wolf from that level on.
MODE_BR = "BR"
MODE_LW = "LW"
MODE_AUTO = "AUTO"
VALID_MODES = (MODE_BR, MODE_LW, MODE_AUTO)
DEFAULT_MODE = MODE_AUTO

# AUTO switch point, decided by the account's CURRENT LEVEL (not match count):
#   level <  MODE_SWITCH_LEVEL -> BR
#   level >= MODE_SWITCH_LEVEL -> Lone Wolf
# e.g. MODE_SWITCH_LEVEL = 3 -> fresh account farms BR until it hits Level 3,
# the next StartMatch after that is already Lone Wolf. Override per deployment
# with the MODE_SWITCH_LEVEL environment variable.
MODE_SWITCH_LEVEL = max(1, int(os.environ.get("MODE_SWITCH_LEVEL", "3") or 3))

_MODE_ALIASES = {
    "BATTLE": MODE_BR, "BATTLE_ROYALE": MODE_BR, "BATTLE ROYALE": MODE_BR,
    "BATTLE-ROYALE": MODE_BR, "BR-1": MODE_BR, "1": MODE_BR,
    "LONEWOLF": MODE_LW, "LONE_WOLF": MODE_LW, "LONE WOLF": MODE_LW,
    "LONE-WOLF": MODE_LW, "WOLF": MODE_LW, "SOLO": MODE_LW, "43": MODE_LW,
    "AUTO_SWITCH": MODE_AUTO, "SWITCH": MODE_AUTO,
}


def normalise_mode(mode) -> str:
    """Turn anything the UI or accounts.json can hold into a valid preference.

    Always returns one of VALID_MODES, defaulting to DEFAULT_MODE, so callers
    never have to defend against junk coming from the request body.
    """
    m = str(mode or "").strip().upper()
    if m in VALID_MODES:
        return m
    return _MODE_ALIASES.get(m, DEFAULT_MODE)


# ==================== SPEED PROFILE (SAFE / RISK) ====================
# Every timing knob the matchmaking loop uses, bundled per profile so an
# account can be run conservatively or aggressively without touching code.
#   SAFE = shipped defaults: a slow, human-looking request pace.
#   RISK = shorter queue interval + much shorter offline gap: more matches per
#          hour, more exposure to the anti-abuse system.
# The profile is picked per account when it is added and stored on its
# accounts.json entry, so it survives restarts.
SPEED_SAFE = "SAFE"
SPEED_RISK = "RISK"
VALID_SPEED_MODES = (SPEED_SAFE, SPEED_RISK)
DEFAULT_SPEED_MODE = SPEED_SAFE

_PRIORITY_REGIONS = ["BD", "IND", "SG", "TH", "PH", "VN", "MY", "ID", "HK", "TW"]

SPEED_PROFILES: Dict[str, Dict[str, Any]] = {
    SPEED_SAFE: {
        "label": "Safe Mode",
        "NO_MATCH_QUARANTINE_CYCLES": 3,
        "START_MATCH_INTERVAL_MAX": 90.0,
        "START_MATCH_INTERVAL": 30.0,
        "NEW_MATCH_DELAY": 50.0,
        "MAX_MATCH_DURATION": 700,
        "MATCH_IDLE_TIMEOUT": 8.0,
        "NO_MATCH_RESTART_TIMEOUT": 300,
        "PRIORITY_REGIONS": list(_PRIORITY_REGIONS),
        "MAX_CONSECUTIVE_PARSE_FAILURES": 5.0,
        "NON_MATCH_RECONNECT_DELAY": 1.0,
    },
    SPEED_RISK: {
        "label": "Risk Mode",
        "NO_MATCH_QUARANTINE_CYCLES": 3,
        "START_MATCH_INTERVAL_MAX": 45.0,
        "START_MATCH_INTERVAL": 8.0,
        "NEW_MATCH_DELAY": 3.0,
        "MAX_MATCH_DURATION": 700,
        "MATCH_IDLE_TIMEOUT": 8.0,
        "NO_MATCH_RESTART_TIMEOUT": 300,
        "PRIORITY_REGIONS": list(_PRIORITY_REGIONS),
        "MAX_CONSECUTIVE_PARSE_FAILURES": 5.0,
        "NON_MATCH_RECONNECT_DELAY": 1.0,
    },
}

_SPEED_ALIASES = {
    "SAFE MODE": SPEED_SAFE, "SAFE_MODE": SPEED_SAFE, "SLOW": SPEED_SAFE,
    "CONSERVATIVE": SPEED_SAFE, "1": SPEED_SAFE,
    "RISK MODE": SPEED_RISK, "RISK_MODE": SPEED_RISK, "RISKY": SPEED_RISK,
    "FAST": SPEED_RISK, "AGGRESSIVE": SPEED_RISK, "2": SPEED_RISK,
}


def normalise_speed_mode(mode) -> str:
    """Always returns a valid profile name, defaulting to SAFE."""
    m = str(mode or "").strip().upper()
    if m in VALID_SPEED_MODES:
        return m
    return _SPEED_ALIASES.get(m, DEFAULT_SPEED_MODE)


def speed_profile(mode) -> Dict[str, Any]:
    """The timing knobs for a profile — unknown names fall back to SAFE."""
    return SPEED_PROFILES[normalise_speed_mode(mode)]

# ==================== BANGLADESH TIMEZONE (UTC+6) ====================
BD_TZ = timezone(timedelta(hours=6))


def bd_now_str() -> str:
    return datetime.now(BD_TZ).strftime("%H:%M:%S")


class BotState:
    def __init__(self):
        self.accounts: Dict[str, Dict[str, Any]] = {}
        self.logs: List[Dict[str, Any]] = []
        self.max_logs = 200
        self.total_matches = 0
        self.total_gained_exp = 0
        self.start_time = time.time()
        self.account_workers: Dict[str, asyncio.Task] = {}
        self.refresh_callbacks: Dict[str, Any] = {}
        self.account_credentials: Dict[str, Dict[str, Any]] = {}
        # uids the game server stopped matching, loaded from accounts.json by
        # main.py at startup. Checked in register_account so a throttled
        # account comes back up already marked instead of hammering the gateway
        # for another few cycles after every restart.
        self.persisted_throttle: set = set()
        self._target_triggered: set = set()

    def log(self, message: str, level: str = "info", uid: Optional[str] = None):
        entry = {
            "time": bd_now_str(),
            "level": level,
            "message": message,
            "uid": uid,
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def register_account(self, uid, nickname, region, level, exp, likes=0,
                         target_level=0, target_matches=0, match_mode=None,
                         speed_mode=None):
        uid_str = str(uid)

        if uid_str not in self.accounts and len(self.accounts) >= MAX_ACCOUNTS:
            self.log(
                f"REJECTED {uid_str} — cap reached ({len(self.accounts)}/{MAX_ACCOUNTS})",
                "warning", uid_str
            )
            return False

        if uid_str not in self.accounts:
            was_throttled = uid_str in self.persisted_throttle
            self.accounts[uid_str] = {
                "uid": uid_str,
                "nickname": nickname or f"Player_{uid_str[:6]}",
                "region": region or "BD",
                "level": level or 1,
                "initial_exp": exp,
                "current_exp": exp,
                "gained_exp": 0,
                "likes": likes or 0,
                "status": "THROTTLED" if was_throttled else "ONLINE",
                "matches_played": 0,
                "active_matches": 0,
                "last_match_time": None,
                "last_updated": bd_now_str(),
                "target_level": int(target_level) if target_level else 0,
                "target_matches": int(target_matches) if target_matches else 0,
                "match_mode": (match_mode or "AUTO"),
                "speed_mode": normalise_speed_mode(speed_mode),
                "throttled": was_throttled,
                "stop_reason": "THROTTLED" if was_throttled else "",
            }
        else:
            acc = self.accounts[uid_str]
            if nickname:
                acc["nickname"] = nickname
            if region:
                acc["region"] = region
            if level:
                acc["level"] = level
            if target_level:
                acc["target_level"] = int(target_level)
            if target_matches:
                acc["target_matches"] = int(target_matches)
            if match_mode:
                acc["match_mode"] = match_mode
            if speed_mode:
                acc["speed_mode"] = normalise_speed_mode(speed_mode)
            acc["current_exp"] = exp
            acc["gained_exp"] = max(0, exp - acc["initial_exp"])
            acc["likes"] = likes
            acc["status"] = "ONLINE"
            acc["last_updated"] = bd_now_str()

        self.recalc_totals()
        self._check_target(uid_str)
        return True

    def update_exp(self, uid: str, current_exp: int, level: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            acc = self.accounts[uid_str]
            old_exp = acc["current_exp"]
            acc["current_exp"] = current_exp
            if level is not None and level > 0:
                acc["level"] = level
            acc["gained_exp"] = max(0, current_exp - acc["initial_exp"])
            acc["last_updated"] = bd_now_str()
            diff = current_exp - old_exp
            if diff > 0:
                self.log(
                    f"Account {acc['nickname']} ({uid_str}) gained +{diff} EXP! "
                    f"Total: +{acc['gained_exp']}",
                    "success", uid_str
                )
            self.recalc_totals()
            self._check_target(uid_str)

    def _check_target(self, uid_str: str):
        """Log only — the functional loop does the actual stop+finish+pause."""
        acc = self.accounts.get(uid_str)
        if not acc:
            return

        target_lvl = int(acc.get("target_level", 0) or 0)
        target_mt = int(acc.get("target_matches", 0) or 0)

        if target_lvl > 0 and int(acc.get("level", 1)) >= target_lvl:
            key = f"lv_{uid_str}"
            if key not in self._target_triggered:
                self._target_triggered.add(key)
                self.log(
                    f"🎯 {acc.get('nickname')} ({uid_str}) hit Target Level {target_lvl} "
                    f"— finishing running matches, then pausing",
                    "success", uid_str
                )

        if target_mt > 0 and int(acc.get("matches_played", 0)) >= target_mt:
            key = f"mt_{uid_str}"
            if key not in self._target_triggered:
                self._target_triggered.add(key)
                self.log(
                    f"🎯 {acc.get('nickname')} ({uid_str}) hit Target {target_mt} matches "
                    f"— finishing running matches, then pausing",
                    "success", uid_str
                )

    def update_status(self, uid: str, status: str, active_matches: Optional[int] = None):
        uid_str = str(uid)
        if uid_str in self.accounts:
            self.accounts[uid_str]["status"] = status
            if active_matches is not None:
                self.accounts[uid_str]["active_matches"] = active_matches
            self.accounts[uid_str]["last_updated"] = bd_now_str()

    def increment_match(self, uid: str):
        uid_str = str(uid)
        self.total_matches += 1
        if uid_str in self.accounts:
            self.accounts[uid_str]["matches_played"] += 1
            self.accounts[uid_str]["last_match_time"] = bd_now_str()
            self.accounts[uid_str]["last_updated"] = bd_now_str()
            self.log(
                f"Account {self.accounts[uid_str]['nickname']} finished Match "
                f"#{self.accounts[uid_str]['matches_played']}",
                "info", uid_str
            )
            self._check_target(uid_str)

    def recalc_totals(self):
        self.total_gained_exp = sum(acc.get("gained_exp", 0) for acc in self.accounts.values())


bot_state = BotState()

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
TEMPLATE_PATH = os.path.join(TEMPLATE_DIR, "index.html")
LOGIN_TEMPLATE_PATH = os.path.join(TEMPLATE_DIR, "login.html")
# Static assets (css/js) live next to the templates and are served pre-auth
# so the login page itself can pull its stylesheet.
STATIC_DIR = os.path.join(TEMPLATE_DIR, "static")

SESSION_COOKIE = "ff_session"
# Signed login-page token (bot friction): set when the login form is served,
# required by handle_login. HttpOnly so only the form's fetch carries it.
LOGIN_TOKEN_COOKIE = "ff_lt"


# ============================================================
# accounts.json helpers
# ============================================================
# Absolute path: the dashboard must find the same file no matter which
# directory the process was started from (a CWD-relative path silently
# created/reads an empty file elsewhere and every login loses its usage
# count after a restart).
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")


def _read_accounts_file() -> List[Dict[str, Any]]:
    """accounts.json must be a list of objects. A bare object or a string
    here would make `acc.get(...)` blow up, so drop anything malformed.

    A torn write (crash mid-save) leaves the primary unreadable; the .bak
    mirror written by _write_accounts_file() still holds the last good copy,
    so rescue it instead of reporting zero accounts."""
    loaded = None
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        try:
            with open(ACCOUNTS_FILE + ".bak", "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception:
            return []
    if isinstance(loaded, list):
        return [a for a in loaded if isinstance(a, dict)]
    return []


def _write_accounts_file(entries: List[Dict[str, Any]]) -> None:
    """Atomic save: write a temp file first so a crash can never truncate or
    corrupt accounts.json, then refresh the .bak mirror."""
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, ACCOUNTS_FILE)
    try:
        bak_tmp = ACCOUNTS_FILE + ".bak.tmp"
        with open(bak_tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        os.replace(bak_tmp, ACCOUNTS_FILE + ".bak")
    except Exception:
        pass


# ============================================================
# ownership  (who added which account)
# ============================================================
# Guest accounts are keyed by uid straight away. Token accounts only learn
# their real uid after the first successful login, so we also index them by
# the token the user pasted and bridge the two once the worker is up.
_uid_owner: Dict[str, str] = {}
_token_owner: Dict[str, str] = {}


def load_owners() -> None:
    """Rebuild the owner maps from accounts.json at server start."""
    for entry in _read_accounts_file():
        owner = str(entry.get("owner") or "").strip()
        if not owner:
            continue
        if entry.get("uid"):
            _uid_owner[str(entry["uid"])] = owner
        if entry.get("token"):
            _token_owner[str(entry["token"])] = owner


def _auth_token_for(uid: str) -> str:
    creds = bot_state.account_credentials.get(uid)
    if isinstance(creds, dict) and creds.get("auth_token"):
        return str(creds["auth_token"])
    for _k, v in bot_state.account_credentials.items():
        if not isinstance(v, dict):
            continue
        if (str(v.get("account_id", "")) == uid or
                str(v.get("auth_uid") or "") == uid):
            return str(v.get("auth_token") or "")
    return ""


def owner_of(uid) -> Optional[str]:
    uid = str(uid)
    owner = _uid_owner.get(uid)
    if owner:
        return owner
    # Bridge account_id <-> the login uid the user actually typed, so an owner
    # keeps seeing his account even when the game reports a different id.
    for _k, v in bot_state.account_credentials.items():
        if not isinstance(v, dict):
            continue
        if str(v.get("account_id") or "") == uid and v.get("auth_uid"):
            owner = _uid_owner.get(str(v["auth_uid"]))
            if owner:
                _uid_owner[uid] = owner
                return owner
    tok = _auth_token_for(uid)
    if tok and tok in _token_owner:
        _uid_owner[uid] = _token_owner[tok]
        return _uid_owner[uid]
    return None


def count_owned(username: str) -> int:
    """How many accounts.json entries belong to `username` (quota basis)."""
    n = 0
    for entry in _read_accounts_file():
        if str(entry.get("owner") or "") == username:
            n += 1
    return n


def _token_label(token: str) -> str:
    """Stable display/key label for a token account that has no uid yet."""
    return "tok_" + str(token or "")[:10]


def _file_owner_of(uid: str) -> Optional[str]:
    """Owner recorded in accounts.json for a uid (or token-label) even when the
    account never reached bot_state. Lets an owner manage a saved account that
    is still signing in or failed to register."""
    uid = str(uid)
    for entry in _read_accounts_file():
        if entry.get("uid") and str(entry["uid"]) == uid:
            return str(entry.get("owner") or "") or None
        tok = str(entry.get("token") or "")
        if tok and _token_label(tok) == uid:
            return str(entry.get("owner") or "") or None
    return None


def _remove_account_entry(uid: str) -> Optional[str]:
    """Drop a saved account by uid or token-label. Returns the removed token
    (so owner maps can be cleaned up) or None when nothing matched."""
    uid = str(uid)
    entries = _read_accounts_file()
    keep: List[Dict[str, Any]] = []
    removed_token: Optional[str] = None
    for entry in entries:
        match = bool(entry.get("uid") and str(entry["uid"]) == uid)
        tok = str(entry.get("token") or "")
        if not match and tok and _token_label(tok) == uid:
            match = True
        if match:
            if tok:
                removed_token = tok
            continue
        keep.append(entry)
    if len(keep) != len(entries):
        _write_accounts_file(keep)
    return removed_token


def _quota_for(username: str) -> int:
    if auth_system.is_admin(username):
        return MAX_ACCOUNTS
    info = auth_system.get_user(username)
    return int((info or {}).get("max_accounts") or 0)


def _owner_check(request: web.Request, uid: str) -> Optional[web.Response]:
    """403 unless the caller is the admin or the owner of this uid."""
    user = request.get("user")
    if auth_system.is_admin(user):
        return None
    if owner_of(uid) == user:
        return None
    if _file_owner_of(uid) == user:
        return None
    return web.json_response(
        {"status": "error", "error": "Not your account"}, status=403
    )


def _me_payload(username: str) -> Dict[str, Any]:
    if auth_system.is_admin(username):
        return {
            "username": username,
            "role": "admin",
            "max_accounts": MAX_ACCOUNTS,
            "used": len(bot_state.accounts),
            "expires": None,
            "days_left": None,
            "enabled": True,
        }
    info = auth_system.get_user(username) or {}
    return {
        "username": username,
        "role": "user",
        "max_accounts": int(info.get("max_accounts") or 0),
        "used": count_owned(username),
        "expires": info.get("expires"),
        "days_left": info.get("days_left"),
        "enabled": bool(info.get("enabled", True)),
    }


# ============================================================
# login rate limit (brute-force guard, escalating lockout)
# ============================================================
# 10 failures inside 10 minutes lock the IP out for 10 minutes; every further
# offence doubles the wait (20m, 40m, ... capped at 2 hours). A successful
# sign-in clears the counter AND the escalation, so the owner can never
# lock himself out for long.
_LOGIN_FAILS: Dict[str, List[float]] = {}
_LOGIN_WINDOW = 600.0
_LOGIN_MAX = 10
_LOGIN_BLOCKED: Dict[str, float] = {}    # ip -> unlock timestamp
_LOGIN_STRIKES: Dict[str, int] = {}      # ip -> completed lockouts
_LOGIN_BLOCK_BASE = 600.0                # first lockout: 10 minutes
_LOGIN_BLOCK_CAP = 7200.0                # never longer than 2 hours


def _client_ip(request: web.Request) -> str:
    return request.remote or "local"


def _login_blocked(ip: str) -> Optional[float]:
    """None when the IP may try; otherwise seconds left of the lockout."""
    until = _LOGIN_BLOCKED.get(ip)
    if until is None or time.time() >= until:
        return None                       # expired — strikes stay for escalation
    return until - time.time()


def _login_fail(ip: str) -> None:
    now = time.time()
    hits = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW]
    hits.append(now)
    _LOGIN_FAILS[ip] = hits
    if len(hits) < _LOGIN_MAX:
        return
    strikes = _LOGIN_STRIKES.get(ip, 0)
    secs = min(_LOGIN_BLOCK_BASE * (2 ** strikes), _LOGIN_BLOCK_CAP)
    _LOGIN_BLOCKED[ip] = now + secs
    _LOGIN_STRIKES[ip] = strikes + 1
    _LOGIN_FAILS.pop(ip, None)
    mins = max(1, int(secs + 59) // 60)
    print(f"\033[91m[!] Login lockout for {ip}: {mins} min "
          f"(strike {strikes + 1})\033[0m")


def _login_ok(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)
    _LOGIN_BLOCKED.pop(ip, None)
    _LOGIN_STRIKES.pop(ip, None)


# ============================================================
# page rendering
# ============================================================
def _html(path: str, fallback: str) -> web.Response:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        content = fallback
    # Substitutions the raw file cannot know about, e.g. the AUTO switch point.
    content = content.replace("{{MODE_SWITCH_LEVEL}}", str(MODE_SWITCH_LEVEL))
    return web.Response(
        text=content, content_type="text/html", charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Session gate for the whole site."""
    token = request.cookies.get(SESSION_COOKIE)
    user = auth_system.session_user(token)
    request["user"] = user
    request["session_token"] = token
    path = request.path

    # ---- pre-auth routes -------------------------------------------------
    if path == "/api/auth/login" and request.method == "POST":
        return await handler(request)

    if path == "/favicon.ico":
        return web.Response(status=204)

    # Static assets (css/js) are public: the login page needs its stylesheet
    # before the user has a session.
    if path.startswith("/static/"):
        return await handler(request)

    # ---- pages -----------------------------------------------------------
    if path == "/":
        if user is None:
            resp = _html(LOGIN_TEMPLATE_PATH, "<h1>login template missing</h1>")
            # mint a fresh login token: handle_login rejects POSTs that do
            # not carry it (scripts never render this page)
            try:
                resp.set_cookie(
                    LOGIN_TOKEN_COOKIE, auth_system.issue_login_token(),
                    max_age=auth_system.LOGIN_TOKEN_TTL, path="/",
                    httponly=True, samesite="Lax",
                )
            except Exception:
                pass
            return resp
        return await handler(request)          # handle_index -> dashboard

    if user is None:
        if path.startswith("/api/"):
            return web.json_response(
                {"status": "error", "error": "Login required", "code": "unauthorized"},
                status=401,
            )
        return web.Response(status=404)

    # ---- admin-only API --------------------------------------------------
    if path.startswith("/api/admin/") and not auth_system.is_admin(user):
        return web.json_response(
            {"status": "error", "error": "Admin only", "code": "forbidden"},
            status=403,
        )

    return await handler(request)


# ============================================================
# auth routes
# ============================================================
async def handle_index(request: web.Request) -> web.Response:
    return _html(TEMPLATE_PATH, "<h1>templates/index.html not found!</h1>")


async def handle_login(request: web.Request) -> web.Response:
    ip = _client_ip(request)
    blocked_for = _login_blocked(ip)
    if blocked_for is not None:
        mins = max(1, int(blocked_for + 59) // 60)
        return web.json_response(
            {"status": "error",
             "error": f"Too many attempts — try again in {mins} minute(s)"},
            status=429,
        )
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"status": "error", "error": "Invalid request"}, status=400)

    # ---- bot protection ---------------------------------------------------
    # 1) honeypot: real people never see or fill this field
    if str(data.get("hp") or "").strip():
        _login_fail(ip)
        return web.json_response(
            {"status": "error", "error": "Wrong username or password"},
            status=401,
        )
    # 2) signed login-page token: only a POST from our own form carries it
    state = auth_system.login_token_state(request.cookies.get(LOGIN_TOKEN_COOKIE))
    if state == "expired":
        # normal case: the tab was open too long — ask the page to reload
        return web.json_response(
            {"status": "error",
             "error": "Session expired — refresh the page and try again",
             "code": "expired"},
            status=401,
        )
    if state != "ok":
        _login_fail(ip)
        return web.json_response(
            {"status": "error", "error": "Wrong username or password"},
            status=401,
        )

    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")

    role = auth_system.verify_login(username, password)
    if role is None:
        _login_fail(ip)
        return web.json_response(
            {"status": "error", "error": "Wrong username or password"},
            status=401,
        )

    _login_ok(ip)
    session = auth_system.create_session(username)
    resp = web.json_response({"status": "ok", "role": role, "username": username})
    resp.set_cookie(
        SESSION_COOKIE, session,
        max_age=auth_system.SESSION_TTL, path="/",
        httponly=True, samesite="Lax",
    )
    # rotate the login token with every successful sign-in
    try:
        resp.set_cookie(
            LOGIN_TOKEN_COOKIE, auth_system.issue_login_token(),
            max_age=auth_system.LOGIN_TOKEN_TTL, path="/",
            httponly=True, samesite="Lax",
        )
    except Exception:
        pass
    bot_state.log(f"🔓 {username} signed in to the dashboard", "info")
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    auth_system.destroy_session(request.get("session_token"))
    resp = web.json_response({"status": "ok"})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def handle_me(request: web.Request) -> web.Response:
    user = request.get("user")
    return web.json_response({"status": "ok", "me": _me_payload(user)})


# ============================================================
# stats
# ============================================================
async def handle_get_stats(request: web.Request) -> web.Response:
    user = request.get("user")
    admin = auth_system.is_admin(user)

    # ---- pagination / filter / search (query string) ----------------------
    # At 10k accounts the old "send everything" poll was unusable, so the
    # client now asks for a slice and the server filters + pages it.
    qs = request.query
    try:
        offset = max(0, int(qs.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(qs.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 2000))
    qtext = (qs.get("q") or "").strip().lower()
    ffilter = (qs.get("f") or "all").strip().lower()

    accounts_data = [
        a for a in bot_state.accounts.values()
        if admin or owner_of(a.get("uid")) == user
    ]

    # ---- guarantee every SAVED account is visible --------------------------
    # accounts.json is the source of truth for what the user added. An entry
    # that is not (yet) registered in bot_state — still signing in, or its
    # worker failed — is shown as a stopped card instead of silently vanishing.
    seen_keys = {str(a.get("uid")) for a in accounts_data if a.get("uid")}
    seen_tokens = set()
    for k, v in bot_state.account_credentials.items():
        seen_keys.add(str(k))
        if not isinstance(v, dict):
            continue
        for f in ("account_id", "auth_uid"):
            if v.get(f):
                seen_keys.add(str(v[f]))
        if v.get("auth_token"):
            seen_tokens.add(str(v["auth_token"]))

    for entry in _read_accounts_file():
        owner = str(entry.get("owner") or "") or auth_system.ADMIN_USER
        if not (admin or owner == user):
            continue
        uid = str(entry.get("uid") or "")
        token = str(entry.get("token") or "")
        if uid and uid in seen_keys:
            continue
        if token and token in seen_tokens:
            continue
        if not uid and not token:
            continue
        label = uid or _token_label(token)
        if label in seen_keys:
            continue
        seen_keys.add(label)
        accounts_data.append({
            "uid": label,
            "nickname": "Signing in…",
            "region": entry.get("region") or "—",
            "level": int(entry.get("level") or 0),
            "initial_exp": 0,
            "current_exp": 0,
            "gained_exp": 0,
            "likes": 0,
            "matches_played": 0,
            "active_matches": 0,
            "status": "STOPPED",
            "match_mode": normalise_mode(entry.get("match_mode", "")),
            "speed_mode": normalise_speed_mode(entry.get("speed_mode", "")),
            "target_level": int(entry.get("target_level", 0) or 0),
            "target_matches": int(entry.get("target_matches", 0) or 0),
            "owner": owner,
            "pending": True,
        })

    for a in accounts_data:
        a["owner"] = owner_of(a.get("uid")) or a.get("owner") or auth_system.ADMIN_USER

    accounts_data.sort(key=lambda x: x.get("gained_exp", 0), reverse=True)

    total_owned = len(accounts_data)

    # numbers must only describe what this login can see: the admin sees the
    # global totals, a normal user only his own accounts
    if admin:
        total_matches = bot_state.total_matches
        total_exp = bot_state.total_gained_exp
    else:
        total_matches = sum(int(a.get("matches_played", 0) or 0) for a in accounts_data)
        total_exp = sum(int(a.get("gained_exp", 0) or 0) for a in accounts_data)

    # ---- filter + search (server side, so paging stays correct) -----------
    def _is_live(a):
        if int(a.get("active_matches", 0) or 0) > 0:
            return True
        t = str(a.get("status") or "").upper()
        return any(k in t for k in ("MATCH", "ONLINE", "SEARCH", "QUEUE",
                                    "CONNECT", "IDLE", "LOADING"))

    def _is_paused(a):
        t = str(a.get("status") or "").upper()
        return any(k in t for k in ("PAUSE", "STOP", "THROTTL"))

    filtered = accounts_data
    if ffilter == "live":
        filtered = [a for a in filtered if _is_live(a)]
    elif ffilter == "paused":
        filtered = [a for a in filtered if _is_paused(a)]
    if qtext:
        filtered = [
            a for a in filtered
            if qtext in f"{a.get('uid','')} {a.get('nickname','')} "
                        f"{a.get('region','')} {a.get('owner','')}".lower()
        ]

    total_filtered = len(filtered)
    page = filtered[offset:offset + limit]

    # Enrich only the returned page. get_status() re-reads daily_exp.json on
    # every call, so calling it for all 10k accounts each poll would melt the
    # dashboard; the page is all the UI needs.
    if daily_exp_tracker is not None:
        for a in page:
            try:
                st = await daily_exp_tracker.get_status(str(a.get("uid", "")))
                a["daily_exp_today"] = st["exp_gained_today"]
                a["daily_exp_cap"] = st["cap"]
                a["daily_exp_remaining"] = st["remaining"]
                a["daily_exp_reached"] = st["reached"]
            except Exception:
                a["daily_exp_today"] = 0
                a["daily_exp_cap"] = daily_exp_tracker.DAILY_EXP_CAP
                a["daily_exp_remaining"] = daily_exp_tracker.DAILY_EXP_CAP
                a["daily_exp_reached"] = False

    me = _me_payload(user)

    return web.json_response({
        "total_accounts": total_owned,
        "accounts_total": total_filtered,
        "offset": offset,
        "limit": limit,
        "max_accounts": me["max_accounts"],
        "total_matches": total_matches,
        "total_gained_exp": total_exp,
        "daily_exp_cap": (daily_exp_tracker.DAILY_EXP_CAP if daily_exp_tracker else 50000),
        "accounts": page,
        "logs": bot_state.logs[-60:],
        "me": me,
    })


# ============================================================
# account routes
# ============================================================
async def handle_add_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        user = request.get("user")
        owner = user or auth_system.ADMIN_USER

        existing = _read_accounts_file()
        if len(existing) >= MAX_ACCOUNTS:
            return web.json_response({
                "status": "error",
                "error": f"Maximum {MAX_ACCOUNTS} accounts allowed"
            })

        # per-user quota (admin has no personal limit beyond MAX_ACCOUNTS)
        if not auth_system.is_admin(user):
            quota = _quota_for(user)
            used = count_owned(user)
            if used >= quota:
                return web.json_response({
                    "status": "error",
                    "error": f"Account limit reached ({used}/{quota})"
                })

        target_level = int(data.get("target_level", 0) or 0)
        target_matches = int(data.get("target_matches", 0) or 0)
        match_mode = normalise_mode(data.get("match_mode", ""))
        speed_mode = normalise_speed_mode(data.get("speed_mode", ""))

        if "uid" in data and "password" in data:
            uid = str(data["uid"]).strip()
            pwd = str(data["password"]).strip()
            if not uid or not pwd:
                return web.json_response({"status": "error", "error": "UID & Password required"})
            entry = {
                "uid": uid, "password": pwd,
                "target_level": target_level,
                "target_matches": target_matches,
                "match_mode": match_mode,
                "speed_mode": speed_mode,
                "owner": owner,
            }
            kind, ident = "uid", uid
        elif "token" in data:
            token = str(data["token"]).strip()
            if not token:
                return web.json_response({"status": "error", "error": "Token required"})
            entry = {
                "token": token,
                "target_level": target_level,
                "target_matches": target_matches,
                "match_mode": match_mode,
                "speed_mode": speed_mode,
                "owner": owner,
            }
            kind, ident = "token", token
        else:
            return web.json_response({"status": "error", "error": "Invalid payload"})

        # Start it for real FIRST. The callback signs in and only reports
        # added=True once the account is registered in bot_state, so the panel
        # and accounts.json can never drift apart: a rejected account is not
        # saved, and a saved account is always visible.
        cb = bot_state.refresh_callbacks.get("on_account_added")
        if cb is None:
            return web.json_response({
                "status": "error",
                "error": "Bot is still starting up — please try again in a moment"
            })

        payload = dict(data)
        payload.update({
            "target_level": target_level,
            "target_matches": target_matches,
            "match_mode": match_mode,
            "speed_mode": speed_mode,
            "owner": owner,
        })
        try:
            result = await cb(payload)
        except Exception as e:
            return web.json_response({
                "status": "error",
                "error": f"Could not start the account: {e}"
            })

        if not (result and result.get("added")):
            reason = (result or {}).get("reason") or "Could not start the account"
            return web.json_response({"status": "error", "error": reason})

        # Attribute the account to whoever added it, using the id the game
        # reported too, so the owner sees it even if that differs from the
        # login uid they typed.
        acc_id = str((result or {}).get("account_id") or "")
        if acc_id:
            _uid_owner[acc_id] = owner

        # Persist only what actually started. Re-read first: the callback may
        # have touched the file and a concurrent add could have landed too.
        fresh = _read_accounts_file()
        if kind == "uid":
            fresh = [a for a in fresh if str(a.get("uid") or "") != ident]
            _uid_owner[ident] = owner
        else:
            fresh = [a for a in fresh if str(a.get("token") or "") != ident]
            _token_owner[ident] = owner
        fresh.append(entry)
        _write_accounts_file(fresh)

        bot_state.log(
            f"New account added by {owner} ({speed_mode} mode, Target: Lv."
            f"{target_level or 'MAX'} / {target_matches or '∞'} matches)",
            "success"
        )
        return web.json_response({"status": "ok", "result": result})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_upload_guest_file(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        user = request.get("user")
        accounts_list = data.get("accounts", [])
        target_level = int(data.get("target_level", 0) or 0)
        target_matches = int(data.get("target_matches", 0) or 0)
        match_mode = normalise_mode(data.get("match_mode", ""))
        speed_mode = normalise_speed_mode(data.get("speed_mode", ""))

        if not isinstance(accounts_list, list):
            return web.json_response({"status": "error", "error": "Invalid format"})

        existing = _read_accounts_file()

        valid_items = []
        seen = set()
        for item in accounts_list:
            if not isinstance(item, dict):
                continue
            uid = str(item.get("uid", "")).strip()
            pwd = str(item.get("password", "")).strip()
            if not uid or not pwd or uid in seen:
                continue
            seen.add(uid)
            valid_items.append({"uid": uid, "password": pwd})

        if not valid_items:
            return web.json_response({
                "status": "error",
                "error": "No valid UID:Password lines found",
            })

        skipped_details = []

        # global cap: count what is already saved, not just what is running
        room_cap = MAX_ACCOUNTS - len(existing)
        if room_cap <= 0:
            return web.json_response({
                "status": "error",
                "error": f"Maximum {MAX_ACCOUNTS} accounts allowed",
            })
        if len(valid_items) > room_cap:
            for item in valid_items[room_cap:]:
                skipped_details.append(f"{item['uid']}: server full ({MAX_ACCOUNTS})")
            valid_items = valid_items[:room_cap]

        # per-user quota: keep only what still fits, report the rest
        if not auth_system.is_admin(user):
            quota = _quota_for(user)
            room = quota - count_owned(user)
            if room <= 0:
                return web.json_response({
                    "status": "error",
                    "error": f"Account limit reached ({quota}/{quota})",
                })
            if len(valid_items) > room:
                for item in valid_items[room:]:
                    skipped_details.append(f"{item['uid']}: account limit reached")
                valid_items = valid_items[:room]

        owner = user or auth_system.ADMIN_USER
        cb = bot_state.refresh_callbacks.get("on_account_added")
        if cb is None:
            return web.json_response({
                "status": "error",
                "error": "Bot is still starting up — please try again in a moment",
            })

        # Sign in each account first; only the ones that really started are
        # written to accounts.json, so nothing gets saved but left invisible.
        added_entries = []
        for item in valid_items:
            if len(bot_state.accounts) >= MAX_ACCOUNTS:
                skipped_details.append(f"{item['uid']}: cap reached")
                continue
            try:
                result = await cb({
                    "uid": item["uid"],
                    "password": item["password"],
                    "target_level": target_level,
                    "target_matches": target_matches,
                    "match_mode": match_mode,
                    "speed_mode": speed_mode,
                    "owner": owner,
                })
            except Exception as e:
                print(f"[UPLOAD] Callback error for {item['uid']}: {e}")
                skipped_details.append(f"{item['uid']}: {e}")
                continue
            if result and result.get("added"):
                _uid_owner[item["uid"]] = owner
                added_entries.append({
                    "uid": item["uid"], "password": item["password"],
                    "target_level": target_level,
                    "target_matches": target_matches,
                    "match_mode": match_mode,
                    "speed_mode": speed_mode,
                    "owner": owner,
                })
            else:
                reason = (result or {}).get("reason") or "could not start"
                skipped_details.append(f"{item['uid']}: {reason}")

        if added_entries:
            fresh = _read_accounts_file()
            for entry in added_entries:
                fresh = [a for a in fresh if str(a.get("uid") or "") != entry["uid"]]
                fresh.append(entry)
            _write_accounts_file(fresh)

        added_count = len(added_entries)
        skipped_count = len(skipped_details)

        bot_state.log(
            f"File upload by {owner} → {added_count} added, {skipped_count} skipped",
            "success"
        )
        return web.json_response({
            "status": "ok",
            "added": added_count,
            "skipped": skipped_count,
            "skipped_details": skipped_details,
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_refresh_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        denied = _owner_check(request, uid)
        if denied:
            return denied
        cb = bot_state.refresh_callbacks.get("on_refresh_account")
        if cb:
            asyncio.create_task(cb(uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        denied = _owner_check(request, uid)
        if denied:
            return denied
        cb = bot_state.refresh_callbacks.get("on_restart_account")
        if cb:
            asyncio.create_task(cb(uid))
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_account(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        denied = _owner_check(request, uid)
        if denied:
            return denied
        cb = bot_state.refresh_callbacks.get("on_delete_account")
        if cb:
            try:
                await cb(uid)
            except Exception as e:
                print(f"[DELETE] Callback error: {e}")
        # Guarantee the saved entry is gone even when the worker never
        # registered (otherwise it would silently come back on restart).
        removed_token = _remove_account_entry(uid)
        _forget_owner(uid)
        if removed_token:
            _token_owner.pop(removed_token, None)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_toggle_pause(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        uid = str(data.get("uid", "")).strip()
        if not uid:
            return web.json_response({"status": "error", "error": "UID required"})
        denied = _owner_check(request, uid)
        if denied:
            return denied
        cb = bot_state.refresh_callbacks.get("on_toggle_pause")
        if cb:
            res = await cb(uid)
            return web.json_response({"status": "ok", "result": res})
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_delete_all(request: web.Request) -> web.Response:
    try:
        user = request.get("user")
        cb = bot_state.refresh_callbacks.get("on_delete_account")
        all_cb = bot_state.refresh_callbacks.get("on_delete_all")

        if auth_system.is_admin(user):
            _uid_owner.clear()
            _token_owner.clear()
            if all_cb:
                asyncio.create_task(all_cb())
            return web.json_response({"status": "ok"})

        # a normal user only wipes HIS OWN accounts
        owned = [uid for uid in list(bot_state.accounts) if owner_of(uid) == user]
        _remove_owner_entries(user)
        if cb:
            for uid in owned:
                asyncio.create_task(cb(uid))
        bot_state.log(f"🗑️ {user} removed all of his accounts ({len(owned)})", "warning")
        return web.json_response({"status": "ok", "removed": len(owned)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


async def handle_restart_all(request: web.Request) -> web.Response:
    try:
        user = request.get("user")
        cb = bot_state.refresh_callbacks.get("on_restart_account")
        all_cb = bot_state.refresh_callbacks.get("on_restart_all")

        if auth_system.is_admin(user):
            if all_cb:
                asyncio.create_task(all_cb())
            return web.json_response({"status": "ok"})

        owned = [uid for uid in list(bot_state.accounts) if owner_of(uid) == user]
        if cb:
            for uid in owned:
                asyncio.create_task(cb(uid))
        return web.json_response({"status": "ok", "restarted": len(owned)})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)})


def _forget_owner(uid: str) -> None:
    uid = str(uid)
    _uid_owner.pop(uid, None)
    tok = _auth_token_for(uid)
    if tok:
        _token_owner.pop(tok, None)


def _remove_owner_entries(username: str) -> None:
    """Drop a user's pending entries from accounts.json (delete-all path)."""
    entries = _read_accounts_file()
    keep = [e for e in entries if str(e.get("owner") or "") != username]
    if len(keep) != len(entries):
        _write_accounts_file(keep)
    for e in entries:
        if str(e.get("owner") or "") == username:
            if e.get("uid"):
                _uid_owner.pop(str(e["uid"]), None)
            if e.get("token"):
                _token_owner.pop(str(e["token"]), None)


# ============================================================
# token generator  (EAT -> access token, via the official Garena callback)
# ============================================================
_EAT_RE = re.compile(r"^[A-Za-z0-9._\-]{16,2000}$")


def extract_eat(raw: str) -> str:
    """Accept either the full callback URL or the bare ?eat= value."""
    raw = str(raw or "").strip().strip('"\'')
    if not raw:
        return ""
    if "eat=" in raw.lower():
        m = re.search(r"[?&/]eat=([^&#\s]+)", raw, re.IGNORECASE)
        if not m:
            m = re.search(r"eat=([^&#\s]+)", raw, re.IGNORECASE)
        if m:
            return unquote(m.group(1)).strip()
    return raw


async def handle_token_generate(request: web.Request) -> web.Response:
    """Exchange an EAT for a real Garena access token.

    Garena's own support-callback endpoint answers a valid EAT with a 302
    whose Location query carries access_token / account_id / nickname /
    region — exactly what the standalone generator sites do, but served
    straight from our own backend so tokens never leave this site.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid request"}, status=400)

    eat = extract_eat(data.get("token", ""))
    if not eat or not _EAT_RE.match(eat):
        return web.json_response({
            "status": "error",
            "error": "Not a valid EAT — paste the full callback URL or the ?eat= value",
        })

    location = ""
    try:
        timeout = ClientTimeout(total=20)
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36")
        }
        async with ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(
                "https://api-otrss.garena.com/support/callback/",
                params={"access_token": eat},
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location", "") or ""
    except Exception as e:
        bot_state.log(f"[TOKEN] Garena callback unreachable: {e}", "warning")
        return web.json_response({
            "status": "error",
            "error": f"Could not reach Garena: {e}",
        })

    if not location:
        return web.json_response({
            "status": "error",
            "error": "Conversion failed — the token is invalid or already expired",
        })

    q = parse_qs(urlsplit(location).query)
    first = lambda k: (q.get(k) or [""])[0]

    access_token = first("access_token")
    if not access_token:
        return web.json_response({
            "status": "error",
            "error": "Conversion failed — the token is invalid or already expired",
        })

    bot_state.log(
        f"[TOKEN] Generated access token for "
        f"{first('nickname') or '?'} ({first('account_id') or '?'})",
        "success",
    )
    return web.json_response({
        "status": "ok",
        "access_token": access_token,
        "account_id": first("account_id") or first("uid"),
        "nickname": first("nickname"),
        "region": first("region"),
    })


# ============================================================
# admin: user management
# ============================================================
async def handle_admin_users(request: web.Request) -> web.Response:
    users = auth_system.list_users()
    for u in users:
        u["used"] = count_owned(u["username"])
    return web.json_response({"status": "ok", "users": users})


async def handle_admin_user_create(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid request"}, status=400)
    ok, err = auth_system.create_user(
        str(data.get("username", "")),
        str(data.get("password", "")),
        data.get("days", 0),
        data.get("max_accounts", 1),
    )
    if not ok:
        return web.json_response({"status": "error", "error": err})
    bot_state.log(f"👤 New login created: {data.get('username')}", "success")
    return web.json_response({"status": "ok"})


async def handle_admin_user_update(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid request"}, status=400)

    fields: Dict[str, Any] = {}
    if data.get("password") is not None:
        fields["password"] = str(data.get("password") or "")
    if "days" in data:
        fields["days"] = data.get("days")
    if "max_accounts" in data:
        fields["max_accounts"] = data.get("max_accounts")
    if "enabled" in data:
        fields["enabled"] = bool(data.get("enabled"))

    ok, err = auth_system.update_user(str(data.get("username", "")), **fields)
    if not ok:
        return web.json_response({"status": "error", "error": err})
    return web.json_response({"status": "ok"})


async def handle_admin_user_delete(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "Invalid request"}, status=400)
    username = str(data.get("username", "")).strip()
    ok, err = auth_system.delete_user(username)
    if not ok:
        return web.json_response({"status": "error", "error": err})
    bot_state.log(f"👤 Login deleted: {username}", "warning")
    return web.json_response({"status": "ok"})


# ============================================================
# server
# ============================================================
async def start_web_dashboard(host: str = "0.0.0.0", port: int = 5000):
    load_owners()

    # Restored-after-restart report: the admin's created logins live in
    # auth.json and the account list in accounts.json — both survive a
    # disconnect/restart, and this line proves it at every boot.
    try:
        users = auth_system.list_users()
        sessions = len(auth_system._load().get("sessions", {}) or {})
        print(f"\033[92m[+] Restored: {len(users)} login(s), "
              f"{len(_read_accounts_file())} account(s), "
              f"{sessions} active session(s)\033[0m")
    except Exception:
        pass

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_index)
    # Static assets (css/js). add_static() raises if the folder is missing,
    # so guard it — the dashboard still works, just unstyled.
    if os.path.isdir(STATIC_DIR):
        app.router.add_static("/static/", STATIC_DIR, name="static")
    app.router.add_post("/api/auth/login", handle_login)
    app.router.add_post("/api/auth/logout", handle_logout)
    app.router.add_get("/api/auth/me", handle_me)
    app.router.add_get("/api/stats", handle_get_stats)
    app.router.add_post("/api/account/add", handle_add_account)
    app.router.add_post("/api/account/upload", handle_upload_guest_file)
    app.router.add_post("/api/account/refresh", handle_refresh_account)
    app.router.add_post("/api/account/restart", handle_restart_account)
    app.router.add_post("/api/account/delete", handle_delete_account)
    app.router.add_post("/api/account/toggle_pause", handle_toggle_pause)
    app.router.add_post("/api/account/delete_all", handle_delete_all)
    app.router.add_post("/api/account/restart_all", handle_restart_all)
    app.router.add_post("/api/token/generate", handle_token_generate)
    app.router.add_get("/api/admin/users", handle_admin_users)
    app.router.add_post("/api/admin/user/create", handle_admin_user_create)
    app.router.add_post("/api/admin/user/update", handle_admin_user_update)
    app.router.add_post("/api/admin/user/delete", handle_admin_user_delete)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"\033[92m[+] Dashboard running on http://localhost:{port}\033[0m")
    print(f"\033[92m[+] Max Accounts: {MAX_ACCOUNTS}\033[0m")
    print(f"\033[92m[+] Login required — admin user: {auth_system.ADMIN_USER}\033[0m")

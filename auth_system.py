# -*- coding: utf-8 -*-
"""
Login + user management for the FreeFire Level Bot dashboard.

Security design
---------------
* No plaintext password exists in any file of this project. Passwords are
  stored as salted PBKDF2-SHA256 hashes (120,000 rounds).
* The built-in admin credential is committed as salt + hash only, so the real
  admin password never appears in the source code, the templates, or auth.json.
* Sessions are random 64-hex tokens kept server-side; the browser only ever
  holds an opaque HttpOnly cookie.
* auth.json (users + sessions) is created automatically on first run.

Account records created by the admin carry:
    expires      -> unix timestamp, or null for "never expires"
    max_accounts -> how many bot accounts this user may add/upload
    enabled      -> admin can suspend a login without deleting it
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json")

SESSION_TTL = 7 * 24 * 3600          # sessions live 7 days
PBKDF2_ROUNDS = 120_000

# ---------------- built-in admin (hash only - never the password) ----------
ADMIN_USER = "admin"
_ADMIN_SALT = "d95fc8ffb25e9130a1770472fe7004f2"
_ADMIN_HASH = (
    "10cf56edadd1e5a5dd27474ca457a770325303ae1127b679e1ad1d3d95949c40"
)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,24}$")

_DEFAULT: Dict[str, Any] = {"users": {}, "sessions": {}}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def _load() -> Dict[str, Any]:
    if not os.path.exists(AUTH_FILE):
        _save(_DEFAULT)
        return json.loads(json.dumps(_DEFAULT))
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        data.setdefault("users", {})
        data.setdefault("sessions", {})
        if not isinstance(data["users"], dict) or not isinstance(data["sessions"], dict):
            raise ValueError("bad shape")
        return data
    except Exception:
        # Primary file exists but is unreadable (torn write / disk glitch).
        # The .bak mirror is written on every save, so it usually still holds
        # the last good copy of every login — restore it instead of wiping
        # all the admin's created logins.
        bak = AUTH_FILE + ".bak"
        if os.path.exists(bak):
            try:
                with open(bak, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("users", {})
                    data.setdefault("sessions", {})
                    if isinstance(data["users"], dict) and isinstance(data["sessions"], dict):
                        _save(data)
                        return data
            except Exception:
                pass
        # No usable backup: keep a backup aside and start clean rather than
        # locking the owner out of his own dashboard
        try:
            os.replace(AUTH_FILE, AUTH_FILE + ".corrupt")
        except Exception:
            pass
        _save(_DEFAULT)
        return json.loads(json.dumps(_DEFAULT))


def _save(data: Dict[str, Any]) -> None:
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, AUTH_FILE)
    # mirror every save so a corrupted primary can be rescued (see _load)
    try:
        bak_tmp = AUTH_FILE + ".bak.tmp"
        with open(bak_tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(bak_tmp, AUTH_FILE + ".bak")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def _pw_ok(password: str) -> bool:
    return isinstance(password, str) and len(password) >= 4


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------
def is_admin(username: Optional[str]) -> bool:
    return username == ADMIN_USER


def _verify_admin(password: str) -> bool:
    if not isinstance(password, str) or not password:
        return False
    return secrets.compare_digest(_hash(password, _ADMIN_SALT), _ADMIN_HASH)


# ---------------------------------------------------------------------------
# user records (public view - never leaks hashes)
# ---------------------------------------------------------------------------
def _public(username: str, rec: Dict[str, Any]) -> Dict[str, Any]:
    expires = rec.get("expires")
    now = time.time()
    days_left = None
    expired = False
    if expires:
        days_left = max(0, int((expires - now) // 86400))
        expired = expires <= now
    return {
        "username": username,
        "created": rec.get("created"),
        "expires": expires,
        "days_left": days_left,
        "max_accounts": int(rec.get("max_accounts", 0) or 0),
        "enabled": bool(rec.get("enabled", True)),
        "expired": expired,
    }


def get_user(username: str) -> Optional[Dict[str, Any]]:
    data = _load()
    rec = data["users"].get(username)
    if not isinstance(rec, dict):
        return None
    return _public(username, rec)


def list_users() -> List[Dict[str, Any]]:
    data = _load()
    out = [_public(u, r) for u, r in data["users"].items() if isinstance(r, dict)]
    out.sort(key=lambda x: x["username"].lower())
    return out


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
def verify_login(username: str, password: str) -> Optional[str]:
    """Return 'admin' or 'user' on success, None otherwise."""
    username = str(username or "").strip()
    if is_admin(username):
        return "admin" if _verify_admin(password) else None

    data = _load()
    rec = data["users"].get(username)
    if not isinstance(rec, dict) or not rec.get("enabled", True):
        return None
    if rec.get("expires") and rec["expires"] <= time.time():
        return None
    if not isinstance(rec.get("hash"), str) or not isinstance(rec.get("salt"), str):
        return None
    if not secrets.compare_digest(_hash(str(password or ""), rec["salt"]), rec["hash"]):
        return None
    return "user"


def user_is_active(username: str) -> bool:
    """Used when re-checking an existing session."""
    if is_admin(username):
        return True
    info = get_user(username)
    if not info:
        return False
    return info["enabled"] and not info["expired"]


# ---------------------------------------------------------------------------
# admin: create / update / delete users
# ---------------------------------------------------------------------------
def create_user(username: str, password: str, days: int,
                max_accounts: int) -> Tuple[bool, Optional[str]]:
    username = str(username or "").strip()
    if not _USERNAME_RE.match(username):
        return False, "Username must be 3-24 chars (letters, digits, _ . -)"
    if is_admin(username):
        return False, "That username is reserved"
    if not _pw_ok(password):
        return False, "Password must be at least 4 characters"

    try:
        days = int(days)
        max_accounts = int(max_accounts)
    except Exception:
        return False, "Days and account limit must be numbers"
    if days < 0:
        return False, "Days cannot be negative"
    if max_accounts < 1:
        return False, "Account limit must be at least 1"

    data = _load()
    if username in data["users"]:
        return False, "Username already exists"

    salt = secrets.token_hex(16)
    data["users"][username] = {
        "salt": salt,
        "hash": _hash(password, salt),
        "created": int(time.time()),
        "expires": (int(time.time()) + days * 86400) if days > 0 else None,
        "max_accounts": max_accounts,
        "enabled": True,
    }
    _save(data)
    return True, None


def update_user(username: str, password: Optional[str] = None,
                days: Optional[int] = None, max_accounts: Optional[int] = None,
                enabled: Optional[bool] = None) -> Tuple[bool, Optional[str]]:
    username = str(username or "").strip()
    if is_admin(username):
        return False, "The admin account cannot be edited here"

    data = _load()
    rec = data["users"].get(username)
    if not isinstance(rec, dict):
        return False, "User not found"

    if password is not None:
        if not _pw_ok(password):
            return False, "Password must be at least 4 characters"
        salt = secrets.token_hex(16)
        rec["salt"] = salt
        rec["hash"] = _hash(password, salt)

    if days is not None:
        try:
            days = int(days)
        except Exception:
            return False, "Days must be a number"
        if days < 0:
            return False, "Days cannot be negative"
        # 0 = no expiry, otherwise restart the clock from now
        rec["expires"] = (int(time.time()) + days * 86400) if days > 0 else None

    if max_accounts is not None:
        try:
            max_accounts = int(max_accounts)
        except Exception:
            return False, "Account limit must be a number"
        if max_accounts < 1:
            return False, "Account limit must be at least 1"
        rec["max_accounts"] = max_accounts

    if enabled is not None:
        rec["enabled"] = bool(enabled)
        if not enabled:
            _drop_sessions_for(username, data)

    _save(data)
    return True, None


def delete_user(username: str) -> Tuple[bool, Optional[str]]:
    username = str(username or "").strip()
    if is_admin(username):
        return False, "The admin account cannot be deleted"
    data = _load()
    if username not in data["users"]:
        return False, "User not found"
    del data["users"][username]
    _drop_sessions_for(username, data)
    _save(data)
    return True, None


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------
def create_session(username: str) -> str:
    data = _load()
    _purge(data)
    token = secrets.token_hex(32)
    data["sessions"][token] = {"user": username, "exp": int(time.time()) + SESSION_TTL}
    _save(data)
    return token


def session_user(token: Optional[str]) -> Optional[str]:
    if not token or not isinstance(token, str):
        return None
    data = _load()
    sess = data["sessions"].get(token)
    if not isinstance(sess, dict):
        return None
    if int(sess.get("exp", 0)) <= time.time():
        data["sessions"].pop(token, None)
        _save(data)
        return None
    username = str(sess.get("user") or "")
    # disabling / deleting a user must kick them out immediately
    if not user_is_active(username):
        data["sessions"].pop(token, None)
        _save(data)
        return None
    return username


def destroy_session(token: Optional[str]) -> None:
    if not token:
        return
    data = _load()
    if token in data["sessions"]:
        del data["sessions"][token]
        _save(data)


def _drop_sessions_for(username: str, data: Dict[str, Any]) -> None:
    """Forget every live session of `username`; caller persists via _save()."""
    dead = [t for t, s in data["sessions"].items()
            if isinstance(s, dict) and s.get("user") == username]
    for t in dead:
        del data["sessions"][t]


def _purge(data: Dict[str, Any]) -> None:
    now = time.time()
    dead = [t for t, s in data["sessions"].items()
            if not isinstance(s, dict) or int(s.get("exp", 0)) <= now]
    for t in dead:
        del data["sessions"][t]


# ---------------------------------------------------------------------------
# login-page token (bot friction)
# ---------------------------------------------------------------------------
# The login form carries a signed timestamp that only this server can mint.
# Scripts that POST straight at /api/auth/login without first rendering the
# page have no valid token and are rejected (and rate-limited) immediately.
# The secret lives in auth.json ("web_secret") so tokens keep working across
# a restart.
LOGIN_TOKEN_TTL = 24 * 3600          # page must be reloaded once a day


def _web_secret() -> str:
    data = _load()
    secret = data.get("web_secret")
    if not isinstance(secret, str) or len(secret) < 32:
        secret = secrets.token_hex(32)
        data["web_secret"] = secret
        _save(data)
    return secret


def issue_login_token() -> str:
    """`ts.hmac` — 24h lifetime, stateless, verified by check_login_token()."""
    ts = str(int(time.time()))
    sig = hmac.new(_web_secret().encode("utf-8"),
                   ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def login_token_state(token: Optional[str]) -> str:
    """'ok' | 'missing' | 'bad' | 'expired' — drives the login-page UX:
    'expired' reloads the page for a fresh token, 'missing'/'bad' means the
    POST never came from our form (a bot) and is rate-limited as a failure."""
    if not token or not isinstance(token, str) or token.count(".") != 1:
        return "missing"
    ts_s, _, sig = token.partition(".")
    if not ts_s.isdigit() or len(sig) != 64:
        return "bad"
    expect = hmac.new(_web_secret().encode("utf-8"),
                      ts_s.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return "bad"
    try:
        age = time.time() - int(ts_s)
    except ValueError:
        return "bad"
    if age < -60 or age > LOGIN_TOKEN_TTL:
        return "expired"
    return "ok"


def check_login_token(token: Optional[str]) -> bool:
    return login_token_state(token) == "ok"

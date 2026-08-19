"""User accounts + session auth for the AppBuilder WebUI.

Everyone who can log in is an admin — there are no roles. The point is to stop an
unauthenticated browser on the LAN from driving a service that holds a GitHub
token, can push commits, and can restart itself.

Deliberately STDLIB ONLY. requirements.txt has no passlib/bcrypt/itsdangerous, and
adding a dependency to the auth path of a self-updating service is a poor trade —
`hashlib.scrypt` and `hmac` are in the standard library and sufficient here.

Storage: ``/etc/ab/users.json``, 0600, next to config.json (a directory the
service already owns and writes). It holds the per-user salt+hash and the session
signing secret. It is NEVER logged and never returned by an API.

Passwords: scrypt (n=2**14, r=8, p=1) with a 16-byte per-user salt. Verification
is constant-time via hmac.compare_digest.

Sessions: a signed cookie, not server-side state — the service restarts often
(every deploy), and server-side sessions would log everyone out each time. The
secret is persisted for the same reason. Token is ``base64(payload).hexsig``
where payload carries the username and an absolute expiry; the signature covers
the payload, so neither can be edited without the secret.

RECOVERY (locked out): from a shell on the box, as root or the service user —

    python3 -c "import sys; sys.path.insert(0,'/opt/ab'); import auth; \\
                auth.set_password('you','newpassword'); print('ok')"

and to start over completely, delete /etc/ab/users.json — the next request
re-enters first-run setup.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime

logger = logging.getLogger("AppBuilder")

try:
    from config_store import CONFIG_DIR
except Exception:  # noqa: BLE001 — keep auth importable in isolation (recovery CLI)
    CONFIG_DIR = os.environ.get("AB_CONFIG_DIR", "/etc/ab")

USERS_FILE = os.path.join(CONFIG_DIR, "users.json")

SESSION_COOKIE = "ab_session"
SESSION_TTL_S = int(os.environ.get("AB_SESSION_TTL_S", str(14 * 24 * 3600)))

# scrypt is preferred but is an OPTIONAL OpenSSL feature — Pythons linked against
# LibreSSL (macOS system Python, some BSDs) have no hashlib.scrypt at all. An auth
# path must not depend on a build option, so PBKDF2-HMAC-SHA256 is the fallback:
# always present, and adequate at a high iteration count. The algorithm and its
# parameters are recorded PER RECORD, so a hash written on one host still verifies
# on another and the cost can be raised later without invalidating old passwords.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2 ** 14, 8, 1, 32
_PBKDF2_ITERS = 480_000
_HAVE_SCRYPT = hasattr(hashlib, "scrypt")

MIN_PASSWORD_LEN = 8


# ── store ────────────────────────────────────────────────────────────────────

def _blank_store() -> dict:
    return {"users": {}, "session_secret": secrets.token_hex(32)}


def _read_store() -> dict:
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("users.json is not an object")
        data.setdefault("users", {})
        # A store written before the secret existed, or one that lost it, must not
        # silently accept unsigned tokens — mint a new secret (invalidates sessions).
        if not data.get("session_secret"):
            data["session_secret"] = secrets.token_hex(32)
            _write_store(data)
        return data
    except FileNotFoundError:
        return _blank_store()
    except Exception as e:  # noqa: BLE001
        # Fail CLOSED: a corrupt store must not be treated as "no users", which
        # would re-open first-run setup to anyone who can reach the port.
        logger.error("auth: could not read %s (%s) — refusing all logins until fixed.",
                     USERS_FILE, e)
        return {"users": {"__unreadable__": {}}, "session_secret": secrets.token_hex(32),
                "_unreadable": True}


def _write_store(data: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, USERS_FILE)   # atomic: never leave a half-written user store


def store_is_readable() -> bool:
    return not _read_store().get("_unreadable")


def any_users() -> bool:
    """True once at least one account exists (first-run setup is then closed)."""
    return bool(_read_store().get("users"))


def list_users() -> list:
    """Usernames + metadata. Never includes salts or hashes."""
    users = _read_store().get("users") or {}
    return sorted(
        ({"username": u,
          "created": rec.get("created"),
          "last_login": rec.get("last_login")}
         for u, rec in users.items() if isinstance(rec, dict)),
        key=lambda r: r["username"],
    )


# ── passwords ────────────────────────────────────────────────────────────────

def _hash(password: str, salt: bytes, algo: str | None = None,
          iters: int | None = None) -> tuple[str, str, int]:
    """Derive a hash. Returns (hex_digest, algo, iters) so the caller records how
    it was produced and verification never has to guess."""
    algo = algo or ("scrypt" if _HAVE_SCRYPT else "pbkdf2_sha256")
    pw = (password or "").encode("utf-8")
    if algo == "scrypt":
        if not _HAVE_SCRYPT:
            # Record written on a host WITH scrypt, now verifying on one without.
            raise RuntimeError("this Python has no hashlib.scrypt (OpenSSL build) — "
                               "cannot verify a scrypt password record here")
        return (hashlib.scrypt(pw, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                               p=_SCRYPT_P, dklen=_DKLEN).hex(), "scrypt", 0)
    it = int(iters or _PBKDF2_ITERS)
    return (hashlib.pbkdf2_hmac("sha256", pw, salt, it, dklen=_DKLEN).hex(),
            "pbkdf2_sha256", it)


def validate_password(password: str) -> str | None:
    """Return an error string, or None when acceptable."""
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None


def create_user(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    if not username:
        return False, "Username is required."
    err = validate_password(password)
    if err:
        return False, err
    data = _read_store()
    if data.get("_unreadable"):
        return False, "User store is unreadable — fix or remove users.json."
    if username in (data.get("users") or {}):
        return False, f"User {username!r} already exists."
    salt = secrets.token_bytes(16)
    _digest, _algo, _iters = _hash(password, salt)
    data.setdefault("users", {})[username] = {
        "salt": salt.hex(),
        "hash": _digest,
        "algo": _algo,
        "iters": _iters,
        "created": datetime.now().isoformat(timespec="seconds"),
        "last_login": None,
    }
    _write_store(data)
    logger.info("auth: created user %r", username)
    return True, f"User {username!r} created."


def set_password(username: str, password: str) -> tuple[bool, str]:
    err = validate_password(password)
    if err:
        return False, err
    data = _read_store()
    rec = (data.get("users") or {}).get(username)
    if not isinstance(rec, dict):
        return False, f"No such user {username!r}."
    salt = secrets.token_bytes(16)
    _digest, _algo, _iters = _hash(password, salt)
    rec.update({"salt": salt.hex(), "hash": _digest, "algo": _algo, "iters": _iters})
    _write_store(data)
    logger.info("auth: password changed for %r", username)
    return True, "Password updated."


def delete_user(username: str) -> tuple[bool, str]:
    data = _read_store()
    users = data.get("users") or {}
    if username not in users:
        return False, f"No such user {username!r}."
    if len(users) <= 1:
        # Removing the last account would lock everyone out and silently re-open
        # first-run setup to whoever reaches the port first.
        return False, "Cannot delete the last remaining user."
    users.pop(username, None)
    _write_store(data)
    logger.info("auth: deleted user %r", username)
    return True, f"User {username!r} deleted."


def verify_credentials(username: str, password: str) -> bool:
    data = _read_store()
    rec = (data.get("users") or {}).get((username or "").strip())
    if not isinstance(rec, dict) or not rec.get("salt") or not rec.get("hash"):
        # Hash anyway so a missing user costs the same time as a wrong password
        # (no user-enumeration signal from response timing).
        _hash(password or "", secrets.token_bytes(16))
        return False
    try:
        calc, _, _ = _hash(password or "", bytes.fromhex(rec["salt"]),
                           algo=rec.get("algo"), iters=rec.get("iters"))
    except Exception as e:  # noqa: BLE001
        logger.error("auth: cannot verify %r (%s)", username, e)
        return False
    if not hmac.compare_digest(calc, rec["hash"]):
        return False
    rec["last_login"] = datetime.now().isoformat(timespec="seconds")
    try:
        _write_store(data)
    except Exception as e:  # noqa: BLE001 — a stamp failure must not block login
        logger.debug("auth: could not record last_login: %s", e)
    return True


# ── sessions ─────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    return (_read_store().get("session_secret") or "").encode("utf-8")


def issue_session(username: str, ttl_s: int | None = None) -> str:
    payload = json.dumps({"u": username,
                          "exp": int(time.time()) + int(ttl_s or SESSION_TTL_S)},
                         separators=(",", ":")).encode("utf-8")
    b = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b}.{sig}"


def verify_session(token: str) -> str | None:
    """Return the username for a valid, unexpired token, else None."""
    if not token or "." not in token:
        return None
    b, _, sig = token.rpartition(".")
    expect = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        raw = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    username = data.get("u")
    # A token signed for a since-deleted account must stop working immediately.
    if username not in (_read_store().get("users") or {}):
        return None
    return username


def rotate_session_secret() -> None:
    """Invalidate every outstanding session (e.g. after removing a user)."""
    data = _read_store()
    data["session_secret"] = secrets.token_hex(32)
    _write_store(data)
    logger.warning("auth: session secret rotated — all users must log in again.")

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
# Postpone evaluation of type hints so "str | None" annotations work on older Pythons.
from __future__ import annotations

# base64 + hmac + hashlib build and check signed session tokens; hashlib also hashes
# passwords; json reads/writes users.json; secrets makes salts and signing secrets;
# time and datetime give token expiry and account timestamps.
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime

# Share the main AppBuilder logger so auth messages land in the same log.
logger = logging.getLogger("AppBuilder")

# Use the service's config directory; if config_store cannot be imported (e.g. running the
# recovery one-liner on its own), fall back to AB_CONFIG_DIR or /etc/ab.
try:
    from config_store import CONFIG_DIR
except Exception:  # noqa: BLE001 — keep auth importable in isolation (recovery CLI)
    CONFIG_DIR = os.environ.get("AB_CONFIG_DIR", "/etc/ab")

# Where accounts and the session signing secret are stored.
USERS_FILE = os.path.join(CONFIG_DIR, "users.json")

# Name of the browser cookie carrying the session token.
SESSION_COOKIE = "ab_session"
# Session lifetime in seconds: 14 days unless AB_SESSION_TTL_S overrides it.
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

# Shortest password accepted when creating or changing an account.
MIN_PASSWORD_LEN = 8


# ── store ────────────────────────────────────────────────────────────────────

def _blank_store() -> dict:
    """Return an empty user store with a freshly generated session signing secret."""
    return {"users": {}, "session_secret": secrets.token_hex(32)}


def _read_store() -> dict:
    """Load users.json, repairing a missing secret; returns a "locked" store if unreadable."""
    # Read and parse the store; errors are handled by the except clauses below.
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
        # The file must hold a JSON object; anything else is treated as corrupt.
        if not isinstance(data, dict):
            raise ValueError("users.json is not an object")
        # Make sure the "users" key exists even in an older or minimal file.
        data.setdefault("users", {})
        # A store written before the secret existed, or one that lost it, must not
        # silently accept unsigned tokens — mint a new secret (invalidates sessions).
        if not data.get("session_secret"):
            data["session_secret"] = secrets.token_hex(32)
            _write_store(data)
        return data
    # No file yet: this is a fresh install, so start with an empty store.
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
    """Save the user store atomically with owner-only (0600) permissions."""
    # Make sure the config directory exists.
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Write to a temporary file first so a crash cannot leave a half-written store.
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    # Restrict the file to its owner; ignore filesystems that do not support chmod.
    try:
        os.chmod(tmp, 0o600)
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, USERS_FILE)   # atomic: never leave a half-written user store


def store_is_readable() -> bool:
    """Return True unless users.json exists but could not be read (fail-closed state)."""
    return not _read_store().get("_unreadable")


def any_users() -> bool:
    """True once at least one account exists (first-run setup is then closed)."""
    # Any non-empty users map means setup has already happened.
    return bool(_read_store().get("users"))


def list_users() -> list:
    """Usernames + metadata. Never includes salts or hashes."""
    # Return one public record per user, sorted by name, without secrets.
    users = _read_store().get("users") or {}
    return sorted(
        ({"username": u,
          "created": rec.get("created"),
          "last_login": rec.get("last_login"),
          "auth_type": rec.get("auth_type", "local")}
         for u, rec in users.items() if isinstance(rec, dict)),
        key=lambda r: r["username"],
    )


# ── passwords ────────────────────────────────────────────────────────────────

def _hash(password: str, salt: bytes, algo: str | None = None,
          iters: int | None = None) -> tuple[str, str, int]:
    """Derive a hash. Returns (hex_digest, algo, iters) so the caller records how
    it was produced and verification never has to guess."""
    # Default to scrypt when this Python has it, else PBKDF2; text is hashed as UTF-8 bytes.
    algo = algo or ("scrypt" if _HAVE_SCRYPT else "pbkdf2_sha256")
    pw = (password or "").encode("utf-8")
    # scrypt branch: refuse clearly if the record needs scrypt but this Python lacks it.
    if algo == "scrypt":
        if not _HAVE_SCRYPT:
            # Record written on a host WITH scrypt, now verifying on one without.
            raise RuntimeError("this Python has no hashlib.scrypt (OpenSSL build) — "
                               "cannot verify a scrypt password record here")
        return (hashlib.scrypt(pw, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                               p=_SCRYPT_P, dklen=_DKLEN).hex(), "scrypt", 0)
    # PBKDF2 branch: use the record's iteration count, or the current default for new hashes.
    it = int(iters or _PBKDF2_ITERS)
    return (hashlib.pbkdf2_hmac("sha256", pw, salt, it, dklen=_DKLEN).hex(),
            "pbkdf2_sha256", it)


def validate_password(password: str) -> str | None:
    """Return an error string, or None when acceptable."""
    # Reject empty or too-short passwords with a message the UI can show.
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    # Password is acceptable.
    return None


def create_user(username: str, password: str) -> tuple[bool, str]:
    """Create a new local password account; returns (ok, message)."""
    # Usernames are trimmed and must not be empty.
    username = (username or "").strip()
    if not username:
        return False, "Username is required."
    # Enforce the password policy.
    err = validate_password(password)
    if err:
        return False, err
    # Load the store; refuse to write over an unreadable one.
    data = _read_store()
    if data.get("_unreadable"):
        return False, "User store is unreadable — fix or remove users.json."
    # Usernames must be unique.
    if username in (data.get("users") or {}):
        return False, f"User {username!r} already exists."
    # Hash the password with a fresh random 16-byte salt.
    salt = secrets.token_bytes(16)
    _digest, _algo, _iters = _hash(password, salt)
    # Store the salt, hash and how it was made, plus timestamps.
    data.setdefault("users", {})[username] = {
        "salt": salt.hex(),
        "hash": _digest,
        "algo": _algo,
        "iters": _iters,
        "created": datetime.now().isoformat(timespec="seconds"),
        "last_login": None,
    }
    # Persist the new account and report success.
    _write_store(data)
    logger.info("auth: created user %r", username)
    return True, f"User {username!r} created."


def upsert_external_user(username: str, provider: str = "entra",
                         email: str = "", name: str = "",
                         oid: str = "") -> tuple[bool, str]:
    """Create (first SSO login) or refresh a federated account that has NO
    password — it can only ever sign in via its identity provider (e.g. Azure
    Entra). Because the record carries no ``salt``/``hash``, ``verify_credentials``
    will always reject a password login for it, so the two auth paths can never
    cross over. Returns ``(ok, message)``."""
    # Usernames are trimmed and must not be empty.
    username = (username or "").strip()
    if not username:
        return False, "External username is required."
    # Load the store; refuse to write over an unreadable one.
    data = _read_store()
    if data.get("_unreadable"):
        return False, "User store is unreadable — fix or remove users.json."
    # Look up any existing record and take one timestamp for this login.
    users = data.setdefault("users", {})
    rec = users.get(username)
    now = datetime.now().isoformat(timespec="seconds")
    # Existing account: refresh its profile details and last-login time.
    if isinstance(rec, dict):
        if rec.get("salt") or rec.get("hash"):
            # A local password account already owns this username — refuse to
            # silently convert it into a passwordless SSO account.
            return False, (f"User {username!r} is a local password account; "
                           "cannot convert it to an external account.")
        rec.update({"auth_type": provider, "email": email or rec.get("email", ""),
                    "name": name or rec.get("name", ""),
                    "oid": oid or rec.get("oid", ""), "last_login": now})
    # First SSO login: create a passwordless external account.
    else:
        users[username] = {
            "auth_type": provider,
            "email": email,
            "name": name,
            "oid": oid,
            "created": now,
            "last_login": now,
        }
    # Persist and report success.
    _write_store(data)
    logger.info("auth: %s user %r signed in", provider, username)
    return True, f"External user {username!r} provisioned."


def set_password(username: str, password: str) -> tuple[bool, str]:
    """Change an existing user's password; returns (ok, message)."""
    # Enforce the password policy first.
    err = validate_password(password)
    if err:
        return False, err
    # The user must exist.
    data = _read_store()
    rec = (data.get("users") or {}).get(username)
    if not isinstance(rec, dict):
        return False, f"No such user {username!r}."
    # Re-hash with a fresh salt and the current algorithm, replacing the old hash.
    salt = secrets.token_bytes(16)
    _digest, _algo, _iters = _hash(password, salt)
    rec.update({"salt": salt.hex(), "hash": _digest, "algo": _algo, "iters": _iters})
    # Persist and report success.
    _write_store(data)
    logger.info("auth: password changed for %r", username)
    return True, "Password updated."


def delete_user(username: str) -> tuple[bool, str]:
    """Delete a user account, refusing to remove the last one; returns (ok, message)."""
    # Load the store and make sure the user exists.
    data = _read_store()
    users = data.get("users") or {}
    if username not in users:
        return False, f"No such user {username!r}."
    if len(users) <= 1:
        # Removing the last account would lock everyone out and silently re-open
        # first-run setup to whoever reaches the port first.
        return False, "Cannot delete the last remaining user."
    # Remove the account and save.
    users.pop(username, None)
    _write_store(data)
    logger.info("auth: deleted user %r", username)
    return True, f"User {username!r} deleted."


def verify_credentials(username: str, password: str) -> bool:
    """Return True if the username/password pair is correct, and record the login time."""
    # Look up the user record (trimmed name).
    data = _read_store()
    rec = (data.get("users") or {}).get((username or "").strip())
    if not isinstance(rec, dict) or not rec.get("salt") or not rec.get("hash"):
        # Hash anyway so a missing user costs the same time as a wrong password
        # (no user-enumeration signal from response timing).
        _hash(password or "", secrets.token_bytes(16))
        return False
    # Re-hash the supplied password with the record's own salt and algorithm.
    try:
        calc, _, _ = _hash(password or "", bytes.fromhex(rec["salt"]),
                           algo=rec.get("algo"), iters=rec.get("iters"))
    # A record we cannot verify (e.g. scrypt record on a host without scrypt) fails closed.
    except Exception as e:  # noqa: BLE001
        logger.error("auth: cannot verify %r (%s)", username, e)
        return False
    # Constant-time comparison so timing does not reveal how much of the hash matched.
    if not hmac.compare_digest(calc, rec["hash"]):
        return False
    # Success: stamp the login time and try to save it.
    rec["last_login"] = datetime.now().isoformat(timespec="seconds")
    try:
        _write_store(data)
    except Exception as e:  # noqa: BLE001 — a stamp failure must not block login
        logger.debug("auth: could not record last_login: %s", e)
    return True


# ── sessions ─────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    """Return the session signing secret as bytes."""
    return (_read_store().get("session_secret") or "").encode("utf-8")


def ensure_session_secret() -> bytes:
    """Return the persisted signing secret, creating + writing an (empty) user
    store on first use so the secret is STABLE across calls.

    ``_read_store`` mints a fresh random secret every time the file is absent
    (``_blank_store``) without persisting it, so two calls would disagree. Any
    signature that must survive a round-trip *before* the first account exists —
    notably the OIDC state cookie when SSO is the very first login — needs this
    to pin the secret to disk once."""
    # No store on disk yet: create one now so the secret is written once and stays stable.
    if not os.path.exists(USERS_FILE):
        data = _blank_store()
        try:
            _write_store(data)
        except Exception as e:  # noqa: BLE001 — fall back to the in-memory secret
            logger.debug("auth: could not persist session secret: %s", e)
        return (data.get("session_secret") or "").encode("utf-8")
    # Store exists: use the secret it already holds.
    return _secret()


def issue_session(username: str, ttl_s: int | None = None) -> str:
    """Create a signed session token for username that expires after ttl_s seconds."""
    # Compact JSON payload with the username and an absolute expiry time.
    payload = json.dumps({"u": username,
                          "exp": int(time.time()) + int(ttl_s or SESSION_TTL_S)},
                         separators=(",", ":")).encode("utf-8")
    # URL-safe base64 without padding so the token is cookie-friendly.
    b = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    # HMAC-SHA256 signature over the encoded payload, using the stored secret.
    sig = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    # Token format: "<payload>.<signature>".
    return f"{b}.{sig}"


def verify_session(token: str) -> str | None:
    """Return the username for a valid, unexpired token, else None."""
    # A token must contain a "." separating payload and signature.
    if not token or "." not in token:
        return None
    # Split off the signature and recompute what it should be.
    b, _, sig = token.rpartition(".")
    expect = hmac.new(_secret(), b.encode("ascii"), hashlib.sha256).hexdigest()
    # Wrong signature means the token was forged or edited.
    if not hmac.compare_digest(sig, expect):
        return None
    # Decode the payload (re-adding base64 padding); garbage means an invalid token.
    try:
        raw = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    # Expired tokens are rejected.
    if int(data.get("exp", 0)) < time.time():
        return None
    # The username the token was issued for.
    username = data.get("u")
    # A token signed for a since-deleted account must stop working immediately.
    if username not in (_read_store().get("users") or {}):
        return None
    return username


def rotate_session_secret() -> None:
    """Invalidate every outstanding session (e.g. after removing a user)."""
    # Replace the secret with a new random one, which breaks every existing token.
    data = _read_store()
    data["session_secret"] = secrets.token_hex(32)
    _write_store(data)
    logger.warning("auth: session secret rotated — all users must log in again.")

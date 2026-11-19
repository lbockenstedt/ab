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
import tempfile
import threading
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

# AB starts one uvicorn process from its systemd service. This lock therefore
# serializes every in-process user-store read/modify/write without a cross-process
# lock file or a new dependency.
_STORE_LOCK = threading.RLock()


# ── store ────────────────────────────────────────────────────────────────────

def _blank_store() -> dict:
    """Return an empty user store with a freshly generated session signing secret."""
    return {"users": {}, "session_secret": secrets.token_hex(32)}


def _read_store_raw() -> dict:
    """Parse one complete store snapshot without repairing or writing it."""
    with open(USERS_FILE, "r") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("users.json is not an object")
    users = data.setdefault("users", {})
    if not isinstance(users, dict):
        raise ValueError("users.json users member is not an object")
    if any(not isinstance(username, str) or not isinstance(record, dict)
           for username, record in users.items()):
        raise ValueError("users.json contains an invalid user record")
    if "session_secret" in data and not isinstance(data["session_secret"], str):
        raise ValueError("users.json session secret is not text")
    return data


def _unreadable_store(error: Exception) -> dict:
    """Return a fail-closed marker without exposing or replacing file contents."""
    logger.error("auth: could not read %s (%s) — refusing user-store access.",
                 USERS_FILE, error)
    return {"users": {}, "session_secret": "", "_unreadable": True}


def _read_store() -> dict:
    """Load users.json and repair a missing secret only after a locked reread."""
    try:
        data = _read_store_raw()
    except FileNotFoundError:
        return _blank_store()
    except Exception as e:  # noqa: BLE001
        return _unreadable_store(e)
    if data.get("session_secret"):
        return data

    # Do not write the stale snapshot: another mutation may have rotated the
    # secret or changed users since this read. Re-read while holding the RMW lock.
    with _STORE_LOCK:
        try:
            data = _read_store_raw()
        except FileNotFoundError:
            return _blank_store()
        except Exception as e:  # noqa: BLE001
            return _unreadable_store(e)
        if not data.get("session_secret"):
            data["session_secret"] = secrets.token_hex(32)
            _write_store(data)
        return data


def _write_store(data: dict) -> None:
    """Atomically save with a unique, owner-only temporary file in the same directory."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(USERS_FILE)}.",
                               suffix=".tmp", dir=os.path.dirname(USERS_FILE) or ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, USERS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


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


def _create_local_user(username: str, password: str, first_only: bool,
                       actor_cookie: str | None = None, require_actor: bool = False
                       ) -> tuple[bool, str, str | None]:
    """Create a local account, optionally validating an actor or signing first setup."""
    # Usernames are trimmed and must not be empty.
    username = (username or "").strip()
    if not username:
        return False, "Username is required.", None
    # Enforce the password policy.
    err = validate_password(password)
    if err:
        return False, err, None
    # Hash the password with a fresh random 16-byte salt.
    salt = secrets.token_bytes(16)
    _digest, _algo, _iters = _hash(password, salt)
    with _STORE_LOCK:
        # Always merge into the latest snapshot; concurrent setup requests cannot
        # overwrite one another or silently replace an existing account.
        data = _read_store()
        if data.get("_unreadable"):
            return False, "User store is unreadable — fix or remove users.json.", None
        if require_actor and _verify_session_in_store(actor_cookie, data) is None:
            raise SessionExpiredError("Session expired; sign in again.")
        users = data.setdefault("users", {})
        if first_only and users:
            return False, "Setup already completed.", None
        if username in users:
            return False, f"User {username!r} already exists.", None
        users[username] = {
            "salt": salt.hex(), "hash": _digest, "algo": _algo, "iters": _iters,
            "created": datetime.now().isoformat(timespec="seconds"), "last_login": None,
        }
        try:
            _write_store(data)
        except OSError as e:
            logger.error("auth: could not create user %r (%s)", username, e)
            return False, "Could not save user account.", None
        token = (_sign_session(username, None, data["session_secret"].encode("utf-8"))
                 if first_only else None)
    logger.info("auth: created user %r", username)
    return True, f"User {username!r} created.", token


def create_user(username: str, password: str) -> tuple[bool, str]:
    """Create a new local password account; returns (ok, message)."""
    ok, message, _token = _create_local_user(username, password, first_only=False)
    return ok, message


def create_user_for_actor(username: str, password: str,
                          actor_cookie: str | None) -> tuple[bool, str]:
    """Create an account only if the original route cookie is current at commit time."""
    ok, message, _token = _create_local_user(
        username, password, first_only=False,
        actor_cookie=actor_cookie, require_actor=True)
    return ok, message


def create_first_user_and_issue_session(
        username: str, password: str) -> tuple[bool, str, str | None]:
    """Create and sign in the first account in one locked store transaction."""
    return _create_local_user(username, password, first_only=True)


def _upsert_external_user(username: str, provider: str, email: str, name: str,
                          oid: str, issue_token: bool
                          ) -> tuple[bool, str, str | None]:
    """Create (first SSO login) or refresh a federated account that has NO
    password — it can only ever sign in via its identity provider (e.g. Azure
    Entra). Because the record carries no ``salt``/``hash``, ``verify_credentials``
    will always reject a password login for it, so the two auth paths can never
    cross over. Returns ``(ok, message, token)``."""
    # Usernames are trimmed and must not be empty.
    username = (username or "").strip()
    if not username:
        return False, "External username is required.", None
    with _STORE_LOCK:
        data = _read_store()
        if data.get("_unreadable"):
            return False, "User store is unreadable — fix or remove users.json.", None
        users = data.setdefault("users", {})
        rec = users.get(username)
        now = datetime.now().isoformat(timespec="seconds")
        if isinstance(rec, dict):
            auth_type = rec.get("auth_type")
            if auth_type in (None, "", "local") and (rec.get("salt") or rec.get("hash")):
                return False, (f"User {username!r} is a local password account; "
                               "cannot convert it to an external account."), None
            if auth_type != provider:
                return (False, f"User {username!r} belongs to a different sign-in provider.",
                        None)
            # Repair legacy mixed records only for the already-bound provider.
            stripped = False
            for key in ("salt", "hash", "algo", "iters"):
                if key in rec:
                    rec.pop(key)
                    stripped = True
            rec.update({"email": email or rec.get("email", ""),
                        "name": name or rec.get("name", ""),
                        "oid": oid or rec.get("oid", ""), "last_login": now})
            if stripped:
                data["session_secret"] = secrets.token_hex(32)
        else:
            users[username] = {
                "auth_type": provider, "email": email, "name": name, "oid": oid,
                "created": now, "last_login": now,
            }
        try:
            _write_store(data)
        except OSError as e:
            logger.error("auth: could not provision external user %r (%s)", username, e)
            return False, "Could not save external user account.", None
        token = (_sign_session(username, None, data["session_secret"].encode("utf-8"))
                 if issue_token else None)
    logger.info("auth: %s user %r signed in", provider, username)
    return True, f"External user {username!r} provisioned.", token


def upsert_external_user(username: str, provider: str = "entra",
                         email: str = "", name: str = "",
                         oid: str = "") -> tuple[bool, str]:
    """Create or refresh an external account without changing the legacy result shape."""
    ok, message, _token = _upsert_external_user(
        username, provider, email, name, oid, issue_token=False)
    return ok, message


def upsert_external_user_and_issue_session(
        username: str, provider: str = "entra", email: str = "",
        name: str = "", oid: str = "") -> tuple[bool, str, str | None]:
    """Provision or refresh an external account and sign its session atomically."""
    return _upsert_external_user(username, provider, email, name, oid, issue_token=True)


class SessionExpiredError(RuntimeError):
    """Raised when a mutation's original request cookie is no longer current."""


def _set_password(username: str, password: str, actor_cookie: str | None,
                  ttl_s: int | None = None) -> tuple[bool, str, str | None]:
    """Update a password and optionally sign the verified request actor atomically."""
    # Enforce the password policy first.
    err = validate_password(password)
    if err:
        return False, err, None
    # Snapshot the current local credential before doing expensive hashing.
    with _STORE_LOCK:
        data = _read_store()
        rec = (data.get("users") or {}).get(username)
        if data.get("_unreadable"):
            return False, "User store is unreadable — fix or remove users.json.", None
        if not isinstance(rec, dict):
            return False, f"No such user {username!r}.", None
        if rec.get("auth_type") not in (None, "", "local"):
            return False, f"User {username!r} signs in through an external provider.", None
        original = tuple(rec.get(k) for k in ("auth_type", "salt", "hash", "algo", "iters"))
    # Re-hash outside the lock, then reject if reset/delete/recreate raced us.
    salt = secrets.token_bytes(16)
    _digest, _algo, _iters = _hash(password, salt)
    with _STORE_LOCK:
        data = _read_store()
        if data.get("_unreadable"):
            return False, "User store is unreadable — fix or remove users.json.", None
        # Validate the incoming cookie against this pre-mutation snapshot. The
        # actor's username is derived from that cookie, never trusted from middleware.
        actor = _verify_session_in_store(actor_cookie, data) if actor_cookie is not None else None
        if actor_cookie is not None and actor is None:
            raise SessionExpiredError("Session expired; sign in again.")
        rec = (data.get("users") or {}).get(username)
        if not isinstance(rec, dict) or rec.get("auth_type") not in (None, "", "local"):
            return False, f"User {username!r} changed while the password was being updated.", None
        current = tuple(rec.get(k) for k in ("auth_type", "salt", "hash", "algo", "iters"))
        if current != original:
            return False, f"User {username!r} changed while the password was being updated.", None
        rec.update({"salt": salt.hex(), "hash": _digest, "algo": _algo, "iters": _iters})
        # Secret rotation and password replacement are one atomic store snapshot.
        data["session_secret"] = secrets.token_hex(32)
        try:
            _write_store(data)
        except OSError as e:
            logger.error("auth: could not update password for %r (%s)", username, e)
            return False, "Could not save password.", None
        token = (_sign_session(actor, ttl_s, data["session_secret"].encode("utf-8"))
                 if actor is not None and actor in data["users"] else None)
    logger.info("auth: password changed for %r", username)
    return True, "Password updated.", token


def set_password(username: str, password: str) -> tuple[bool, str]:
    """Change an existing user's password; returns the compatible two-value result."""
    ok, message, _token = _set_password(username, password, actor_cookie=None)
    return ok, message


def set_password_and_issue_actor_session(
        username: str, password: str, actor_cookie: str,
        ttl_s: int | None = None) -> tuple[bool, str, str | None]:
    """Change a password and refresh only the actor proven by the original cookie."""
    return _set_password(username, password, actor_cookie, ttl_s)


def _delete_user(username: str, actor_cookie: str | None,
                 ttl_s: int | None = None) -> tuple[bool, str, str | None]:
    """Delete an account and optionally refresh a surviving, cookie-verified actor."""
    with _STORE_LOCK:
        data = _read_store()
        if data.get("_unreadable"):
            return False, "User store is unreadable — fix or remove users.json.", None
        actor = _verify_session_in_store(actor_cookie, data) if actor_cookie is not None else None
        if actor_cookie is not None and actor is None:
            raise SessionExpiredError("Session expired; sign in again.")
        users = data.get("users") or {}
        if actor is not None and username == actor:
            return False, "You cannot delete the account you are signed in as.", None
        if username not in users:
            return False, f"No such user {username!r}.", None
        if len(users) <= 1:
            return False, "Cannot delete the last remaining user.", None
        users.pop(username)
        # Deletion and global session invalidation are one atomic store write.
        data["session_secret"] = secrets.token_hex(32)
        try:
            _write_store(data)
        except OSError as e:
            logger.error("auth: could not delete user %r (%s)", username, e)
            return False, "Could not delete user.", None
        token = (_sign_session(actor, ttl_s, data["session_secret"].encode("utf-8"))
                 if actor is not None and actor in users else None)
    logger.info("auth: deleted user %r", username)
    return True, f"User {username!r} deleted.", token


def delete_user(username: str) -> tuple[bool, str]:
    """Delete a user; preserves the existing two-value public result."""
    ok, message, _token = _delete_user(username, actor_cookie=None)
    return ok, message


def delete_user_and_issue_actor_session(
        username: str, actor_cookie: str, ttl_s: int | None = None
        ) -> tuple[bool, str, str | None]:
    """Delete an account and return a token for the verified surviving actor."""
    return _delete_user(username, actor_cookie, ttl_s)


def _credential_fingerprint(rec: dict) -> tuple:
    """Return exactly the credential identity fields that must remain unchanged."""
    return tuple(rec.get(k) for k in ("salt", "hash", "algo", "iters", "auth_type"))


def _hash_matches(calculated: str, stored: object) -> bool:
    """Compare well-formed text hashes in constant time, rejecting malformed records."""
    if not isinstance(stored, str):
        return False
    try:
        return hmac.compare_digest(calculated, stored)
    except (TypeError, ValueError):
        return False


def _authenticate_credentials(username: str, password: str) -> tuple[bool, bytes | None]:
    """Verify a password snapshot and return its still-current signing key, if valid."""
    username = (username or "").strip()
    # Copy only the fields needed for hashing; all records sharing the username
    # must remain password-local, never a legacy external account with a hash.
    with _STORE_LOCK:
        data = _read_store()
        rec = (data.get("users") or {}).get(username)
        if (data.get("_unreadable") or not isinstance(rec, dict)
                or rec.get("auth_type") not in (None, "", "local")
                or not rec.get("salt") or not rec.get("hash")):
            snapshot = None
        else:
            snapshot = dict(rec)

    if snapshot is None:
        # Equalize missing/external-user paths with one dummy password hash.
        try:
            _hash(password or "", secrets.token_bytes(16))
        except Exception:
            pass
        return False, None

    try:
        calc, _, _ = _hash(password or "", bytes.fromhex(snapshot["salt"]),
                           algo=snapshot.get("algo"), iters=snapshot.get("iters"))
    except Exception as e:  # noqa: BLE001
        logger.error("auth: cannot verify password record for %r (%s)", username, e)
        return False, None
    if not _hash_matches(calc, snapshot["hash"]):
        return False, None

    with _STORE_LOCK:
        # Password reset/delete/recreate may have happened while hashing. Never
        # retry against the new record: the submitted password authenticated only
        # the snapshot above, not whatever now occupies this username.
        current = _read_store()
        rec = (current.get("users") or {}).get(username)
        if (current.get("_unreadable") or not isinstance(rec, dict)
                or rec.get("auth_type") not in (None, "", "local")
                or _credential_fingerprint(rec) != _credential_fingerprint(snapshot)
                or not _hash_matches(calc, rec.get("hash"))):
            return False, None
        rec["last_login"] = datetime.now().isoformat(timespec="seconds")
        try:
            _write_store(current)
        except Exception as e:  # noqa: BLE001 — a stamp failure must not block login
            logger.debug("auth: could not record last_login: %s", e)
        # Return the secret captured from the validated snapshot. If a reset wins
        # after this lock is released, it rotates the secret and invalidates token.
        return True, (current.get("session_secret") or "").encode("utf-8")


def verify_credentials(username: str, password: str) -> bool:
    """Return True only for a still-current local password credential."""
    valid, _secret_bytes = _authenticate_credentials(username, password)
    return valid


# ── sessions ─────────────────────────────────────────────────────────────────

def ensure_session_secret() -> bytes:
    """Return the persisted signing secret, creating + writing an (empty) user
    store on first use so the secret is STABLE across calls.

    ``_read_store`` mints a fresh random secret every time the file is absent
    (``_blank_store``) without persisting it, so two calls would disagree. Any
    signature that must survive a round-trip *before* the first account exists —
    notably the OIDC state cookie when SSO is the very first login — needs this
    to pin the secret to disk once."""
    with _STORE_LOCK:
        try:
            data = _read_store_raw()
        except FileNotFoundError:
            data = _blank_store()
            _write_store(data)
        except Exception as e:  # noqa: BLE001 — never replace a corrupt store
            _unreadable_store(e)
            raise RuntimeError("User store is unreadable; refusing to initialize its secret.")
        if not data.get("session_secret"):
            data["session_secret"] = secrets.token_hex(32)
            _write_store(data)
        return data["session_secret"].encode("utf-8")


def _sign_session(username: str, ttl_s: int | None, secret_bytes: bytes) -> str:
    """Sign one username/expiry payload using only the explicitly supplied key."""
    payload = json.dumps({"u": username,
                          "exp": int(time.time()) + int(ttl_s or SESSION_TTL_S)},
                         separators=(",", ":")).encode("utf-8")
    b = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    sig = hmac.new(secret_bytes, b.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b}.{sig}"


def issue_session(username: str, ttl_s: int | None = None) -> str:
    """Create a signed session token for username that expires after ttl_s seconds."""
    with _STORE_LOCK:
        data = _read_store()
        if data.get("_unreadable"):
            raise RuntimeError("User store is unreadable; refusing to sign a session.")
        secret_bytes = (data.get("session_secret") or "").encode("utf-8")
        return _sign_session(username, ttl_s, secret_bytes)


def authenticate_session(username: str, password: str,
                         ttl_s: int | None = None) -> str | None:
    """Return a token signed by the key snapshot that validated this password."""
    valid, secret_bytes = _authenticate_credentials(username, password)
    if not valid or secret_bytes is None:
        return None
    return _sign_session((username or "").strip(), ttl_s, secret_bytes)


def _verify_session_in_store(token: str, store: dict) -> str | None:
    """Validate an untrusted cookie against exactly one supplied store snapshot."""
    # Validate the untrusted cookie shape before decoding or HMAC operations.
    if (not isinstance(token, str) or token.count(".") != 1
            or not token.isascii() or store.get("_unreadable")):
        return None
    b, sig = token.split(".", 1)
    if (not b or len(sig) != 64 or any(c not in "0123456789abcdef" for c in sig)
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                   for c in b)):
        return None
    # Authenticate the envelope before parsing an attacker-controlled JSON payload.
    secret_bytes = (store.get("session_secret") or "").encode("utf-8")
    expect = hmac.new(secret_bytes, b.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        raw = base64.b64decode(b + "=" * (-len(b) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if (not isinstance(data, dict) or not isinstance(data.get("u"), str)
            or not data["u"] or isinstance(data.get("exp"), bool)
            or not isinstance(data.get("exp"), int) or data["exp"] <= time.time()):
        return None
    return data["u"] if data["u"] in (store.get("users") or {}) else None


def verify_session(token: str) -> str | None:
    """Return the username for a valid, unexpired token, else None."""
    # Verify user membership and key against the same atomic file snapshot.
    return _verify_session_in_store(token, _read_store())


def rotate_session_secret() -> None:
    """Invalidate every outstanding session (e.g. after removing a user)."""
    with _STORE_LOCK:
        data = _read_store()
        if data.get("_unreadable"):
            raise RuntimeError("User store is unreadable; refusing to rotate its secret.")
        data["session_secret"] = secrets.token_hex(32)
        _write_store(data)
    logger.warning("auth: session secret rotated — all users must log in again.")

"""Loopback-only, token-gated, read-only admin ops API (``/admin/ops/*``).

Lets an operator or automation on the AB VM pull diagnostic data with
``curl https://127.0.0.1/admin/ops/...`` without a browser session. Modelled on
LM's ``core/src/routes/admin_ops.py``. Two independent gates guard every route:

1. The TCP peer must be loopback (127.0.0.1 / ::1 / ::ffff:127.0.0.1). A caller
   from anywhere else gets a bare 404, so the route is not revealed. Proxy
   headers (X-Forwarded-*) are deliberately ignored: a remote caller could forge
   them.
2. The ``X-AB-Admin-Token`` header must match a root-minted 0600 token, compared
   with ``secrets.compare_digest``. A wrong or missing token gets 403.

The token comes from ``AB_ADMIN_OPS_TOKEN`` when set, else from
``/etc/ab/admin_ops_token`` (``./admin_ops_token`` when /etc/ab is not
writable, as config_store does). The token value is never logged. Every string
returned is redacted (identities and secret-looking text).
"""
import asyncio
import os
import re
import secrets
import stat
import threading
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from main import logger

router = APIRouter()

# Peers accepted as "this box".
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# Header carrying the token, and the env var that overrides the token file.
_TOKEN_HEADER = "x-ab-admin-token"
_TOKEN_ENV = "AB_ADMIN_OPS_TOKEN"
_TOKEN_FILE_NAME = "admin_ops_token"
# Record statuses that mean a person has to look at the issue.
_NEEDS_YOU = {"failed", "non-actionable", "ops-alert", "awaiting_human", "triaged", "awaiting_review"}
_NEEDS_YOU_MAX = 200
# Log tail limits: default/max lines returned, and how much of the file end is read.
_LOG_LINES_DEFAULT, _LOG_LINES_MAX = 200, 2000
_LOG_CHUNK = 4 * 1024 * 1024
# The leading timestamp of an AB log line.
_LOG_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

_token_lock = threading.Lock()
_token_cache = None


def _token_dirs():
    """Directories to try for the token file, persistent location first."""
    from config_store import CONFIG_DIR
    return [CONFIG_DIR, os.getcwd()]


def _write_token(path, token):
    """Write the token to ``path`` created 0600 (never wider, even under a lax umask)."""
    # Open with the restrictive mode from the start, then chmod in case a prior file was wider.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def load_or_mint_token():
    """Return the admin-ops token: env override, else the token file, else mint one.

    A new token is 32 random bytes, written 0600 to the first writable directory.
    Only the path is logged, never the value."""
    global _token_cache
    with _token_lock:
        # An explicit env token always wins.
        env = os.environ.get(_TOKEN_ENV, "").strip()
        if env:
            return env
        if _token_cache:
            return _token_cache
        # Reuse a token file that already exists and is non-empty.
        for directory in _token_dirs():
            try:
                existing = os.path.join(directory, _TOKEN_FILE_NAME)
                # A token file others can read is no secret: tighten it, or skip it if we cannot.
                if os.stat(existing).st_mode & 0o077:
                    os.chmod(existing, stat.S_IRUSR | stat.S_IWUSR)
                with open(existing) as fh:
                    tok = fh.read().strip()
                if tok:
                    _token_cache = tok
                    return tok
            except OSError:
                continue
        # Otherwise mint one in the first directory we can write.
        tok = secrets.token_urlsafe(32)
        for directory in _token_dirs():
            path = os.path.join(directory, _TOKEN_FILE_NAME)
            try:
                os.makedirs(directory, exist_ok=True)
                _write_token(path, tok)
                logger.warning("admin_ops: minted admin-ops token at %s (0600)", path)
                break
            except OSError:
                continue
        else:
            logger.error("admin_ops: could not persist the admin-ops token; keeping it in memory")
        _token_cache = tok
        return tok


def _guard(request: Request):
    """Both gates: loopback peer (else 404) AND a matching token (else 403)."""
    # Gate 1: the real TCP peer must be this box. 404 so the route is not revealed.
    host = request.client.host if request.client else None
    if host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=404, detail="Not Found")
    # Gate 2: constant-time token compare.
    supplied = request.headers.get(_TOKEN_HEADER, "")
    if not supplied or not secrets.compare_digest(supplied.encode(), load_or_mint_token().encode()):
        raise HTTPException(status_code=403, detail="invalid admin token")


# Fail closed: from any secret word to the end of the line is masked, whatever the separator or
# quoting ("password is 'a b c'", NAME=x, Bearer x), because patterns cannot find a spaced secret's end.
_SECRET_NAME_RE = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:password|passwd|secret|token|api[_ -]?key|credential|authorization"
    r"|bearer|cookie|key|akia)[A-Za-z0-9_.-]*).*")


def _redact(value):
    """Redact identities and secret-looking text in a string (non-strings pass through)."""
    if not isinstance(value, str):
        return value
    import github_ops
    # Lazy import: fix_engine imports a lot and would be circular at module load.
    from fix_engine import _CREDENTIAL_RE
    # Mask from the secret word to end of line first, then the token shapes _CREDENTIAL_RE knows.
    value = _SECRET_NAME_RE.sub(r"\1 [redacted]", github_ops.redact_identity(value))
    return _CREDENTIAL_RE.sub("[redacted]", value)


def _read_log_tail(path, grep, since):
    """Return the lines of the last _LOG_CHUNK bytes of ``path`` matching the filters."""
    with open(path, "rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - _LOG_CHUNK)
        fh.seek(start)
        text = fh.read().decode("utf-8", "replace")
    lines = text.splitlines()
    # A mid-file start cuts the first line in half: drop it.
    if start and lines:
        lines = lines[1:]
    # since: keep timestamped lines at/after it; continuation lines follow their parent.
    if since:
        kept, keep = [], True
        for line in lines:
            if _LOG_TS_RE.match(line):
                keep = line[:19] >= since
            if keep:
                kept.append(line)
        lines = kept
    # grep is a LITERAL, case-insensitive substring (never a regex).
    if grep:
        needle = grep.lower()
        lines = [ln for ln in lines if needle in ln.lower()]
    return lines


@router.get("/admin/ops/ping")
async def ops_ping(request: Request):
    """Liveness and auth check: both gates passed."""
    _guard(request)
    return {"ok": True, "time": datetime.now().isoformat()}


@router.get("/admin/ops/state")
async def ops_state(request: Request):
    """Processed-issue counts by status plus the records that need a person."""
    _guard(request)
    from config_store import load_processed
    processed = await asyncio.to_thread(load_processed)
    records = [(k, r) for k, r in processed.items() if isinstance(r, dict)]
    counts = Counter(str(r.get("status") or "unknown") for _, r in records)
    needs_you = []
    for key, rec in records:
        if rec.get("status") not in _NEEDS_YOU:
            continue
        reason = rec.get("reason") or rec.get("error") or rec.get("last_error") or ""
        needs_you.append({"id": _redact(str(key)), "status": rec.get("status"),
                          "title": _redact(str(rec.get("title") or "")),
                          "reason": _redact(str(reason)[:200]),
                          "timestamp": rec.get("timestamp")})
    return {"counts": dict(counts), "total": len(records),
            "needs_you_total": len(needs_you), "needs_you": needs_you[:_NEEDS_YOU_MAX]}


@router.get("/admin/ops/mission-control")
async def ops_mission_control(request: Request):
    """The same data as GET /api/mission-control, without a session."""
    _guard(request)
    # Lazy import: routes imports main, which includes this router.
    import routes
    data = await asyncio.to_thread(routes._mission)
    return _redact_tree(data)


def _redact_tree(value):
    """Redact every string inside nested dicts/lists."""
    if isinstance(value, dict):
        return {k: _redact_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_tree(v) for v in value]
    return _redact(value)


@router.get("/admin/ops/log")
async def ops_log(request: Request, lines: int = _LOG_LINES_DEFAULT, grep: str = "", since: str = ""):
    """Tail of the AB log: ``lines`` (max 2000), literal ``grep``, optional ``since`` ISO time."""
    _guard(request)
    lines = max(1, min(lines, _LOG_LINES_MAX))
    # Normalise since to the log's 'YYYY-MM-DD HH:MM:SS' prefix so a string compare works.
    since_ts = ""
    if since:
        try:
            since_ts = datetime.fromisoformat(since).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(status_code=400, detail="since must be an ISO timestamp")
    from main import get_log_path
    path = get_log_path()
    try:
        found = await asyncio.to_thread(_read_log_tail, path, grep, since_ts)
    except OSError:
        found = []
    return {"lines": [_redact(ln) for ln in found[-lines:]]}


@router.get("/admin/ops/llm")
async def ops_llm(request: Request):
    """LLM health: per-endpoint reachability, the any-online rollup and the legacy slots."""
    _guard(request)
    from app_state import state
    out = {"llm_endpoints_online": _redact_tree(state.get("llm_endpoints_online", [])),
           "any_llm_online": bool(state.get("any_llm_online"))}
    for n in range(1, 5):
        out["provider_%d_online" % n] = bool(state.get("provider_%d_online" % n))
    return out

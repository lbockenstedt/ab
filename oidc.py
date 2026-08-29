"""Azure Entra ID (OIDC) login provider for the AppBuilder WebUI.

Adapted from the LM hub's ``security/oidc.py``. Implements the Authorization
Code flow with PKCE against Microsoft Entra ID (Azure AD) and lets Entra users
sign in alongside the local password accounts in :mod:`auth`.

Two ways to authenticate this app to Entra are supported (pick one in the
Entra app registration → *Certificates & secrets*):

* **Client secret** — set ``client_secret`` in the OIDC config. Simplest.
* **Certificate** (confidential client, no secret) — set ``key_path`` /
  ``cert_path``; a RS256 ``client_assertion`` JWT is signed by the cert key.

The id-token is verified against the Entra JWKS (signature, issuer, audience,
nonce). MFA is enforced by an Entra Conditional Access policy, not in-token —
Entra omits ``amr`` when MFA is satisfied from an existing session, so an
in-token check would falsely reject valid logins.

On success the Entra user is provisioned into the local user store as an
``entra`` account (no password — they can only ever SSO in) and a normal
AppBuilder session cookie is issued, so the rest of the app is unchanged.

Config lives in ``config.json`` under the ``oidc`` key (managed from the WebUI
Settings), with ``AB_OIDC_*`` environment overrides taking precedence. This
module holds no secrets at rest beyond what the operator stored in the config.
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
from urllib.parse import quote as _url_quote

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

import auth as _auth

logger = logging.getLogger("AppBuilder")

_DEFAULT_SCOPE = "openid profile email offline_access"
_STATE_TTL_S = 300  # the OIDC round-trip must complete within 5 minutes
STATE_COOKIE = "ab_oidc_state"


class OidcError(Exception):
    """Raised for any OIDC-flow failure the callback should surface as 4xx.

    The message is safe to return to the browser (no secret material); the log
    gets the full context via ``logger.exception`` at the call site."""


# ── config ──────────────────────────────────────────────────────────────────

def _config_store() -> tuple:
    """Return ``(load_config, save_config)`` from config_store, importable in
    isolation (the recovery/test path may not have config_store wired)."""
    from config_store import load_config, save_config
    return load_config, save_config


def _bool_env(val, default: bool) -> bool:
    if val is None:
        return bool(default)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class OidcConfig:
    """Resolved OIDC configuration (``config["oidc"]`` + ``AB_OIDC_*`` env).

    Env overrides win over stored config so an operator can re-point the app at
    a different tenant without the WebUI. ``ready`` requires ``tenant_id`` +
    ``client_id`` + one credential (secret OR key_path)."""

    def __init__(self, stored: dict | None = None):
        stored = stored or {}
        env = os.environ
        self.tenant_id = (env.get("AB_OIDC_TENANT_ID") or stored.get("tenant_id") or "").strip()
        self.client_id = (env.get("AB_OIDC_CLIENT_ID") or stored.get("client_id") or "").strip()
        self.client_secret = (env.get("AB_OIDC_CLIENT_SECRET") or stored.get("client_secret") or "").strip()
        self.redirect_uri = (env.get("AB_OIDC_REDIRECT_URI") or stored.get("redirect_uri") or "").strip()
        self.cert_path = (env.get("AB_OIDC_CLIENT_CERT") or stored.get("cert_path") or "").strip()
        self.key_path = (env.get("AB_OIDC_CLIENT_KEY") or stored.get("key_path") or "").strip()
        self.allowed_group = (env.get("AB_OIDC_ALLOWED_GROUP") or stored.get("allowed_group") or "").strip()
        self.enabled = _bool_env(env.get("AB_OIDC_ENABLED"), stored.get("enabled", False))

    @property
    def has_credential(self) -> bool:
        return bool(self.client_secret or self.key_path)

    @property
    def ready(self) -> bool:
        """True when enough is configured to attempt a login."""
        return bool(self.tenant_id and self.client_id and self.redirect_uri and self.has_credential)

    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    def discovery_url(self) -> str:
        return (f"https://login.microsoftonline.com/{self.tenant_id}"
                "/v2.0/.well-known/openid-configuration")


def get_oidc_config() -> OidcConfig:
    """Load the stored OIDC config from ``config.json`` and layer env on top."""
    try:
        load_config, _ = _config_store()
        stored = (load_config() or {}).get("oidc", {}) or {}
    except Exception:  # noqa: BLE001 — env-only / test path
        stored = {}
    return OidcConfig(stored)


def save_oidc_config(patch: dict) -> dict:
    """Persist the operator-editable OIDC fields into ``config["oidc"]``.

    ``client_secret`` is only overwritten when a non-empty value is supplied, so
    saving other fields from the WebUI never wipes a stored secret. Returns the
    stored (redacted) config."""
    load_config, save_config = _config_store()
    config = load_config() or {}
    oidc = dict(config.get("oidc", {}) or {})
    for key in ("tenant_id", "client_id", "redirect_uri", "allowed_group",
                "cert_path", "key_path"):
        if key in patch:
            oidc[key] = str(patch.get(key) or "").strip()
    if "enabled" in patch:
        oidc["enabled"] = bool(patch.get("enabled"))
    # Secret: blank input keeps the existing one; a sentinel clears it.
    if "client_secret" in patch:
        sec = str(patch.get("client_secret") or "")
        if sec == "__CLEAR__":
            oidc["client_secret"] = ""
        elif sec.strip():
            oidc["client_secret"] = sec.strip()
    config["oidc"] = oidc
    save_config(config)
    return redact_config(oidc)


def redact_config(oidc: dict) -> dict:
    """A copy of the stored OIDC config safe to return to the WebUI — the secret
    is replaced by a ``configured`` boolean, never the value."""
    out = {k: oidc.get(k) for k in ("enabled", "tenant_id", "client_id",
                                    "redirect_uri", "allowed_group",
                                    "cert_path", "key_path")}
    out["client_secret_configured"] = bool(oidc.get("client_secret"))
    return out


# ── state cookie (PKCE + nonce carrier) ─────────────────────────────────────

def _state_secret() -> bytes:
    """HMAC key for the state cookie. Prefers ``AB_OIDC_STATE_SECRET``; else the
    session-signing secret already persisted by :mod:`auth` (rotates with it)."""
    sec = os.environ.get("AB_OIDC_STATE_SECRET", "").strip()
    if sec:
        return sec.encode()
    s = _auth.ensure_session_secret()
    if s:
        return s
    logger.warning("OIDC state cookie has no secret — using weak fallback")
    return b"ab-oidc-state-weak-fallback"


def sign_state_cookie(state: str, nonce: str, code_verifier: str) -> str:
    """Build the state cookie value: ``state:nonce:verifier:ts`` + HMAC."""
    ts = int(time.time())
    payload = f"{state}:{nonce}:{code_verifier}:{ts}"
    sig = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_state_cookie(cookie: str) -> tuple | None:
    """Return ``(state, nonce, code_verifier)`` if the cookie's HMAC is valid and
    fresh (within ``_STATE_TTL_S``); else ``None``."""
    if not cookie or "." not in cookie:
        return None
    payload, _, sig = cookie.rpartition(".")
    expected = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    parts = payload.split(":")
    if len(parts) != 4:
        return None
    state, nonce, verifier, ts_s = parts
    try:
        ts = int(ts_s)
    except ValueError:
        return None
    if abs(time.time() - ts) > _STATE_TTL_S:
        return None
    return state, nonce, verifier


# ── PKCE helpers ────────────────────────────────────────────────────────────

def new_pkce() -> tuple:
    """Return ``(state, nonce, code_verifier, code_challenge)``."""
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return state, nonce, code_verifier, code_challenge


def hmac_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a or ""), str(b or ""))


# ── discovery + authorize URL ───────────────────────────────────────────────

_discovery_cache: dict = {}  # tenant_id -> (fetched_at, doc)


async def discover(cfg: OidcConfig, http: httpx.AsyncClient | None = None) -> dict:
    """Fetch + cache the OIDC discovery doc (5 min TTL, so JWKS rotation is
    picked up without a restart)."""
    now = time.time()
    cached = _discovery_cache.get(cfg.tenant_id)
    if cached and now - cached[0] < 300:
        return cached[1]
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.get(cfg.discovery_url())
        resp.raise_for_status()
        doc = resp.json()
    _discovery_cache[cfg.tenant_id] = (now, doc)
    return doc


def authorize_url(cfg: OidcConfig, discovery_doc: dict,
                  state: str, nonce: str, code_challenge: str) -> str:
    """Build the Entra authorize URL (Authorization Code + PKCE)."""
    endpoint = discovery_doc.get("authorization_endpoint") or \
        f"https://login.microsoftonline.com/{cfg.tenant_id}/oauth2/v2.0/authorize"
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": _DEFAULT_SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return endpoint + "?" + "&".join(f"{k}={_url_quote(str(v), safe='')}"
                                     for k, v in params.items())


# ── client assertion (cert-based confidential client) ───────────────────────

def cert_thumbprint_x5t(cert_pem: bytes) -> str:
    """``x5t`` header Entra requires: base64url( SHA-1( DER(cert) ) )."""
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return base64.urlsafe_b64encode(hashlib.sha1(der).digest()).rstrip(b"=").decode()


def build_client_assertion(cfg: OidcConfig, token_endpoint: str) -> str:
    """Build a RS256 JWT ``client_assertion`` signed by the cert's private key."""
    if not cfg.key_path or not os.path.exists(cfg.key_path):
        raise OidcError(f"OIDC client key not found at {cfg.key_path!r}")
    if not cfg.cert_path or not os.path.exists(cfg.cert_path):
        raise OidcError(f"OIDC client certificate not found at {cfg.cert_path!r} "
                        "(needed for the Entra x5t header)")
    with open(cfg.key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), None)
    with open(cfg.cert_path, "rb") as f:
        cert_pem = f.read()
    now = int(time.time())
    payload = {
        "iss": cfg.client_id,
        "sub": cfg.client_id,
        "aud": token_endpoint,
        "jti": secrets.token_urlsafe(16),
        "iat": now,
        "exp": now + 300,
        "nbf": now,
    }
    headers = {"x5t": cert_thumbprint_x5t(cert_pem)}
    return jwt.encode(payload, key, algorithm="RS256", headers=headers)


# ── code exchange ───────────────────────────────────────────────────────────

def _entra_error_detail(resp) -> str:
    """Extract Entra's ``error_description`` (AADSTS code) from a failed token
    response so the callback surfaces WHY. Safe to show (no secret)."""
    try:
        j = resp.json()
        detail = j.get("error_description") or j.get("error") or ""
        detail = str(detail).replace("\r", "\n").split("\n")[0].strip()[:300]
        return f" — {detail}" if detail else ""
    except Exception:  # noqa: BLE001
        txt = (getattr(resp, "text", "") or "")[:200].strip()
        return f" — {txt}" if txt else ""


async def exchange_code(cfg: OidcConfig, discovery_doc: dict,
                        code: str, code_verifier: str,
                        http: httpx.AsyncClient | None = None) -> dict:
    """Exchange an authorization code for tokens, authenticating this app to
    Entra with the client secret OR a cert-signed ``client_assertion``."""
    token_endpoint = discovery_doc.get("token_endpoint") or \
        f"https://login.microsoftonline.com/{cfg.tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": code_verifier,
        "scope": _DEFAULT_SCOPE,
    }
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    else:
        data["client_assertion_type"] = \
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        data["client_assertion"] = build_client_assertion(cfg, token_endpoint)
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.post(token_endpoint, data=data)
    if resp.status_code != 200:
        raise OidcError(f"token exchange failed: HTTP {resp.status_code}"
                        f"{_entra_error_detail(resp)}")
    return resp.json()


# ── directory reads (app-only / client-credentials) ─────────────────────────

async def fetch_app_token(cfg: OidcConfig, scope: str,
                          http: httpx.AsyncClient | None = None) -> str:
    """Mint an app-only (client-credentials) token for AppBuilder's own app
    registration, for any resource the app holds a permission on.

    Unlike the LM hub's equivalent this accepts EITHER credential type, because
    AppBuilder supports a client secret as well as a cert ``client_assertion``
    — refusing the secret here would make the group picker unavailable on a
    secret-configured install that otherwise logs in fine."""
    token_endpoint = (f"https://login.microsoftonline.com/{cfg.tenant_id}"
                      "/oauth2/v2.0/token")
    data = {
        "grant_type": "client_credentials",
        "client_id": cfg.client_id,
        "scope": scope,
    }
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    else:
        data["client_assertion_type"] = \
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        data["client_assertion"] = build_client_assertion(cfg, token_endpoint)
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.post(token_endpoint, data=data)
    if resp.status_code != 200:
        raise OidcError(f"app-token failed ({scope}): HTTP {resp.status_code}"
                        f"{_entra_error_detail(resp)}")
    tok = resp.json().get("access_token")
    if not tok:
        raise OidcError("app-token response had no access_token")
    return tok


async def fetch_directory_groups(cfg: OidcConfig,
                                 http: httpx.AsyncClient | None = None,
                                 limit: int = 2000) -> list:
    """List the tenant's Entra groups as ``[{id, displayName}]`` so the admin
    can PICK the allowed group instead of pasting its object ID.

    The object IDs returned here are exactly what ``allowed_group`` matches at
    login, which is the point: a group *name* silently matches nothing, so the
    picker is what stops an admin from locking everyone out. Needs the Graph
    ``Group.Read.All`` **application** permission with admin consent. Pages
    ``@odata.nextLink`` up to ``limit``."""
    token = await fetch_app_token(cfg, "https://graph.microsoft.com/.default",
                                  http=http)
    out: list = []
    url = ("https://graph.microsoft.com/v1.0/groups"
           "?$select=id,displayName&$top=999")
    async with (http or httpx.AsyncClient(timeout=20.0)) as client:
        while url:
            resp = await client.get(url,
                                    headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                raise OidcError(f"Graph groups list failed: HTTP "
                                f"{resp.status_code} — {resp.text[:200]}")
            body = resp.json()
            for v in body.get("value", []):
                gid = v.get("id")
                if gid:
                    out.append({"id": gid,
                                "displayName": v.get("displayName") or gid})
                    if len(out) >= limit:
                        return out
            url = body.get("@odata.nextLink")
    return out


# ── id-token verification ───────────────────────────────────────────────────

async def fetch_jwks(jwks_uri: str, http: httpx.AsyncClient | None = None) -> list:
    """Fetch the JWKS signing keys."""
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        return resp.json().get("keys", [])


def _jwk_to_key(jwk: dict):
    kty = jwk.get("kty")
    if kty == "RSA":
        return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    if kty == "EC":
        return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    raise OidcError(f"unsupported JWKS kty: {kty!r}")


def verify_id_token(cfg: OidcConfig, id_token: str, nonce: str,
                    jwks_keys: list) -> dict:
    """Verify an Entra id-token (signature/issuer/audience/nonce) and return the
    decoded claims. MFA is enforced by Entra Conditional Access, not here."""
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise OidcError(f"malformed id_token: {e}") from e
    kid = unverified_header.get("kid")
    keys = [_jwk_to_key(k) for k in jwks_keys if k.get("kid") == kid] or \
           [_jwk_to_key(k) for k in jwks_keys]
    if not keys:
        raise OidcError("no matching JWKS key for id_token kid")
    last_err: Exception | None = None
    claims = None
    for key in keys:
        try:
            claims = jwt.decode(
                id_token, key=key, algorithms=["RS256"],
                audience=cfg.client_id, issuer=cfg.issuer(),
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            break
        except jwt.PyJWTError as e:
            last_err = e
    if claims is None:
        raise OidcError(f"id_token verification failed: {last_err}")
    if claims.get("nonce") != nonce:
        raise OidcError("nonce mismatch — id_token replay suspected")
    return claims


def extract_member_groups(claims: dict) -> list:
    """The Entra ``groups`` claim (group object IDs), when present."""
    g = claims.get("groups")
    if isinstance(g, list):
        return [str(x) for x in g]
    return []


def enforce_allowed_group(allowed_group: str, member_of: list) -> None:
    """If ``allowed_group`` is set, refuse a user who is not in any of the
    listed group object IDs. ``allowed_group`` may be a comma/space-separated
    list; membership in ANY grants login."""
    import re as _re
    allowed = [g for g in _re.split(r"[\s,]+", str(allowed_group or "")) if g.strip()]
    if not allowed:
        return
    if not (set(member_of or []) & set(allowed)):
        if not member_of:
            raise OidcError(
                "Access denied: this app read 0 groups for you (the id_token had "
                "no groups claim). Add a groups claim in the Entra app's Token "
                "Configuration, or clear the allowed-group restriction.")
        raise OidcError(
            "Access denied: you are not a member of an allowed group. Each "
            "allowed-group entry must be the group's OBJECT ID (a UUID).")


# ── provisioning ────────────────────────────────────────────────────────────

def provision_entra_user(claims: dict) -> str:
    """Provision (first login) or refresh an Entra user in the local user store
    and return the username to open a session for. The username is the user's
    email / UPN when available (stable + human-readable), else the ``oid``."""
    oid = str(claims.get("oid") or "").strip()
    email = str(claims.get("email") or claims.get("preferred_username") or "").strip()
    name = str(claims.get("name") or "").strip()
    if not oid and not email:
        raise OidcError("id_token missing both oid and email claims")
    username = email or oid
    _auth.upsert_external_user(username, provider="entra", email=email,
                               name=name, oid=oid)
    return username

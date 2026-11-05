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
# Allows "dict | None" style type hints on older-style annotations.
from __future__ import annotations

# base64 encodes the PKCE challenge and the certificate thumbprint in URL-safe form.
import base64
# hashlib provides SHA-256 (PKCE, cookie HMAC) and SHA-1 (Entra's x5t cert thumbprint).
import hashlib
# hmac signs and verifies the state cookie with constant-time comparison.
import hmac
# json serialises JWKS keys for PyJWT's key loaders.
import json
# logging reports configuration problems.
import logging
# os reads AB_OIDC_* environment overrides and checks that key/cert files exist.
import os
# secrets generates unpredictable state, nonce, verifier and JWT ids.
import secrets
# time stamps the state cookie and JWTs and drives the discovery cache TTL.
import time
# URL-encodes the authorize URL's query parameters.
from urllib.parse import quote as _url_quote

# Async HTTP client used to call Entra and Microsoft Graph.
import httpx
# PyJWT: verifies id-tokens and signs the certificate client assertion.
import jwt
# Loads PEM keys/certificates and converts certificates to DER.
from cryptography.hazmat.primitives import serialization

# Local user store and session secret (Entra users are provisioned into it).
import auth as _auth

# Share AppBuilder's main logger so OIDC messages appear with the rest of the app.
logger = logging.getLogger("AppBuilder")

# Scopes requested from Entra: identity (openid/profile/email) plus a refresh token.
_DEFAULT_SCOPE = "openid profile email offline_access"
_STATE_TTL_S = 300  # the OIDC round-trip must complete within 5 minutes
# Name of the browser cookie that carries the signed state/nonce/PKCE verifier.
STATE_COOKIE = "ab_oidc_state"


class OidcError(Exception):
    """Raised for any OIDC-flow failure the callback should surface as 4xx.

    The message is safe to return to the browser (no secret material); the log
    gets the full context via ``logger.exception`` at the call site."""


# ── config ──────────────────────────────────────────────────────────────────

def _config_store() -> tuple:
    """Return ``(load_config, save_config)`` from config_store, importable in
    isolation (the recovery/test path may not have config_store wired)."""
    # Imported lazily so this module can load even where config_store is unavailable.
    from config_store import load_config, save_config
    return load_config, save_config


def _bool_env(val, default: bool) -> bool:
    """Parse an environment flag ("1/true/yes/on") as a bool, or use ``default`` when unset."""
    # Unset variable: fall back to the stored setting.
    if val is None:
        return bool(default)
    # Any of the common "true" spellings enables the flag; everything else disables it.
    return str(val).strip().lower() in ("1", "true", "yes", "on")


class OidcConfig:
    """Resolved OIDC configuration (``config["oidc"]`` + ``AB_OIDC_*`` env).

    Env overrides win over stored config so an operator can re-point the app at
    a different tenant without the WebUI. ``ready`` requires ``tenant_id`` +
    ``client_id`` + one credential (secret OR key_path)."""

    def __init__(self, stored: dict | None = None):
        """Resolve every OIDC setting, preferring AB_OIDC_* env vars over the stored config."""
        # Treat a missing stored config as empty and read the process environment.
        stored = stored or {}
        env = os.environ
        # Each setting: environment variable first, then stored config, else empty.
        self.tenant_id = (env.get("AB_OIDC_TENANT_ID") or stored.get("tenant_id") or "").strip()
        self.client_id = (env.get("AB_OIDC_CLIENT_ID") or stored.get("client_id") or "").strip()
        self.client_secret = (env.get("AB_OIDC_CLIENT_SECRET") or stored.get("client_secret") or "").strip()
        self.redirect_uri = (env.get("AB_OIDC_REDIRECT_URI") or stored.get("redirect_uri") or "").strip()
        self.cert_path = (env.get("AB_OIDC_CLIENT_CERT") or stored.get("cert_path") or "").strip()
        self.key_path = (env.get("AB_OIDC_CLIENT_KEY") or stored.get("key_path") or "").strip()
        self.allowed_group = (env.get("AB_OIDC_ALLOWED_GROUP") or stored.get("allowed_group") or "").strip()
        # Enabled flag: env var if set, otherwise the stored value (off by default).
        self.enabled = _bool_env(env.get("AB_OIDC_ENABLED"), stored.get("enabled", False))

    @property
    def has_credential(self) -> bool:
        """True when a client secret or a certificate key is configured."""
        return bool(self.client_secret or self.key_path)

    @property
    def ready(self) -> bool:
        """True when enough is configured to attempt a login."""
        return bool(self.tenant_id and self.client_id and self.redirect_uri and self.has_credential)

    def issuer(self) -> str:
        """Return the Entra v2.0 issuer URL that id-tokens for this tenant must carry."""
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    def discovery_url(self) -> str:
        """Return the tenant's OpenID Connect discovery document URL."""
        return (f"https://login.microsoftonline.com/{self.tenant_id}"
                "/v2.0/.well-known/openid-configuration")


def get_oidc_config() -> OidcConfig:
    """Load the stored OIDC config from ``config.json`` and layer env on top."""
    # Read config.json's "oidc" section; missing config_store or bad config means "nothing stored".
    try:
        load_config, _ = _config_store()
        stored = (load_config() or {}).get("oidc", {}) or {}
    except Exception:  # noqa: BLE001 — env-only / test path
        stored = {}
    # Combine the stored values with any environment overrides.
    return OidcConfig(stored)


def save_oidc_config(patch: dict) -> dict:
    """Persist the operator-editable OIDC fields into ``config["oidc"]``.

    ``client_secret`` is only overwritten when a non-empty value is supplied, so
    saving other fields from the WebUI never wipes a stored secret. Returns the
    stored (redacted) config."""
    # Load the full config and copy the current OIDC section so it can be edited.
    load_config, save_config = _config_store()
    config = load_config() or {}
    oidc = dict(config.get("oidc", {}) or {})
    # Copy each plain field the caller supplied, normalised to a trimmed string.
    for key in ("tenant_id", "client_id", "redirect_uri", "allowed_group",
                "cert_path", "key_path"):
        if key in patch:
            oidc[key] = str(patch.get(key) or "").strip()
    # Update the enabled flag only when the caller sent it.
    if "enabled" in patch:
        oidc["enabled"] = bool(patch.get("enabled"))
    # Secret: blank input keeps the existing one; a sentinel clears it.
    if "client_secret" in patch:
        sec = str(patch.get("client_secret") or "")
        if sec == "__CLEAR__":
            oidc["client_secret"] = ""
        elif sec.strip():
            oidc["client_secret"] = sec.strip()
    # Store the updated section and write config.json back to disk.
    config["oidc"] = oidc
    save_config(config)
    # Return the saved values with the secret hidden.
    return redact_config(oidc)


def redact_config(oidc: dict) -> dict:
    """A copy of the stored OIDC config safe to return to the WebUI — the secret
    is replaced by a ``configured`` boolean, never the value."""
    # Copy the non-secret fields only.
    out = {k: oidc.get(k) for k in ("enabled", "tenant_id", "client_id",
                                    "redirect_uri", "allowed_group",
                                    "cert_path", "key_path")}
    # Report whether a secret exists without revealing it.
    out["client_secret_configured"] = bool(oidc.get("client_secret"))
    return out


# ── state cookie (PKCE + nonce carrier) ─────────────────────────────────────

def _state_secret() -> bytes:
    """HMAC key for the state cookie. Prefers ``AB_OIDC_STATE_SECRET``; else the
    session-signing secret already persisted by :mod:`auth` (rotates with it)."""
    # A dedicated state secret from the environment wins.
    sec = os.environ.get("AB_OIDC_STATE_SECRET", "").strip()
    if sec:
        return sec.encode()
    # Otherwise reuse the persisted session-signing secret.
    s = _auth.ensure_session_secret()
    if s:
        return s
    # No secret available at all: warn loudly and use a fixed (weak) key so login still works.
    logger.warning("OIDC state cookie has no secret — using weak fallback")
    return b"ab-oidc-state-weak-fallback"


def sign_state_cookie(state: str, nonce: str, code_verifier: str) -> str:
    """Build the state cookie value: ``state:nonce:verifier:ts`` + HMAC."""
    # Bundle the values with the current time so the cookie can expire.
    ts = int(time.time())
    payload = f"{state}:{nonce}:{code_verifier}:{ts}"
    # Sign the payload so the browser cannot tamper with it.
    sig = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_state_cookie(cookie: str) -> tuple | None:
    """Return ``(state, nonce, code_verifier)`` if the cookie's HMAC is valid and
    fresh (within ``_STATE_TTL_S``); else ``None``."""
    # Missing or malformed cookie.
    if not cookie or "." not in cookie:
        return None
    # Split off the signature (last ".") and recompute it over the payload.
    payload, _, sig = cookie.rpartition(".")
    expected = hmac.new(_state_secret(), payload.encode(), hashlib.sha256).hexdigest()
    # Signature mismatch: the cookie was tampered with or signed with another key.
    if not hmac.compare_digest(expected, sig):
        return None
    # The payload must have exactly four ":"-separated parts.
    parts = payload.split(":")
    if len(parts) != 4:
        return None
    state, nonce, verifier, ts_s = parts
    # The timestamp must be an integer.
    try:
        ts = int(ts_s)
    except ValueError:
        return None
    # Too old (or from the future): the login round-trip took too long.
    if abs(time.time() - ts) > _STATE_TTL_S:
        return None
    # Valid and fresh: hand back the values needed to finish the login.
    return state, nonce, verifier


# ── PKCE helpers ────────────────────────────────────────────────────────────

def new_pkce() -> tuple:
    """Return ``(state, nonce, code_verifier, code_challenge)``."""
    # Random state (CSRF protection), nonce (replay protection) and PKCE verifier.
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    # PKCE S256 challenge: base64url(SHA-256(verifier)) without padding.
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return state, nonce, code_verifier, code_challenge


def hmac_eq(a: str, b: str) -> bool:
    """Compare two strings in constant time (treating None as empty) to avoid timing leaks."""
    return hmac.compare_digest(str(a or ""), str(b or ""))


# ── discovery + authorize URL ───────────────────────────────────────────────

# Cached discovery documents per tenant, so Entra is not queried on every login.
_discovery_cache: dict = {}  # tenant_id -> (fetched_at, doc)


async def discover(cfg: OidcConfig, http: httpx.AsyncClient | None = None) -> dict:
    """Fetch + cache the OIDC discovery doc (5 min TTL, so JWKS rotation is
    picked up without a restart)."""
    # Serve the cached document while it is less than 5 minutes old.
    now = time.time()
    cached = _discovery_cache.get(cfg.tenant_id)
    if cached and now - cached[0] < 300:
        return cached[1]
    # Fetch the discovery document (using the caller's client if given, e.g. in tests).
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.get(cfg.discovery_url())
        resp.raise_for_status()
        doc = resp.json()
    # Remember it with its fetch time.
    _discovery_cache[cfg.tenant_id] = (now, doc)
    return doc


def authorize_url(cfg: OidcConfig, discovery_doc: dict,
                  state: str, nonce: str, code_challenge: str) -> str:
    """Build the Entra authorize URL (Authorization Code + PKCE)."""
    # Use the endpoint advertised by discovery, or the well-known Entra default.
    endpoint = discovery_doc.get("authorization_endpoint") or \
        f"https://login.microsoftonline.com/{cfg.tenant_id}/oauth2/v2.0/authorize"
    # Authorization Code + PKCE request parameters.
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
    # Append the URL-encoded parameters as the query string.
    return endpoint + "?" + "&".join(f"{k}={_url_quote(str(v), safe='')}"
                                     for k, v in params.items())


# ── client assertion (cert-based confidential client) ───────────────────────

def cert_thumbprint_x5t(cert_pem: bytes) -> str:
    """``x5t`` header Entra requires: base64url( SHA-1( DER(cert) ) )."""
    # Imported lazily; only needed for certificate-based authentication.
    from cryptography import x509
    # Parse the PEM certificate and re-encode it as DER bytes.
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    # SHA-1 of the DER bytes, base64url-encoded without padding.
    return base64.urlsafe_b64encode(hashlib.sha1(der).digest()).rstrip(b"=").decode()


def build_client_assertion(cfg: OidcConfig, token_endpoint: str) -> str:
    """Build a RS256 JWT ``client_assertion`` signed by the cert's private key."""
    # Fail clearly when the private key or certificate file is missing.
    if not cfg.key_path or not os.path.exists(cfg.key_path):
        raise OidcError(f"OIDC client key not found at {cfg.key_path!r}")
    if not cfg.cert_path or not os.path.exists(cfg.cert_path):
        raise OidcError(f"OIDC client certificate not found at {cfg.cert_path!r} "
                        "(needed for the Entra x5t header)")
    # Load the private key (unencrypted PEM) and the certificate.
    with open(cfg.key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), None)
    with open(cfg.cert_path, "rb") as f:
        cert_pem = f.read()
    # Standard client-assertion claims: this app as issuer/subject, the token endpoint as
    # audience, a unique id, and a 5-minute validity window.
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
    # Entra identifies the signing certificate by its thumbprint in the x5t header.
    headers = {"x5t": cert_thumbprint_x5t(cert_pem)}
    # Sign with RS256 using the certificate's private key.
    return jwt.encode(payload, key, algorithm="RS256", headers=headers)


# ── code exchange ───────────────────────────────────────────────────────────

def _entra_error_detail(resp) -> str:
    """Extract Entra's ``error_description`` (AADSTS code) from a failed token
    response so the callback surfaces WHY. Safe to show (no secret)."""
    # Prefer Entra's JSON error description, first line only, capped at 300 chars.
    try:
        j = resp.json()
        detail = j.get("error_description") or j.get("error") or ""
        detail = str(detail).replace("\r", "\n").split("\n")[0].strip()[:300]
        return f" — {detail}" if detail else ""
    # Non-JSON response: fall back to the start of the raw body text.
    except Exception:  # noqa: BLE001
        txt = (getattr(resp, "text", "") or "")[:200].strip()
        return f" — {txt}" if txt else ""


async def exchange_code(cfg: OidcConfig, discovery_doc: dict,
                        code: str, code_verifier: str,
                        http: httpx.AsyncClient | None = None) -> dict:
    """Exchange an authorization code for tokens, authenticating this app to
    Entra with the client secret OR a cert-signed ``client_assertion``."""
    # Token endpoint from discovery, or the well-known Entra default.
    token_endpoint = discovery_doc.get("token_endpoint") or \
        f"https://login.microsoftonline.com/{cfg.tenant_id}/oauth2/v2.0/token"
    # Authorization-code grant, including the PKCE verifier proving we started this login.
    data = {
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": code_verifier,
        "scope": _DEFAULT_SCOPE,
    }
    # Authenticate this app with the client secret when one is configured ...
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    # ... otherwise with a certificate-signed JWT client assertion.
    else:
        data["client_assertion_type"] = \
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        data["client_assertion"] = build_client_assertion(cfg, token_endpoint)
    # Send the token request.
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.post(token_endpoint, data=data)
    # Any non-200 reply is a failed exchange; include Entra's reason.
    if resp.status_code != 200:
        raise OidcError(f"token exchange failed: HTTP {resp.status_code}"
                        f"{_entra_error_detail(resp)}")
    # Return the token response (id_token, access_token, ...).
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
    # The tenant's v2.0 token endpoint.
    token_endpoint = (f"https://login.microsoftonline.com/{cfg.tenant_id}"
                      "/oauth2/v2.0/token")
    # Client-credentials grant for the requested resource scope.
    data = {
        "grant_type": "client_credentials",
        "client_id": cfg.client_id,
        "scope": scope,
    }
    # Authenticate with the secret if present, otherwise with a certificate assertion.
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    else:
        data["client_assertion_type"] = \
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        data["client_assertion"] = build_client_assertion(cfg, token_endpoint)
    # Request the token.
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.post(token_endpoint, data=data)
    # Surface Entra's reason when the request fails.
    if resp.status_code != 200:
        raise OidcError(f"app-token failed ({scope}): HTTP {resp.status_code}"
                        f"{_entra_error_detail(resp)}")
    # The response must actually contain an access token.
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
    # Get an app-only Microsoft Graph token.
    token = await fetch_app_token(cfg, "https://graph.microsoft.com/.default",
                                  http=http)
    # Collected groups, and the first page URL (only id and name, max page size).
    out: list = []
    url = ("https://graph.microsoft.com/v1.0/groups"
           "?$select=id,displayName&$top=999")
    # Follow the paging links until there are no more pages.
    async with (http or httpx.AsyncClient(timeout=20.0)) as client:
        while url:
            # Fetch one page of groups using the Graph token.
            resp = await client.get(url,
                                    headers={"Authorization": f"Bearer {token}"})
            # A failed page aborts the listing with Graph's error text.
            if resp.status_code != 200:
                raise OidcError(f"Graph groups list failed: HTTP "
                                f"{resp.status_code} — {resp.text[:200]}")
            # Add each group with an id, using the id as its name if it has none.
            body = resp.json()
            for v in body.get("value", []):
                gid = v.get("id")
                if gid:
                    out.append({"id": gid,
                                "displayName": v.get("displayName") or gid})
                    # Stop once the limit is reached.
                    if len(out) >= limit:
                        return out
            # Next page URL, or None when this was the last page.
            url = body.get("@odata.nextLink")
    return out


# ── id-token verification ───────────────────────────────────────────────────

async def fetch_jwks(jwks_uri: str, http: httpx.AsyncClient | None = None) -> list:
    """Fetch the JWKS signing keys."""
    # Download the key set and return its list of keys.
    async with (http or httpx.AsyncClient(timeout=15.0)) as client:
        resp = await client.get(jwks_uri)
        resp.raise_for_status()
        return resp.json().get("keys", [])


def _jwk_to_key(jwk: dict):
    """Convert one JWKS entry into a key object PyJWT can verify signatures with."""
    # The key type decides which PyJWT loader to use.
    kty = jwk.get("kty")
    # RSA keys (what Entra normally uses).
    if kty == "RSA":
        return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    # Elliptic-curve keys.
    if kty == "EC":
        return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
    # Anything else cannot be used for verification.
    raise OidcError(f"unsupported JWKS kty: {kty!r}")


def verify_id_token(cfg: OidcConfig, id_token: str, nonce: str,
                    jwks_keys: list) -> dict:
    """Verify an Entra id-token (signature/issuer/audience/nonce) and return the
    decoded claims. MFA is enforced by Entra Conditional Access, not here."""
    # Read the token header (unverified) to learn which key id signed it.
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as e:
        raise OidcError(f"malformed id_token: {e}") from e
    # Prefer keys matching the token's key id; if none match, try every key.
    kid = unverified_header.get("kid")
    keys = [_jwk_to_key(k) for k in jwks_keys if k.get("kid") == kid] or \
           [_jwk_to_key(k) for k in jwks_keys]
    # No keys at all: cannot verify.
    if not keys:
        raise OidcError("no matching JWKS key for id_token kid")
    # Try each candidate key until one verifies the signature, issuer, audience and expiry.
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
    # No key verified the token: reject it with the last error seen.
    if claims is None:
        raise OidcError(f"id_token verification failed: {last_err}")
    # The nonce must match the one we generated, proving this token belongs to this login.
    if claims.get("nonce") != nonce:
        raise OidcError("nonce mismatch — id_token replay suspected")
    # Return the verified claims.
    return claims


def extract_member_groups(claims: dict) -> list:
    """The Entra ``groups`` claim (group object IDs), when present."""
    # Return the group ids as strings when the claim is a list; otherwise no groups.
    g = claims.get("groups")
    if isinstance(g, list):
        return [str(x) for x in g]
    return []


def enforce_allowed_group(allowed_group: str, member_of: list) -> None:
    """If ``allowed_group`` is set, refuse a user who is not in any of the
    listed group object IDs. ``allowed_group`` may be a comma/space-separated
    list; membership in ANY grants login."""
    # Imported locally; only needed to split the allowed-group list.
    import re as _re
    # Split the allowed-group setting on commas/whitespace into individual group ids.
    allowed = [g for g in _re.split(r"[\s,]+", str(allowed_group or "")) if g.strip()]
    # No restriction configured: everyone may log in.
    if not allowed:
        return
    # The user shares no group with the allowed list.
    if not (set(member_of or []) & set(allowed)):
        # No groups at all usually means the token is missing the groups claim; say so.
        if not member_of:
            raise OidcError(
                "Access denied: this app read 0 groups for you (the id_token had "
                "no groups claim). Add a groups claim in the Entra app's Token "
                "Configuration, or clear the allowed-group restriction.")
        # The user has groups, just none of the allowed ones.
        raise OidcError(
            "Access denied: you are not a member of an allowed group. Each "
            "allowed-group entry must be the group's OBJECT ID (a UUID).")


# ── provisioning ────────────────────────────────────────────────────────────

def provision_entra_user(claims: dict) -> str:
    """Provision (first login) or refresh an Entra user in the local user store
    and return the username to open a session for. The username is the user's
    email / UPN when available (stable + human-readable), else the ``oid``."""
    # Read the stable object id, email/UPN and display name from the token claims.
    oid = str(claims.get("oid") or "").strip()
    email = str(claims.get("email") or claims.get("preferred_username") or "").strip()
    name = str(claims.get("name") or "").strip()
    # Without either an id or an email there is no way to identify the user.
    if not oid and not email:
        raise OidcError("id_token missing both oid and email claims")
    # Use the email as the username when present (readable), else the object id.
    username = email or oid
    # Create or update the local "entra" account for this user.
    _auth.upsert_external_user(username, provider="entra", email=email,
                               name=name, oid=oid)
    # The caller opens a session for this username.
    return username

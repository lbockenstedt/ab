"""Tests for the Entra allowed-group picker backend (``oidc.fetch_app_token`` /
``oidc.fetch_directory_groups``).

These back the Settings → Sign-in (SSO) group picker. The picker exists because
``allowed_group`` only ever matches group OBJECT IDs, so an admin who types a
group *name* silently locks every user out — the list has to return real IDs.

No network: a stub stands in for ``httpx.AsyncClient`` so the request shape
(grant type, credential choice, Authorization header) and the ``@odata.nextLink``
paging are asserted directly.
"""
import asyncio

import pytest

import oidc


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


class _StubClient:
    """Minimal async httpx.AsyncClient stand-in that records calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        self.posts.append((url, data))
        return self._responses.pop(0)

    async def get(self, url, headers=None):
        self.gets.append((url, headers))
        return self._responses.pop(0)


def _run(coro):
    """Run a coroutine on a private loop.

    Deliberately NOT asyncio.get_event_loop(): other modules in this suite
    close or replace the global loop, which made these tests pass in isolation
    and fail when run together."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _tok(cfg, scope, http=None):
    return "TOK"


def _cfg(**over):
    stored = {
        "tenant_id": "tid",
        "client_id": "cid",
        "redirect_uri": "https://h/auth/oidc/callback",
        "client_secret": "s3cret",
    }
    stored.update(over)
    return oidc.OidcConfig(stored)


# ── fetch_app_token ─────────────────────────────────────────────────────────

def test_app_token_uses_client_secret_when_set():
    stub = _StubClient([_Resp({"access_token": "TOK"})])
    tok = _run(oidc.fetch_app_token(_cfg(), "https://graph.microsoft.com/.default", http=stub))
    assert tok == "TOK"
    url, data = stub.posts[0]
    assert url.endswith("/tid/oauth2/v2.0/token")
    assert data["grant_type"] == "client_credentials"
    assert data["scope"] == "https://graph.microsoft.com/.default"
    assert data["client_secret"] == "s3cret"
    # A secret install must NOT also try to sign an assertion it has no key for.
    assert "client_assertion" not in data


def test_app_token_uses_cert_assertion_when_no_secret(monkeypatch):
    monkeypatch.setattr(oidc, "build_client_assertion", lambda cfg, ep: "ASSERTION")
    stub = _StubClient([_Resp({"access_token": "TOK"})])
    _run(oidc.fetch_app_token(_cfg(client_secret=""), "scope/.default", http=stub))
    _, data = stub.posts[0]
    assert data["client_assertion"] == "ASSERTION"
    assert data["client_assertion_type"].endswith("jwt-bearer")
    assert "client_secret" not in data


def test_app_token_raises_on_http_error():
    stub = _StubClient([_Resp({"error": "invalid_client"}, status=401)])
    with pytest.raises(oidc.OidcError):
        _run(oidc.fetch_app_token(_cfg(), "scope/.default", http=stub))


def test_app_token_raises_when_response_has_no_token():
    stub = _StubClient([_Resp({"token_type": "Bearer"})])
    with pytest.raises(oidc.OidcError):
        _run(oidc.fetch_app_token(_cfg(), "scope/.default", http=stub))


# ── fetch_directory_groups ──────────────────────────────────────────────────

def _groups(responses, monkeypatch, **kw):
    monkeypatch.setattr(oidc, "fetch_app_token", _tok)
    stub = _StubClient(responses)
    out = _run(oidc.fetch_directory_groups(_cfg(), http=stub, **kw))
    return out, stub


def test_directory_groups_returns_id_and_name(monkeypatch):
    out, stub = _groups([_Resp({"value": [
        {"id": "g1", "displayName": "Lab Admin Users"},
        {"id": "g2", "displayName": "Lab Manager Admin"},
    ]})], monkeypatch)
    assert out == [{"id": "g1", "displayName": "Lab Admin Users"},
                   {"id": "g2", "displayName": "Lab Manager Admin"}]
    _, headers = stub.gets[0]
    assert headers["Authorization"] == "Bearer TOK"


def test_directory_groups_follows_odata_next_link(monkeypatch):
    out, stub = _groups([
        _Resp({"value": [{"id": "g1", "displayName": "A"}],
               "@odata.nextLink": "https://graph.microsoft.com/page2"}),
        _Resp({"value": [{"id": "g2", "displayName": "B"}]}),
    ], monkeypatch)
    assert [g["id"] for g in out] == ["g1", "g2"]
    assert stub.gets[1][0] == "https://graph.microsoft.com/page2"


def test_directory_groups_falls_back_to_id_when_unnamed(monkeypatch):
    out, _ = _groups([_Resp({"value": [{"id": "g1"}]})], monkeypatch)
    assert out == [{"id": "g1", "displayName": "g1"}]


def test_directory_groups_skips_entries_without_id(monkeypatch):
    out, _ = _groups([_Resp({"value": [{"displayName": "no id"},
                                       {"id": "g1", "displayName": "A"}]})],
                     monkeypatch)
    assert [g["id"] for g in out] == ["g1"]


def test_directory_groups_honours_limit(monkeypatch):
    out, _ = _groups([_Resp({"value": [{"id": f"g{i}"} for i in range(10)],
                             "@odata.nextLink": "https://graph/next"})],
                     monkeypatch, limit=3)
    assert len(out) == 3


def test_directory_groups_raises_on_graph_error(monkeypatch):
    monkeypatch.setattr(oidc, "fetch_app_token", _tok)
    stub = _StubClient([_Resp({"error": "Authorization_RequestDenied"}, status=403)])
    with pytest.raises(oidc.OidcError):
        _run(oidc.fetch_directory_groups(_cfg(), http=stub))


# ── the IDs the picker returns must be what login actually matches ──────────

def test_picked_group_id_is_what_enforce_allowed_group_matches(monkeypatch):
    """The picker's value is pasted straight into allowed_group, so a group the
    admin picks must let a member in — and a group NAME must not."""
    out, _ = _groups([_Resp({"value": [
        {"id": "6ca5db04-502c-4af4-adb8-e27d4a8fa5bb",
         "displayName": "Lab Manager Admin"}]})], monkeypatch)
    picked = out[0]["id"]
    # A member of the picked group is admitted.
    oidc.enforce_allowed_group(picked, [picked])
    # The display NAME must not be accepted as a substitute for the object ID.
    with pytest.raises(oidc.OidcError):
        oidc.enforce_allowed_group(out[0]["displayName"], [picked])


def test_multiple_picked_groups_are_or_ed():
    """The UI joins picks with ', ' — membership in ANY must grant access."""
    allowed = "aaa, bbb"
    oidc.enforce_allowed_group(allowed, ["bbb"])
    with pytest.raises(oidc.OidcError):
        oidc.enforce_allowed_group(allowed, ["ccc"])

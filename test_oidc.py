"""Tests for the Azure Entra (OIDC) login provider and external-user provisioning."""
import base64
import importlib
import json
import os
import time

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture()
def ab_env(tmp_path, monkeypatch):
    """Point auth at a temp dir and inject an in-memory config store.

    ``config_store`` cannot be imported in a unit test (it does ``from main
    import logger`` and main.py's import chain circularly re-imports it — see
    test_config_store_atomic_write.py). ``auth`` tolerates that (it falls back to
    ``AB_CONFIG_DIR``), and ``oidc`` only touches config_store through the lazily
    resolved ``_config_store()`` hook, which we replace with a fake here."""
    monkeypatch.setenv("AB_CONFIG_DIR", str(tmp_path))
    for mod in ("auth", "oidc"):
        importlib.sys.modules.pop(mod, None)
    import auth
    import oidc
    monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(auth, "USERS_FILE", os.path.join(str(tmp_path), "users.json"), raising=False)
    store = {"config": {}}
    def _fake_load():
        return json.loads(json.dumps(store["config"]))
    def _fake_save(cfg):
        store["config"] = json.loads(json.dumps(cfg))
    monkeypatch.setattr(oidc, "_config_store", lambda: (_fake_load, _fake_save))
    return store, auth, oidc


def test_config_from_store_and_env(ab_env, monkeypatch):
    store, _auth, oidc = ab_env
    store["config"] = {"oidc": {"enabled": True, "tenant_id": "t1",
                                "client_id": "c1", "redirect_uri": "https://h/cb",
                                "client_secret": "shh"}}
    cfg = oidc.get_oidc_config()
    assert cfg.enabled and cfg.ready and cfg.tenant_id == "t1"
    # env overrides stored config
    monkeypatch.setenv("AB_OIDC_TENANT_ID", "envtenant")
    assert oidc.get_oidc_config().tenant_id == "envtenant"


def test_not_ready_without_credential(ab_env):
    _, _a, oidc = ab_env
    cfg = oidc.OidcConfig({"tenant_id": "t", "client_id": "c", "redirect_uri": "u"})
    assert not cfg.ready  # no secret or key
    cfg2 = oidc.OidcConfig({"tenant_id": "t", "client_id": "c",
                            "redirect_uri": "u", "client_secret": "s"})
    assert cfg2.ready


def test_state_cookie_roundtrip_and_tamper(ab_env):
    _, _a, oidc = ab_env
    cookie = oidc.sign_state_cookie("st", "no", "vf")
    assert oidc.verify_state_cookie(cookie) == ("st", "no", "vf")
    assert oidc.verify_state_cookie(cookie + "x") is None  # bad sig
    assert oidc.verify_state_cookie("garbage") is None


def test_state_cookie_expiry(ab_env, monkeypatch):
    _, _a, oidc = ab_env
    cookie = oidc.sign_state_cookie("st", "no", "vf")
    _orig = time.time
    monkeypatch.setattr(oidc.time, "time", lambda: _orig() + oidc._STATE_TTL_S + 5)
    assert oidc.verify_state_cookie(cookie) is None


def test_pkce_challenge_matches_verifier(ab_env):
    _, _a, oidc = ab_env
    _s, _n, verifier, challenge = oidc.new_pkce()
    import hashlib
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect


def test_authorize_url_has_pkce_params(ab_env):
    _, _a, oidc = ab_env
    cfg = oidc.OidcConfig({"tenant_id": "t", "client_id": "cid",
                           "redirect_uri": "https://h/cb", "client_secret": "s"})
    url = oidc.authorize_url(cfg, {}, "state1", "nonce1", "chal1")
    assert "code_challenge=chal1" in url and "code_challenge_method=S256" in url
    assert "client_id=cid" in url and "response_type=code" in url


def _make_cert_and_key(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ab-test")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(__import__("datetime").datetime.utcnow())
            .not_valid_after(__import__("datetime").datetime.utcnow()
                             + __import__("datetime").timedelta(days=1))
            .sign(key, hashes.SHA256()))
    key_path = tmp_path / "k.pem"
    cert_path = tmp_path / "c.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(key_path), str(cert_path)


def test_client_assertion_is_valid_rs256(ab_env, tmp_path):
    _, _a, oidc = ab_env
    key_path, cert_path = _make_cert_and_key(tmp_path)
    cfg = oidc.OidcConfig({"tenant_id": "t", "client_id": "cid",
                           "redirect_uri": "u", "key_path": key_path, "cert_path": cert_path})
    assert cfg.ready  # key_path counts as a credential
    assertion = oidc.build_client_assertion(cfg, "https://login/token")
    header = jwt.get_unverified_header(assertion)
    assert "x5t" in header
    with open(cert_path, "rb") as f:
        pub = x509.load_pem_x509_certificate(f.read()).public_key()
    claims = jwt.decode(assertion, pub, algorithms=["RS256"], audience="https://login/token")
    assert claims["iss"] == "cid" and claims["sub"] == "cid"


def test_verify_id_token_end_to_end(ab_env, tmp_path):
    _, _a, oidc = ab_env
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cfg = oidc.OidcConfig({"tenant_id": "TEN", "client_id": "AUD", "client_secret": "s",
                           "redirect_uri": "u"})
    now = int(time.time())
    claims = {"iss": cfg.issuer(), "aud": "AUD", "iat": now, "exp": now + 300,
              "nonce": "N1", "oid": "oid-123", "email": "u@example.com", "name": "U"}
    id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "K1"})
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "K1"
    out = oidc.verify_id_token(cfg, id_token, "N1", [jwk])
    assert out["oid"] == "oid-123"
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(cfg, id_token, "WRONG-NONCE", [jwk])


def test_group_gate(ab_env):
    _, _a, oidc = ab_env
    oidc.enforce_allowed_group("", [])  # no restriction -> ok
    oidc.enforce_allowed_group("g1, g2", ["g2"])  # member -> ok
    with pytest.raises(oidc.OidcError):
        oidc.enforce_allowed_group("g1", ["g9"])
    with pytest.raises(oidc.OidcError):
        oidc.enforce_allowed_group("g1", [])  # no groups claim


def test_provision_entra_user_creates_passwordless_account(ab_env):
    _, auth, oidc = ab_env
    username = oidc.provision_entra_user({"oid": "o1", "email": "sso@x.com", "name": "SSO"})
    assert username == "sso@x.com"
    users = auth.list_users()
    rec = [u for u in users if u["username"] == "sso@x.com"][0]
    assert rec["auth_type"] == "entra"
    # passwordless: a password login must be impossible
    assert auth.verify_credentials("sso@x.com", "anything") is False
    # second login refreshes, does not duplicate
    oidc.provision_entra_user({"oid": "o1", "email": "sso@x.com", "name": "SSO2"})
    assert len([u for u in auth.list_users() if u["username"] == "sso@x.com"]) == 1


def test_local_account_not_converted_to_sso(ab_env):
    _, auth, _o = ab_env
    ok, _ = auth.create_user("localguy", "hunter2000")
    assert ok
    ok2, msg = auth.upsert_external_user("localguy", provider="entra", email="x")
    assert not ok2 and "local password account" in msg


def test_save_config_redacts_and_preserves_secret(ab_env):
    _store, _a, oidc = ab_env
    oidc.save_oidc_config({"tenant_id": "t", "client_id": "c",
                           "redirect_uri": "u", "client_secret": "topsecret", "enabled": True})
    # saving other fields with blank secret must NOT wipe the stored secret
    stored = oidc.save_oidc_config({"tenant_id": "t2", "client_secret": ""})
    assert stored["client_secret_configured"] is True
    assert "client_secret" not in stored  # never returned raw
    cfg = oidc.get_oidc_config()
    assert cfg.client_secret == "topsecret" and cfg.tenant_id == "t2"
    # sentinel clears it
    oidc.save_oidc_config({"client_secret": "__CLEAR__"})
    assert oidc.get_oidc_config().client_secret == ""

import asyncio
import datetime
import json
import os
import shutil
import stat
import sys
import types

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import hub_agent
from hub_agent import HubAgentClient, MessageSigner, split_frame

_SCRATCH = os.path.join(os.getcwd(), ".test-run", "hub-agent-mtls")


class _FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = []

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self, code=None, reason=None):
        self.closed.append((code, reason))


def _decode_sent(frame):
    _sig, body = split_frame(frame)
    return json.loads(body)


def _client(base):
    c = HubAgentClient.__new__(HubAgentClient)
    c._ws = _FakeWs()
    c.signer = MessageSigner("secret")
    c.spoke_id = "ab"
    c._hub_mtls = True
    c._present_cert = True
    c._tls_ca_cert = os.path.join(base, "etc", "hub-ca.pem")
    c._client_cert_file = os.path.join(base, "etc", "hub-client-cert.pem")
    c._client_key_file = os.path.join(base, "etc", "hub-client-key.pem")
    c._verify = lambda _msg: True
    return c


def _pair(cn="ab.example"):
    """A real self-signed (cert PEM, key PEM) pair, so load_cert_chain accepts it."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(getattr(datetime, "UTC", datetime.timezone.utc))
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert_pem, key_pem


def _read(path):
    """The text of ``path``."""
    with open(path) as f:
        return f.read()


def _reset_env():
    for key in ("AB_CONFIG_DIR", "AB_SSL_CERT", "AB_SSL_KEY", "LM_HUB_CA_CERT"):
        os.environ.pop(key, None)


def setup_function(_fn):
    shutil.rmtree(_SCRATCH, ignore_errors=True)
    os.makedirs(_SCRATCH, exist_ok=True)
    _reset_env()


def teardown_function(_fn):
    _reset_env()
    shutil.rmtree(_SCRATCH, ignore_errors=True)


def test_set_mtls_materials_persists_ca_without_clobbering_ab_client_cert():
    base = os.path.join(_SCRATCH, "set-materials")
    etc = os.path.join(base, "etc")
    os.makedirs(etc, exist_ok=True)
    os.environ["AB_CONFIG_DIR"] = etc
    c = _client(base)
    with open(c._client_cert_file, "w") as f:
        f.write("dedicated-ab-client-cert\n")
    with open(c._client_key_file, "w") as f:
        f.write("dedicated-ab-client-key\n")

    msg = {
        "header": {"message_id": "corr-1"},
        "payload": {
            "type": "SPOKE_SET_MTLS_MATERIALS",
            "data": {
                "ca_bundle": "hub-ca-bundle",
                "client_cert": "wildcard-client-cert",
                "client_key": "wildcard-client-key",
            },
        },
    }
    asyncio.run(c._handle_message(msg))

    assert open(c._tls_ca_cert).read() == "hub-ca-bundle\n"
    assert open(c._client_cert_file).read() == "dedicated-ab-client-cert\n"
    assert open(c._client_key_file).read() == "dedicated-ab-client-key\n"
    assert f"LM_HUB_CA_CERT={c._tls_ca_cert}\n" in open(os.path.join(etc, ".env")).read()
    reply = _decode_sent(c._ws.sent[-1])
    assert reply["correlation_id"] == "corr-1"
    assert reply["payload"]["data"]["status"] == "SUCCESS"

    asyncio.run(c._handle_get_mtls_status({"header": {"message_id": "corr-2"}}))
    status = _decode_sent(c._ws.sent[-1])["payload"]["data"]["mtls"]
    assert status["ca_present"] is True
    assert status["client_cert_present"] is True
    assert status["client_key_present"] is True
    assert status["ca_path"] == c._tls_ca_cert
    assert status["client_cert_path"] == c._client_cert_file
    assert status["client_key_path"] == c._client_key_file


def test_install_cert_persists_chain_as_hub_ca_bundle():
    base = os.path.join(_SCRATCH, "install-cert")
    etc = os.path.join(base, "etc")
    os.makedirs(etc, exist_ok=True)
    os.environ["AB_CONFIG_DIR"] = etc
    os.environ["AB_SSL_CERT"] = os.path.join(etc, "cert.pem")
    os.environ["AB_SSL_KEY"] = os.path.join(etc, "key.pem")
    c = _client(base)
    leaf, key = _pair()
    intermediate = _pair("intermediate")[0]

    asyncio.run(c._handle_install_cert(
        {"header": {"message_id": "corr-3"}},
        {"fullchain": f"{leaf}{intermediate}", "privkey": key},
    ))

    assert _read(os.environ["AB_SSL_CERT"]) == f"{leaf}{intermediate}"
    assert _read(os.environ["AB_SSL_KEY"]) == key
    assert _read(c._tls_ca_cert) == intermediate
    assert f"LM_HUB_CA_CERT={c._tls_ca_cert}\n" in _read(os.path.join(etc, ".env"))
    reply = _decode_sent(c._ws.sent[-1])
    assert reply["correlation_id"] == "corr-3"
    assert reply["payload"]["data"]["status"] == "SUCCESS"


def _install_cert_env(name):
    """Point the WebUI cert/key and config dir at a fresh scratch dir."""
    etc = os.path.join(_SCRATCH, name, "etc")
    os.makedirs(etc, exist_ok=True)
    os.environ["AB_CONFIG_DIR"] = etc
    os.environ["AB_SSL_CERT"] = os.path.join(etc, "cert.pem")
    os.environ["AB_SSL_KEY"] = os.path.join(etc, "key.pem")
    return os.path.join(_SCRATCH, name), etc


def test_private_keys_and_env_are_owner_only_from_creation(monkeypatch):
    """A key must never be created under the umask and chmod-ed afterwards: a
    failed (here: no-op) chmod used to leave it world-readable for good."""
    base, etc = _install_cert_env("perms")
    monkeypatch.setattr(hub_agent.os, "chmod", lambda *a, **k: None)
    old_umask = os.umask(0o022)
    try:
        c = _client(base)
        cert, key = _pair()
        asyncio.run(c._handle_install_cert({"header": {"message_id": "p1"}},
                                           {"fullchain": cert, "privkey": key}))
        asyncio.run(c._handle_message({
            "header": {"message_id": "p2"},
            "payload": {"type": "SPOKE_SET_MTLS_CLIENT_CERT",
                        "data": {"cert": cert, "key": key}}}))
        c._persist_env_key("AB_TEST_SECRET", "s3cret")
    finally:
        os.umask(old_umask)
        os.environ.pop("AB_TEST_SECRET", None)
    for path in (os.environ["AB_SSL_KEY"], c._client_key_file, os.path.join(etc, ".env")):
        assert os.path.exists(path), path
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0, path
    assert not [n for n in os.listdir(etc) if ".tmp-" in n]


def test_mismatched_pair_is_refused_and_keeps_the_old_files():
    """INSTALL_CERT restarts AB to load the pair; a pair that will not load must
    be refused before anything is written, or AB cannot start again."""
    base, _etc = _install_cert_env("mismatch")
    c = _client(base)
    good_cert, good_key = _pair()
    with open(os.environ["AB_SSL_CERT"], "w") as f:
        f.write(good_cert)
    with open(os.environ["AB_SSL_KEY"], "w") as f:
        f.write(good_key)
    other_key = _pair()[1]
    for bad in ({"fullchain": good_cert, "privkey": other_key},
                {"fullchain": "not a cert\n", "privkey": good_key}):
        asyncio.run(c._handle_install_cert({"header": {"message_id": "m"}}, bad))
        reply = _decode_sent(c._ws.sent[-1])
        assert reply["payload"]["data"]["status"] == "ERROR"
        assert _read(os.environ["AB_SSL_CERT"]) == good_cert
        assert _read(os.environ["AB_SSL_KEY"]) == good_key


def test_mismatched_mtls_client_pair_is_refused():
    """The same check guards the hub mTLS client identity."""
    base, _etc = _install_cert_env("mismatch-mtls")
    c = _client(base)
    os.makedirs(os.path.dirname(c._client_cert_file), exist_ok=True)
    with open(c._client_cert_file, "w") as f:
        f.write("old-cert\n")
    cert = _pair()[0]
    asyncio.run(c._handle_message({
        "header": {"message_id": "m2"},
        "payload": {"type": "SPOKE_SET_MTLS_CLIENT_CERT",
                    "data": {"cert": cert, "key": _pair()[1]}}}))
    assert _read(c._client_cert_file) == "old-cert\n"
    assert not os.path.exists(c._client_key_file)
    assert not c._ws.closed


def _old_webui_pair(name):
    """Install a working WebUI pair in a fresh scratch dir; return (client, cert, key)."""
    base, _etc = _install_cert_env(name)
    cert, key = _pair("old.example")
    with open(os.environ["AB_SSL_CERT"], "w") as f:
        f.write(cert)
    with open(os.environ["AB_SSL_KEY"], "w") as f:
        f.write(key)
    return _client(base), cert, key


def test_failed_swap_of_either_file_keeps_the_old_pair(monkeypatch):
    """Cert and key are one unit: if replacing either file fails, the ERROR reply
    must leave the old cert AND the old key on disk, never a mixed pair."""
    real_replace = os.replace
    for failing in ("AB_SSL_KEY", "AB_SSL_CERT"):
        c, old_cert, old_key = _old_webui_pair(f"swap-{failing}")
        target = os.environ[failing]

        def _replace(src, dst, _target=target):
            if dst == _target and ".tmp-" in src:
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        monkeypatch.setattr(hub_agent.os, "replace", _replace)
        new_cert, new_key = _pair("new.example")
        asyncio.run(c._handle_install_cert({"header": {"message_id": "s"}},
                                           {"fullchain": new_cert, "privkey": new_key}))
        monkeypatch.setattr(hub_agent.os, "replace", real_replace)
        reply = _decode_sent(c._ws.sent[-1])
        assert reply["payload"]["data"]["status"] == "ERROR", failing
        assert _read(os.environ["AB_SSL_CERT"]) == old_cert, failing
        assert _read(os.environ["AB_SSL_KEY"]) == old_key, failing
        etc = os.path.dirname(target)
        assert not [n for n in os.listdir(etc) if ".tmp-" in n], failing


def test_failed_rollback_is_reported_as_a_mismatched_pair(monkeypatch):
    """If the cert swap fails AND the key cannot be put back, the files are a new
    key with the old cert. That ERROR must say so, not read like an ordinary
    failure that kept the old pair; with or without a previous key."""
    real_replace, real_remove = os.replace, os.remove
    for had_key in (True, False):
        c, _old_cert, _old_key = _old_webui_pair(f"rollback-{had_key}")
        cert_path, key_path = os.environ["AB_SSL_CERT"], os.environ["AB_SSL_KEY"]
        if not had_key:
            os.remove(key_path)

        def _replace(src, dst):
            if dst == cert_path and ".tmp-" in src:
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        def _remove(path):
            if path == key_path:
                raise PermissionError(13, "Permission denied")
            return real_remove(path)

        def _no_restore(*_a, **_k):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(hub_agent.os, "replace", _replace)
        monkeypatch.setattr(hub_agent.os, "remove", _remove)
        monkeypatch.setattr(c, "_atomic_write_file", _no_restore)
        new_cert, new_key = _pair("new.example")
        asyncio.run(c._handle_install_cert({"header": {"message_id": "r"}},
                                           {"fullchain": new_cert, "privkey": new_key}))
        monkeypatch.setattr(hub_agent.os, "replace", real_replace)
        monkeypatch.setattr(hub_agent.os, "remove", real_remove)
        data = _decode_sent(c._ws.sent[-1])["payload"]["data"]
        assert data["status"] == "ERROR", had_key
        assert "could not be restored" in data["message"], had_key
        assert "no longer matches" in data["message"], had_key
        assert _read(key_path) == new_key, had_key


def test_ca_bundle_failure_after_install_replies_error_without_restart(monkeypatch):
    """The pair and the hub CA bundle go live together. A failed bundle save after
    the pair is written replies ERROR naming the partial state (so the hub retries)
    and does not restart (a restart could reconnect with a stale trust bundle). The
    retry rewrites the same pair, saves the bundle, and restarts."""
    app_state = types.SimpleNamespace(state={"restart_pending": False})
    monkeypatch.setitem(sys.modules, "app_state", app_state)
    c, _old_cert, _old_key = _old_webui_pair("ca-fails")
    leaf, key = _pair("new.example")
    intermediate = _pair("intermediate")[0]
    real_persist = c._persist_hub_ca_bundle

    def _boom(_bundle):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(c, "_persist_hub_ca_bundle", _boom)
    payload = {"fullchain": f"{leaf}{intermediate}", "privkey": key}
    asyncio.run(c._handle_install_cert({"header": {"message_id": "ca"}}, payload))

    data = _decode_sent(c._ws.sent[-1])["payload"]["data"]
    assert data["status"] == "ERROR"
    assert "written but not loaded" in data["message"]
    assert "hub CA bundle not saved (OSError)" in data["message"]
    assert _read(os.environ["AB_SSL_CERT"]) == f"{leaf}{intermediate}"
    assert _read(os.environ["AB_SSL_KEY"]) == key
    assert app_state.state["restart_pending"] is False

    # The hub's retry of the same push completes it.
    monkeypatch.setattr(c, "_persist_hub_ca_bundle", real_persist)
    asyncio.run(c._handle_install_cert({"header": {"message_id": "ca2"}}, payload))
    assert _decode_sent(c._ws.sent[-1])["payload"]["data"]["status"] == "SUCCESS"
    assert app_state.state["restart_pending"] is True


def test_failed_staging_writes_nothing(monkeypatch):
    """A failure while writing the second staged file (here a failed fsync) must
    change neither file: both are staged before either is replaced."""
    c, old_cert, old_key = _old_webui_pair("stage-fail")
    real_fsync = os.fsync
    calls = []

    def _fsync(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError(5, "I/O error")
        return real_fsync(fd)

    monkeypatch.setattr(hub_agent.os, "fsync", _fsync)
    new_cert, new_key = _pair("new.example")
    asyncio.run(c._handle_install_cert({"header": {"message_id": "f"}},
                                       {"fullchain": new_cert, "privkey": new_key}))
    monkeypatch.setattr(hub_agent.os, "fsync", real_fsync)
    assert _decode_sent(c._ws.sent[-1])["payload"]["data"]["status"] == "ERROR"
    assert _read(os.environ["AB_SSL_CERT"]) == old_cert
    assert _read(os.environ["AB_SSL_KEY"]) == old_key
    assert not [n for n in os.listdir(os.path.dirname(os.environ["AB_SSL_KEY"])) if ".tmp-" in n]


def test_unchanged_key_with_failed_chmod_is_rewritten_owner_only(monkeypatch):
    """An existing key with the same content but a world-readable mode must not
    stay exposed when chmod on it fails: the file is rewritten at 0600 instead."""
    base, etc = _install_cert_env("unchanged-chmod")
    c = _client(base)
    path = os.path.join(etc, "key.pem")
    with open(path, "w") as f:
        f.write("same-key\n")
    os.chmod(path, 0o644)
    real_chmod = os.chmod

    def _chmod(p, mode, *a, **k):
        if p == path:
            raise PermissionError(1, "Operation not permitted")
        return real_chmod(p, mode, *a, **k)

    monkeypatch.setattr(hub_agent.os, "chmod", _chmod)
    assert c._atomic_write_file(path, "same-key\n", 0o600) is True
    monkeypatch.setattr(hub_agent.os, "chmod", real_chmod)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert _read(path) == "same-key\n"
    # Already current (content and mode): no rewrite.
    assert c._atomic_write_file(path, "same-key\n", 0o600) is False


def test_cert_file_work_runs_off_the_event_loop(monkeypatch):
    """Parsing the pair and fsyncing files must not block the event loop: both
    cert handlers and the CA-bundle save go through asyncio.to_thread."""
    c, _cert, _key = _old_webui_pair("off-loop")
    real_to_thread = asyncio.to_thread
    ran = []

    async def _to_thread(fn, *a, **k):
        ran.append(getattr(fn, "__name__", ""))
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(hub_agent.asyncio, "to_thread", _to_thread)
    cert, key = _pair()
    asyncio.run(c._handle_install_cert({"header": {"message_id": "o1"}},
                                       {"fullchain": cert, "privkey": key, "chain": "ca-chain"}))
    asyncio.run(c._handle_message({
        "header": {"message_id": "o2"},
        "payload": {"type": "SPOKE_SET_MTLS_CLIENT_CERT", "data": {"cert": cert, "key": key}}}))
    asyncio.run(c._handle_message({
        "header": {"message_id": "o3"},
        "payload": {"type": "SPOKE_SET_MTLS_MATERIALS", "data": {"ca_bundle": "ca-2"}}}))
    assert ran.count("_install_cert_pair") == 2
    assert ran.count("_persist_hub_ca_bundle") == 2

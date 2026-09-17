import asyncio
import json
import os
import shutil

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
    leaf = "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----"
    intermediate = "-----BEGIN CERTIFICATE-----\nintermediate\n-----END CERTIFICATE-----"

    asyncio.run(c._handle_install_cert(
        {"header": {"message_id": "corr-3"}},
        {"fullchain": f"{leaf}\n{intermediate}\n", "privkey": "webui-key"},
    ))

    assert open(os.environ["AB_SSL_CERT"]).read() == f"{leaf}\n{intermediate}\n"
    assert open(c._tls_ca_cert).read() == f"{intermediate}\n"
    assert f"LM_HUB_CA_CERT={c._tls_ca_cert}\n" in open(os.path.join(etc, ".env")).read()
    reply = _decode_sent(c._ws.sent[-1])
    assert reply["correlation_id"] == "corr-3"
    assert reply["payload"]["data"]["status"] == "SUCCESS"

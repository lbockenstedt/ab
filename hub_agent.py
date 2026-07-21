"""BugFixer Hub agent — a self-contained WebSocket agent client.

This makes BugFixer authenticate to the Lab Manager (LM) Hub the same way every
other spoke/agent does: zero-touch connect, admin approval in the Hub WebUI,
HMAC session-key exchange, then signed heartbeats + request/response messages
over a single persistent WebSocket. It replaces the old static-token HTTP
calls (LM_ADMIN_TOKEN / X-Admin-Token), which the Hub never actually honored.

The module is intentionally self-contained — it reimplements the Hub's signing
scheme (core/src/security/signer.py) and mirrors the connect/auth/heartbeat
handshake from core/src/messaging/control_plane.py without importing the lm
package, so BugFixer can run on hosts that don't have the lm source tree.
"""

import asyncio
import collections
import hashlib
import hmac
import json
import logging
import os
import ssl
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosedError

# Self-contained runtime DEBUG/INFO flip used by the WebUI "Enable Debug"
# button (the Hub broadcasts SET_LOG_LEVEL to every connected spoke). Inline
# fallback because bugfixer does NOT import lm/core (see module docstring) — the
# fallback is the standard block.
try:
    from logging_setup import set_log_level
except ImportError:
    try:
        from core.src.logging_setup import set_log_level
    except ImportError:
        def set_log_level(enabled):
            level = logging.DEBUG if enabled else logging.INFO
            logging.getLogger().setLevel(level)
            for _n in list(logging.root.manager.loggerDict):
                logging.getLogger(_n).setLevel(level)
            return level

logger = logging.getLogger("HubAgent")


class _HubLogRelayHandler(logging.Handler):
    """Captures INFO+ records into a bounded ring buffer for relay to the hub as
    SPOKE_LOG. Per logging-observability-contract.md: the BugFixer's own logs
    and crashes must reach the hub (Error Log + the BugFixer's own GET_LOGS) —
    the tool that triages every module can't be the one blind spot. Buffered
    while disconnected; drained by the relay task once connected."""

    def __init__(self, buf: "collections.deque"):
        super().__init__(level=logging.INFO)
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{record.levelname}] "
                f"{record.name}: {self.format(record)}")
        except Exception:
            pass


# Reconnect backoff (seconds) after a lost/failed connection.
_RECONNECT_DELAY = 5
_HEARTBEAT_INTERVAL = 30
_HANDSHAKE_TIMEOUT = 5.0


class MessageSigner:
    """HMAC-SHA256 over the canonical JSON of a message (signature excluded).

    Mirrors lm/core/src/security/signer.py exactly: recursively sort dict keys,
    serialize with separators=(',',':'). Python's json.dumps(sort_keys=True)
    sorts nested dict keys recursively too, so this matches the Hub's verify
    path (main.py: json.dumps(data, sort_keys=True, separators=(',',':'))).
    """

    def __init__(self, secret: str):
        self.secret = secret

    @staticmethod
    def _canonicalize(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: MessageSigner._canonicalize(obj[k]) for k in sorted(obj.keys())}
        if isinstance(obj, list):
            return [MessageSigner._canonicalize(i) for i in obj]
        return obj

    def sign(self, msg: Dict[str, Any]) -> str:
        data = {k: v for k, v in msg.items() if k != "signature"}
        canonical = self._canonicalize(data)
        message_bytes = json.dumps(canonical, separators=(",", ":")).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify(self, msg: Dict[str, Any]) -> bool:
        sig = msg.get("signature")
        if not sig:
            return False
        return hmac.compare_digest(self.sign(msg), sig)

    def sign_bytes(self, message_bytes: bytes) -> str:
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def verify_bytes(self, message_bytes: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign_bytes(message_bytes), signature)


def encode_frame(signer, msg: Dict[str, Any]) -> str:
    """Wire form ``<sig>.<body>`` — body serialized ONCE, sig over those exact
    bytes; byte-identical to lm/core so the Hub verifies received bytes directly."""
    body = json.dumps(msg, separators=(",", ":"))
    sig = signer.sign_bytes(body.encode()) if signer is not None else ""
    return sig + "." + body


def split_frame(wire: str):
    """Split ``<sig>.<body>`` → (sig, body) on the FIRST '.'; unsigned = ('', body)."""
    sig, sep, body = wire.partition(".")
    return ("", wire) if not sep else (sig, body)


def _normalize_hub_ws_url(url: Optional[str]) -> Optional[str]:
    """Fill in a pinned HUB_WS_URL's scheme/port/path with sane defaults.

    Mirrors ``BaseControlPlane._normalize_hub_url`` (core/src/messaging/
    control_plane.py) — this module is intentionally self-contained and
    doesn't import the lm package, so the logic is ported rather than shared.
    ``websockets.connect()`` dials the URL verbatim; a bare host or a URL
    missing the ``/ws/spoke`` path connects to the wrong thing. Defaults:
    scheme -> ``wss``, port -> ``443``, path -> ``/ws/spoke`` (only when the
    port is 443 — a pin to some other, explicitly-given port is assumed to be
    a legacy raw-socket listener with no path routing, so it's left alone).
    Empty/``None`` pass through unchanged.
    """
    if not url:
        return url
    raw = url.strip()
    if "://" not in raw:
        raw = "wss://" + raw
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(raw)
    except Exception:
        return url
    scheme = parts.scheme or "wss"
    netloc = parts.netloc
    host_part = netloc.rsplit("]", 1)[-1] if netloc else netloc
    if netloc and ":" not in host_part:
        netloc = f"{netloc}:443"
        port = 443
    else:
        port = parts.port
    if scheme == "ws" and port == 443:
        scheme = "wss"
    path = parts.path
    if port == 443 and path in ("", "/"):
        path = "/ws/spoke"
    elif path not in ("", "/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


class HubAgentClient:
    """Persistent Hub WebSocket agent for BugFixer.

    Runs an asyncio event loop in a daemon thread. Sync callers use
    request_sync(); the loop handles connect/auth/heartbeat/receive.
    """

    def __init__(
        self,
        hub_ws_url: str,
        spoke_id: str,
        secret: str = "",
        hub_secret: str = "",
        on_status: Optional[Callable[[str, str], None]] = None,
        on_secret: Optional[Callable[[str], None]] = None,
        on_hub_secret: Optional[Callable[[str], None]] = None,
    ):
        self.hub_ws_url = _normalize_hub_ws_url(hub_ws_url)
        self.spoke_id = spoke_id
        self.secret = secret or ""
        self.hub_secrets = [hub_secret] if hub_secret else []
        self.on_status = on_status or (lambda _s, _m: None)
        self.on_secret = on_secret or (lambda _s: None)
        self.on_hub_secret = on_hub_secret or (lambda _s: None)

        # wss:// TLS to the unified :443 hub. Default: encrypt WITHOUT authenticating
        # the self-signed hub cert (matches BaseControlPlane._client_ssl_ctx); set
        # LM_HUB_TLS_VERIFY=1 + LM_HUB_CA_CERT to verify against a shipped CA.
        self._tls_verify = os.environ.get("LM_HUB_TLS_VERIFY", "0") == "1"
        self._tls_ca_cert = (os.environ.get("LM_HUB_CA_CERT", "") or "").strip()
        # mTLS CLIENT identity for the wss connection, installed by the hub's
        # cert-distribution (INSTALL_CERT, LE-issued). When both files exist the
        # SSL context presents them so the hub can mutually-authenticate bugfixer
        # — required by hubs that gate data reads on a client cert.
        self._client_cert_file = (os.environ.get("BUGFIXER_HUB_CLIENT_CERT")
                                  or "/etc/bugfixer/hub-client-cert.pem")
        self._client_key_file = (os.environ.get("BUGFIXER_HUB_CLIENT_KEY")
                                 or "/etc/bugfixer/hub-client-key.pem")
        # Present the client cert whenever it exists — the hub's reverse
        # HUB_REQUEST channel (log reads + fleet update triggers) is authorized
        # ONLY for the connection that presents the pinned BugFixer cert over mTLS
        # (_hub_request_authorized), so bugfixer MUST present it to read hub data.
        # BUGFIXER_HUB_MTLS=0 force-disables (debug). But an UNTRUSTED cert breaks
        # the wss handshake entirely, so _present_cert is a runtime fallback: a
        # handshake failure while presenting flips it off for the next attempt
        # (recover the session-key connection so a corrected cert can be
        # re-deployed), and INSTALL_CERT flips it back on to try the fresh cert.
        self._hub_mtls = os.environ.get("BUGFIXER_HUB_MTLS", "1") != "0"
        self._present_cert = True
        self._presented_cert = False   # was a cert presented on the current attempt?
        self._cert_ever_worked = False  # did a handshake ever SUCCEED while presenting the cert?

        self.signer = MessageSigner(self.secret) if self.secret else None
        # Pending request Futures keyed by the request header.message_id.
        self._pending: Dict[str, asyncio.Future] = {}
        self._ws = None
        self._approved = bool(self.secret)  # optimistic; corrected by Hub messages
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Log relay to the hub (contract reqs 1-4). Handler installed ONCE on the
        # root logger; records buffer here while disconnected and are drained as
        # SPOKE_LOG by _log_relay_task once connected. Bounded ring (drop-oldest)
        # so a long hub outage keeps the most recent lines.
        self._log_relay_buf: "collections.deque" = collections.deque(maxlen=1000)
        _relay_handler = _HubLogRelayHandler(self._log_relay_buf)
        _relay_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(_relay_handler)
        self._install_uncaught_exception_relay()

    # -------------------------------------------------------------- log relay

    def _install_uncaught_exception_relay(self) -> None:
        """Route uncaught SYNC exceptions through the HubAgent logger (→ relay →
        hub) before the interpreter's default handler. asyncio counterpart set
        in _run(). Contract req 4."""
        _prev = sys.excepthook

        def _hook(exc_type, exc, tb):
            try:
                if not issubclass(exc_type, KeyboardInterrupt):
                    logger.error("Uncaught exception", exc_info=(exc_type, exc, tb))
            finally:
                _prev(exc_type, exc, tb)

        sys.excepthook = _hook

    def _asyncio_exception_relay(self, loop, context) -> None:
        """asyncio loop exception handler — relays unhandled task exceptions
        through the HubAgent logger then defers to the default handler."""
        exc = context.get("exception")
        msg = context.get("message") or "unhandled asyncio exception"
        if exc is not None:
            logger.error("Uncaught asyncio exception: %s", msg, exc_info=exc)
        else:
            logger.error("asyncio error: %s", msg)
        loop.default_exception_handler(context)

    async def _log_relay_task(self, websocket) -> None:
        """Drain the buffered log records to the hub as signed SPOKE_LOG frames
        every 5s. The hub registers this agent in active_connections (it auths
        as a spoke), so SPOKE_LOG lands in agent_logs[spoke_id] → Error Log +
        GET_LOGS."""
        while True:
            await asyncio.sleep(5)
            entries = []
            while self._log_relay_buf and len(entries) < 300:
                entries.append(self._log_relay_buf.popleft())
            if not entries:
                continue
            try:
                msg = {
                    "header": {"message_id": str(uuid.uuid4()), "timestamp": time.time(),
                               "sender_id": self.spoke_id, "destination_id": "hub"},
                    "payload": {"type": "SPOKE_LOG", "data": {"entries": entries}},
                }
                await websocket.send(encode_frame(self.signer, msg))
            except Exception as e:  # noqa: BLE001
                logger.debug("BugFixer log relay send failed: %s", e)

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Start the client in a background daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._run())
            except Exception as e:  # pragma: no cover — logged for diagnostics
                logger.error("Hub agent loop exited: %s", e)
                self.on_status("error", f"agent loop exited: {e}")
            finally:
                self.loop.close()

        self._thread = threading.Thread(target=_runner, name="hub-agent", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._cancel_all(), self.loop)

    async def _cancel_all(self):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("agent shutting down"))
        self._pending.clear()

    def request_sync(self, req_type: str, data: Optional[dict] = None, timeout: float = 20.0) -> Optional[dict]:
        """Thread-safe request. Returns the result dict, or None if the agent
        is not approved/connected (callers treat None as "skip, not 401")."""
        if not self.loop or not self._approved:
            return None
        try:
            coro = self.request(req_type, data or {}, timeout)
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            return future.result(timeout=timeout + 5)
        except Exception as e:
            logger.warning("Hub request '%s' failed: %s", req_type, e)
            return None

    async def request(self, req_type: str, data: dict, timeout: float = 20.0) -> dict:
        """Send a signed HUB_REQUEST and await the correlated HUB_RESPONSE."""
        if not self.signer or not self._ws:
            raise RuntimeError("agent not ready (no secret or no connection)")

        msg_id = str(uuid.uuid4())
        msg = {
            "header": {
                "message_id": msg_id,
                "timestamp": round(time.time(), 6),
                "sender_id": self.spoke_id,
                "destination_id": "hub",
            },
            "payload": {"type": "HUB_REQUEST", "data": {"type": req_type, **data}},
        }

        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(encode_frame(self.signer, msg))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    # ------------------------------------------------------------------ loop

    async def _run(self):
        # Relay unhandled asyncio-task exceptions through the logger → hub
        # (sync excepthook installed in __init__). Contract req 4.
        try:
            asyncio.get_running_loop().set_exception_handler(self._asyncio_exception_relay)
        except Exception:  # noqa: BLE001
            pass
        logger.info("Hub agent starting: %s -> %s", self.spoke_id, self.hub_ws_url)
        if self.secret:
            self.on_status("pending", "reconnecting with stored secret")
        else:
            self.on_status("not_registered", "awaiting admin approval")

        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
            except ConnectionClosedError as e:
                # Stale/rotated session secret: Hub sends 1008 "Authentication".
                # Clear the stored secret so the next retry reconnects zero-touch
                # and receives a freshly provisioned key from the Hub.
                if (
                    self.secret
                    and getattr(e, "rcvd", None)
                    and e.rcvd.code == 1008
                    and "Authentication" in (e.rcvd.reason or "")
                ):
                    logger.warning(
                        "Hub rejected secret for '%s' (stale/rotated key). "
                        "Clearing stored secret — next retry uses zero-touch provisioning.",
                        self.spoke_id,
                    )
                    self.secret = ""
                    self.signer = None
                    self._approved = False
                    self.on_secret("")
                    self.on_status("not_registered", "secret rejected — re-onboarding")
                else:
                    logger.warning("Hub connection closed (%s); reconnecting", e)
            except (OSError, Exception) as e:
                logger.warning("Hub connection error (%s); reconnecting in %ds", e, _RECONNECT_DELAY)
                # If we presented the client cert this attempt and the handshake
                # died before we ever connected WITH the cert, it is being REJECTED
                # (untrusted CA / not chained to the hub's mTLS CA). Drop it for the
                # next attempt so the session-key connection recovers instead of
                # hard-looping offline. Gate on _cert_ever_worked (NOT the secret) —
                # bugfixer keeps a persisted session secret across the transport-
                # layer cert, so the old `not self.secret` guard never fired and the
                # rejected cert stranded it. A restart or a fresh INSTALL_CERT
                # re-arms _present_cert to try again (e.g. after the hub is taught to
                # trust the BugFixer cert's CA).
                if self._presented_cert and not self._cert_ever_worked:
                    self._present_cert = False
                    logger.warning(
                        "wss: hub rejected our client cert (handshake failed) — "
                        "retrying WITHOUT it so the basic connection recovers. The hub "
                        "must trust the BugFixer cert's CA before mTLS log/update "
                        "access can work; re-deploy it from the LE module (tagged "
                        "BugFixer) or restart bugfixer to retry.")

            if self._stop.is_set():
                break
            await asyncio.sleep(_RECONNECT_DELAY)

    def _client_ssl_ctx(self):
        """SSL context for a ``wss://`` connect to the hub. Default: unverified
        (encrypt without authenticating the self-signed hub cert) — the exact
        private API used hub-wide (see memory ssl-create-unverified-context).
        Without this, ``websockets.connect`` builds a verifying context for a
        wss:// URI and the self-signed hub cert fails CERTIFICATE_VERIFY_FAILED."""
        try:
            if self._tls_verify and self._tls_ca_cert:
                ctx = ssl.create_default_context(cafile=self._tls_ca_cert)
            else:
                ctx = ssl._create_unverified_context()
            # Present the installed mTLS client cert (needed for HUB_REQUEST) —
            # unless a prior handshake failure flipped _present_cert off, so a
            # wrong/untrusted cert can't permanently brick connectivity.
            self._presented_cert = False
            if (self._hub_mtls and self._present_cert and ctx is not None
                    and os.path.exists(self._client_cert_file)
                    and os.path.exists(self._client_key_file)):
                try:
                    ctx.load_cert_chain(self._client_cert_file, self._client_key_file)
                    self._presented_cert = True
                    logger.info("wss: presenting hub mTLS client cert %s", self._client_cert_file)
                except Exception as e:  # noqa: BLE001
                    logger.warning("wss: could not load client cert (mTLS off): %s", e)
            return ctx
        except Exception as e:  # noqa: BLE001
            logger.error("Could not build wss SSL context: %s", e)
            return None

    async def _handle_install_cert(self, msg, data):
        """Install a hub-distributed (LE) cert. It serves TWO roles for bugfixer,
        both from the one deployment:
          1. the WebUI SERVER cert (BUGFIXER_SSL_CERT/KEY, default
             /etc/bugfixer/cert.pem+key.pem) — so browsers AND GitHub webhooks
             reach the bugfixer WebUI over a PUBLICLY-TRUSTED cert instead of the
             install-time self-signed one (GitHub rejects self-signed webhook URLs);
          2. our mTLS CLIENT identity for the hub connection (hub-client-cert.pem)
             — the cert the hub's HUB_REQUEST channel authorizes on (log reads +
             fleet update commands), gated by BugFixer SAN pinning.
        Write both, reply the COMMAND_RESULT the hub's request_response awaits, then
        trigger a graceful full-service restart so uvicorn reloads the new WebUI
        cert (a running listener can't hot-swap its cert) and the reconnect presents
        the new mTLS cert. Mirrors the INSTALL_CERT contract other cert-capable
        spokes implement (cert_distribution.py)."""
        status, message = "SUCCESS", "cert installed"
        try:
            fullchain = data.get("fullchain") or ""
            privkey = data.get("privkey") or ""
            if not fullchain or not privkey:
                status, message = "ERROR", "missing fullchain/privkey"
            else:
                fc = fullchain if fullchain.endswith("\n") else fullchain + "\n"
                pk = privkey if privkey.endswith("\n") else privkey + "\n"
                # 1. mTLS client identity for the hub connection.
                os.makedirs(os.path.dirname(self._client_cert_file) or ".", exist_ok=True)
                with open(self._client_cert_file, "w") as f:
                    f.write(fc)
                with open(self._client_key_file, "w") as f:
                    f.write(pk)
                try:
                    os.chmod(self._client_key_file, 0o600)
                except OSError:
                    pass
                # 2. WebUI server cert (so GitHub webhooks + browsers trust it).
                webui_cert = os.environ.get("BUGFIXER_SSL_CERT", "/etc/bugfixer/cert.pem")
                webui_key = os.environ.get("BUGFIXER_SSL_KEY", "/etc/bugfixer/key.pem")
                webui_written = False
                try:
                    os.makedirs(os.path.dirname(webui_cert) or ".", exist_ok=True)
                    with open(webui_cert, "w") as f:
                        f.write(fc)
                    with open(webui_key, "w") as f:
                        f.write(pk)
                    try:
                        os.chmod(webui_key, 0o600)
                    except OSError:
                        pass
                    webui_written = True
                except Exception as we:  # noqa: BLE001 — WebUI cert is best-effort
                    logger.warning("INSTALL_CERT: WebUI cert write failed (%s) — "
                                   "mTLS client cert still installed", we)
                # A freshly-deployed cert is worth trying even if a prior one was
                # rejected — re-arm the present-cert path so the restart's reconnect
                # presents THIS cert over mTLS.
                self._present_cert = True
                self._cert_ever_worked = False
                message = (f"cert installed for {data.get('domain') or '?'}"
                           f"{' (WebUI + mTLS)' if webui_written else ' (mTLS only)'}"
                           " — restarting to load it")
                logger.info("INSTALL_CERT: %s", message)
        except Exception as e:  # noqa: BLE001
            status, message = "ERROR", str(e)
            logger.warning("INSTALL_CERT failed: %s", e)
        # Reply the COMMAND_RESULT the hub awaits (correlation_id echoes the
        # inbound message_id; signed with our session secret).
        if self._ws is not None and self.signer is not None:
            reply = {
                "correlation_id": msg.get("header", {}).get("message_id"),
                "header": {"message_id": str(uuid.uuid4()), "timestamp": round(time.time(), 6),
                           "sender_id": self.spoke_id, "destination_id": "hub"},
                "payload": {"type": "COMMAND_RESULT", "data": {"status": status, "message": message}},
            }
            try:
                await self._ws.send(encode_frame(self.signer, reply))
            except Exception as e:  # noqa: BLE001
                logger.warning("INSTALL_CERT: failed to send result: %s", e)
        # Success: trigger a graceful full-service restart so uvicorn reloads the
        # new WebUI server cert (a live listener can't hot-swap its cert) AND the
        # reconnect presents the new mTLS client cert. Prefer the restart_worker's
        # deferred-restart flag (grace window + watchdog backstop, decoupled from
        # this task); if app_state isn't importable, fall back to just closing the
        # ws so at least the mTLS cert is re-presented on reconnect.
        if status == "SUCCESS":
            restarted = False
            try:
                import app_state
                app_state.state["restart_pending"] = True
                restarted = True
                logger.info("INSTALL_CERT: restart scheduled to load the new cert(s)")
            except Exception as e:  # noqa: BLE001
                logger.warning("INSTALL_CERT: could not schedule restart (%s) — "
                               "closing ws to at least re-present the mTLS cert", e)
            if not restarted and self._ws is not None:
                try:
                    await self._ws.close(code=1012, reason="reconnect to present new cert")
                except Exception:  # noqa: BLE001
                    pass

    async def _connect_and_serve(self):
        # max_size: the hub's GET_LOGS response aggregates every spoke's logs
        # and can exceed the default 1 MiB frame ceiling, which closed us with
        # code 1009 "message too big". Match the hub's 16 MiB ceiling so the
        # large GET_LOGS response arrives intact (the hub also self-caps its
        # payload under 12 MiB in collect_all_logs).
        # For wss:// pass an explicit SSL context (unverified by default) so the
        # self-signed hub cert doesn't fail the handshake; ws:// passes ssl=None.
        _ssl = self._client_ssl_ctx() if str(self.hub_ws_url or "").startswith("wss://") else None
        async with websockets.connect(self.hub_ws_url, max_size=16 * 1024 * 1024, ssl=_ssl) as websocket:
            self._ws = websocket
            # The wss handshake completed. If we presented the client cert, the hub
            # ACCEPTED it — remember that, so a later transient network drop doesn't
            # get mistaken for a cert rejection and disengage a working cert.
            if self._presented_cert:
                self._cert_ever_worked = True

            # 1. Spoke authentication handshake.
            # module_type "bugfixer" (NOT "agent"): bugfixer is a STANDALONE
            # module, not a generic multi-role node. Connecting as "agent" made
            # the hub treat it as a role-hosting agent — it hid from the plain
            # approval list, tried to LOAD_ROLE a (non-existent) bugfixer role
            # (UI stuck on "activating"), and diverted the session-key push so it
            # never armed. The hub reaches bugfixer by spoke_id ("bugfixer",
            # HUB_AGENT_ID) and broadcasts SET_LOG_LEVEL to ALL spokes, so a
            # distinct type keeps every integration working while it registers as
            # an ordinary approvable module.
            auth_payload = {"spoke_id": self.spoke_id, "module_type": "bugfixer"}
            sent_secret = bool(self.secret)
            if self.secret:
                auth_payload["secret"] = self.secret
            await websocket.send(json.dumps(auth_payload, separators=(",", ":")))
            logger.info("Connected to Hub as %s; performing mutual auth...", self.spoke_id)

            # 2. Hub mutual authentication (verify Hub's identity proof).
            try:
                hub_proof_json = await asyncio.wait_for(websocket.recv(), timeout=_HANDSHAKE_TIMEOUT)
                hub_proof = json.loads(hub_proof_json)
            except asyncio.TimeoutError:
                await websocket.close(1008, "Mutual authentication timed out")
                raise

            if hub_proof.get("status") != "HUB_VERIFIED":
                await websocket.close(1008, "Mutual authentication failed")
                raise RuntimeError(f"unexpected hub proof: {hub_proof.get('status')}")

            challenge = hub_proof.get("challenge", "")
            signature = hub_proof.get("signature", "")
            if self.hub_secrets:
                verified = False
                for hs in self.hub_secrets:
                    expected = hmac.new(hs.encode(), challenge.encode(), hashlib.sha256).hexdigest()
                    if hmac.compare_digest(expected, signature):
                        verified = True
                        break
                if not verified:
                    await websocket.close(1008, "Hub verification failed")
                    raise RuntimeError("hub identity verification failed")
            else:
                logger.warning("No hub secret configured — skipping Hub identity verification (insecure).")
            await websocket.send(json.dumps({"status": "HUB_OK"}, separators=(",", ":")))

            # We presented a stored secret and the hub sent HUB_VERIFIED without
            # closing 1008 "Authentication" — so it ACCEPTED our secret: we are an
            # already-approved spoke re-establishing its session. The hub only
            # (re)sends SPOKE_UPDATE_SESSION_KEY / APPROVED on a ZERO-TOUCH connect,
            # NOT when a valid secret is presented, so without this flip the status
            # set to "pending" at reconnect (line ~374) would stick forever and
            # bugfixer would keep suppressing its hub-log scans as if unapproved. A
            # stale secret self-corrects: the hub closes 1008 → the reconnect
            # handler clears the secret and re-onboards zero-touch.
            if sent_secret and self.secret:
                self._approved = True
                self.on_status("approved", "reconnected — session re-established")

            # 3. Heartbeat task (unsigned while pending so the Hub accepts it).
            async def heartbeat():
                while True:
                    try:
                        ts = round(time.time(), 6)
                        msg = {
                            "header": {"message_id": str(uuid.uuid4()), "timestamp": ts,
                                       "sender_id": self.spoke_id, "destination_id": "hub"},
                            "payload": {"type": "HEARTBEAT", "data": {}},
                        }
                        await websocket.send(encode_frame(self.signer, msg))
                    except Exception:
                        return
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)

            hb_task = asyncio.create_task(heartbeat())
            # Drain buffered logs (startup + reconnect gap) to the hub and keep
            # streaming while connected. Started per-connection; the capturing
            # handler (added in __init__) persists so nothing is lost between.
            lr_task = asyncio.create_task(self._log_relay_task(websocket))
            try:
                async for message in websocket:
                    # Wire form <sig>.<body>: verify the RECEIVED body bytes
                    # directly, parse once. Bootstrap (no secret yet) allows
                    # unsigned so onboarding frames pass.
                    sig, body = split_frame(message)
                    try:
                        msg = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if self.secret and self.signer and sig and not self.signer.verify_bytes(body.encode(), sig):
                        logger.warning("Invalid signature — dropping")
                        continue
                    await self._handle_message(msg)
            finally:
                hb_task.cancel()
                lr_task.cancel()
                await asyncio.gather(hb_task, lr_task, return_exceptions=True)
                self._ws = None

    # ------------------------------------------------------------------ dispatch

    def _verify(self, msg: dict) -> bool:
        """The HMAC is now verified at the receive loop over the RECEIVED body
        bytes (<sig>.<body> format) before dispatch — the parsed dict no longer
        carries a "signature" field, so re-verifying it here would always fail.
        Kept as an always-true hook for existing call sites."""
        return True

    def _read_version(self) -> str:
        """Read this agent's VERSION file (beside this module) for get_version
        replies. Falls back to ``"unknown"`` if unreadable."""
        try:
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
            with open(p) as f:
                return f.read().strip() or "unknown"
        except Exception:
            return "unknown"

    async def _handle_message(self, msg: dict) -> None:
        if not self._verify(msg):
            logger.warning("Invalid signature on inbound message; dropping.")
            return

        payload = msg.get("payload", {})
        cmd_type = payload.get("type")
        data = payload.get("data", {}) or {}

        if cmd_type == "APPROVAL_REQUIRED":
            self._approved = False
            self.on_status("pending", "pending admin approval — approve in Hub WebUI (Setup → Spoke Approvals)")
            return

        if cmd_type == "APPROVED":
            # Secret may already have arrived via SPOKE_UPDATE_SESSION_KEY;
            # approval just confirms it. Status flips to approved once we
            # have a secret (set in SPOKE_UPDATE_SESSION_KEY / on connect).
            if self.secret:
                self._approved = True
                self.on_status("approved", "approved by admin")
            else:
                self.on_status("pending", "approved — awaiting session key")
            return

        if cmd_type == "SPOKE_UPDATE_SESSION_KEY":
            new_secret = data.get("secret")
            if new_secret:
                self.secret = new_secret
                self.signer = MessageSigner(new_secret)
                self._approved = True
                self.on_secret(new_secret)
                self.on_status("approved", "session key received — zero-touch ready")
                logger.info("Session key received from Hub for %s", self.spoke_id)
            return

        if cmd_type == "INSTALL_CERT":
            await self._handle_install_cert(msg, data)
            return

        if cmd_type == "SPOKE_SET_HUB_SECRET":
            new_hub_secret = data.get("hub_secret")
            if new_hub_secret:
                self.hub_secrets.insert(0, new_hub_secret)
                self.hub_secrets = self.hub_secrets[:3]
                self.on_hub_secret(new_hub_secret)
                logger.info("Hub secret stored for %s", self.spoke_id)
            return

        if cmd_type == "HUB_RESPONSE":
            corr_id = data.get("correlation_id")
            result = data.get("result")
            fut = self._pending.pop(corr_id, None)
            if fut and not fut.done():
                fut.set_result(result)
            else:
                logger.debug("HUB_RESPONSE with unknown correlation_id: %s", corr_id)
            return

        if cmd_type == "DENIED":
            self._approved = False
            self.on_status("error", "approval revoked by admin")
            return

        if cmd_type in ("HELP_ASK", "help_ask"):
            # LLM-turn executor for the hub's Help "Ask" assistant. The HUB owns
            # the doc corpus, the tools, and the agentic loop; BugFixer just runs
            # ONE model turn (reusing its multi-provider call_llm) and returns the
            # normalized {content, tool_calls}. call_llm is sync → run off-thread
            # so we don't block the agent's WS receive loop. Reply mirrors the
            # get_version COMMAND_RESULT pattern (correlation_id echoes the inbound
            # message_id; the hub's request_response keys on it).
            if self._ws is None:
                return
            corr = msg.get("header", {}).get("message_id")
            try:
                from main import call_llm  # lazy: main imports this module
                msgs = data.get("messages") or []
                tools = data.get("tools") or None
                system = data.get("system") or "You are the Lab Manager help assistant."
                result, err = await asyncio.to_thread(
                    call_llm, "", system_prompt=system, messages=msgs, tools=tools)
                if err or not isinstance(result, dict):
                    rdata = {"status": "ERROR", "message": str(err or "no result from LLM")}
                else:
                    rdata = {"status": "SUCCESS", "assistant": {
                        "content": result.get("text") or "",
                        "tool_calls": result.get("tool_calls") or [],
                    }}
            except Exception as e:  # noqa: BLE001
                logger.warning("HELP_ASK failed: %s", e)
                rdata = {"status": "ERROR", "message": str(e)}
            reply = {
                "correlation_id": corr,
                "header": {
                    "message_id": str(uuid.uuid4()),
                    "timestamp": round(time.time(), 6),
                    "sender_id": self.spoke_id,
                    "destination_id": "hub",
                },
                "payload": {"type": "COMMAND_RESULT", "data": rdata},
            }
            try:
                await self._ws.send(encode_frame(self.signer, reply))
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to send HELP_ASK reply: %s", e)
            return

        if cmd_type in ("get_version", "GET_VERSION"):
            # Hub queries our version after approval (and now after a
            # post-connect admin approval). Reply with a signed COMMAND_RESULT
            # carrying data.version — the Hub stores it (main.py: COMMAND_RESULT
            # with a "version" key) so the Diagnostics page shows our .NN
            # instead of "unknown". The top-level correlation_id echoes the
            # inbound message_id (the Hub's ack path keys on it).
            if self._ws is None:
                return
            reply = {
                "correlation_id": msg.get("header", {}).get("message_id"),
                "header": {
                    "message_id": str(uuid.uuid4()),
                    "timestamp": round(time.time(), 6),
                    "sender_id": self.spoke_id,
                    "destination_id": "hub",
                },
                "payload": {
                    "type": "COMMAND_RESULT",
                    "data": {"status": "SUCCESS", "version": self._read_version()},
                },
            }
            try:
                await self._ws.send(encode_frame(self.signer, reply))
            except Exception as e:
                logger.warning("Failed to send get_version reply: %s", e)
            return

        if cmd_type in ("SET_LOG_LEVEL", "SPOKE_SET_LOG_LEVEL"):
            # WebUI "Enable Debug" broadcast. It's broadcast to every connected
            # spoke (any module_type), so bugfixer receives it while in
            # active_connections; without a handler here it silently dropped to
            # "Unhandled"
            # and the BugFixer/HubAgent loggers never flipped to DEBUG.
            enabled = bool(data.get("enabled", False))
            level = set_log_level(enabled)
            logger.info("Log level set to %s", logging.getLevelName(level))
            return

        logger.debug("Unhandled Hub message type: %s", cmd_type)


# Module-level singleton, started by BugFixer at app startup if HUB_WS_URL is set.
hub_agent_client: Optional[HubAgentClient] = None


def start_agent_from_config(config: dict, on_status=None, on_secret=None, on_hub_secret=None) -> Optional[HubAgentClient]:
    """Build and start the Hub agent from a BugFixer config dict.

    Returns the client (also stored as the module singleton), or None if
    HUB_WS_URL is not configured.
    """
    global hub_agent_client
    hub_ws_url = (config.get("HUB_WS_URL") or "").strip()
    if not hub_ws_url:
        return None
    spoke_id = (config.get("HUB_AGENT_ID") or "bugfixer").strip() or "bugfixer"
    secret = (config.get("HUB_AGENT_SECRET") or "").strip()
    hub_secret = (config.get("HUB_SECRET") or "").strip()
    client = HubAgentClient(
        hub_ws_url=hub_ws_url,
        spoke_id=spoke_id,
        secret=secret,
        hub_secret=hub_secret,
        on_status=on_status,
        on_secret=on_secret,
        on_hub_secret=on_hub_secret,
    )
    hub_agent_client = client
    client.start()
    return client
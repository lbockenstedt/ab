"""AppBuilder Hub agent — a self-contained WebSocket agent client.

This makes AppBuilder authenticate to the Lab Manager (LM) Hub the same way every
other spoke/agent does: zero-touch connect, admin approval in the Hub WebUI,
HMAC session-key exchange, then signed heartbeats + request/response messages
over a single persistent WebSocket. It replaces the old static-token HTTP
calls (LM_ADMIN_TOKEN / X-Admin-Token), which the Hub never actually honored.

The module is intentionally self-contained — it reimplements the Hub's signing
scheme (core/src/security/signer.py) and mirrors the connect/auth/heartbeat
handshake from core/src/messaging/control_plane.py without importing the lm
package, so AppBuilder can run on hosts that don't have the lm source tree.
"""

import asyncio
import collections
import errno
import hashlib
import hmac
import json
import logging
import os
import socket
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
# fallback because ab does NOT import lm/core (see module docstring) — the
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
    SPOKE_LOG. Per logging-observability-contract.md: the AppBuilder's own logs
    and crashes must reach the hub (Error Log + the AppBuilder's own GET_LOGS) —
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
# After the hub rejects our client cert we disengage it (connect on the session
# key). Retry the cert on a natural reconnect once this long has passed since the
# rejection, so ab auto-recovers mTLS once the hub is taught to trust the
# cert — no manual restart needed.
_CERT_RETRY_INTERVAL = 300

# Errnos that mean "nothing is listening / no route" — the hub is DOWN, not
# refusing us. An LM upgrade restarts the hub, so this is the ordinary case.
_HUB_DOWN_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH,
                    errno.ENETDOWN, errno.ETIMEDOUT, errno.EHOSTDOWN}


# Hub commands whose handlers do minutes of work (an LLM turn, a cross-repo
# GitHub dedup search). These MUST NOT be awaited inline in the receive loop:
# while the consumer is suspended nothing drains the socket, the library's
# receive queue (default max_queue=32) fills, it stops reading the transport,
# and a hub that restarts has its close frame left unread — the socket parks in
# CLOSE-WAIT and the agent never notices it was disconnected. Running the work
# off-thread (run_in_executor/to_thread) frees the EVENT LOOP but not the
# CONSUMER, so it does not help; the dispatch itself has to be concurrent.
# Everything not listed here stays inline, because ordering matters for the
# onboarding/approval frames (APPROVED, SPOKE_UPDATE_SESSION_KEY, ...).
_SLOW_CMDS = frozenset({"ANALYZE_LOGS", "ESCALATE_LOG_ISSUE", "HELP_ASK", "help_ask"})
# Backstop against a flood of slow commands spawning unbounded tasks. Real
# traffic runs 1-2 concurrently; at the cap we drop and let the hub's durable
# mailbox redeliver, rather than stall the reader (the bug we're fixing).
_MAX_INFLIGHT_HANDLERS = 8


def _hub_unreachable(exc: BaseException) -> bool:
    """True when the connection failed because the hub never answered.

    The cert-rejection heuristic below must fire ONLY when the hub answered and
    refused our client certificate. Without this split, a hub that is simply
    restarting (every LM upgrade → connection refused) is misread as "our cert
    was rejected": ab disengages mTLS and then won't retry the cert for
    _CERT_RETRY_INTERVAL (5 min), turning a routine restart into a five-minute
    mTLS outage.

    An ssl.SSLError is deliberately NOT unreachable — the TLS layer only speaks
    when something IS listening, so it stays a rejection candidate (as does a
    reset mid-handshake, which is how a server commonly refuses a client cert).
    """
    if isinstance(exc, (asyncio.TimeoutError, socket.gaierror, socket.timeout)):
        return True                      # timed out / DNS — hub never answered
    if isinstance(exc, ssl.SSLError):
        return False                     # TLS spoke → the hub is up
    if isinstance(exc, OSError) and exc.errno in _HUB_DOWN_ERRNOS:
        return True
    return False


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
    """Persistent Hub WebSocket agent for AppBuilder.

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
        on_connection: Optional[Callable[[bool], None]] = None,
    ):
        self.hub_ws_url = _normalize_hub_ws_url(hub_ws_url)
        self.spoke_id = spoke_id
        self.secret = secret or ""
        self.hub_secrets = [hub_secret] if hub_secret else []
        self.on_status = on_status or (lambda _s, _m: None)
        self.on_secret = on_secret or (lambda _s: None)
        # Fired with the LIVE socket state (True on a completed handshake, False when
        # the connection drops) so the UI can show whether ab is ACTUALLY
        # connected right now — distinct from on_status, which reports the
        # registration state (approved/pending) that persists across brief drops.
        self.on_connection = on_connection or (lambda _c: None)
        self.on_hub_secret = on_hub_secret or (lambda _s: None)

        # wss:// TLS to the unified :443 hub. Default: encrypt WITHOUT authenticating
        # the self-signed hub cert (matches BaseControlPlane._client_ssl_ctx); set
        # LM_HUB_TLS_VERIFY=1 + LM_HUB_CA_CERT to verify against a shipped CA.
        self._tls_verify = os.environ.get("LM_HUB_TLS_VERIFY", "0") == "1"
        self._tls_ca_cert = (os.environ.get("LM_HUB_CA_CERT", "") or "").strip()
        # mTLS CLIENT identity for the wss connection: a Hub-Local-CA clientAuth
        # cert delivered by SPOKE_SET_MTLS_CLIENT_CERT (NOT the LE WebUI cert —
        # public CAs can't issue a cert that chains to the hub's client CA, so an
        # LE cert presented here is rejected at the handshake). When both files
        # exist the SSL context presents them so the hub can mutually-authenticate
        # ab — required by hubs that gate data reads (HUB_REQUEST log/update) on a
        # pinned client cert.
        self._client_cert_file = (os.environ.get("AB_HUB_CLIENT_CERT")
                                  or "/etc/ab/hub-client-cert.pem")
        self._client_key_file = (os.environ.get("AB_HUB_CLIENT_KEY")
                                 or "/etc/ab/hub-client-key.pem")
        # Present the client cert whenever it exists — the hub's reverse
        # HUB_REQUEST channel (log reads + fleet update triggers) is authorized
        # ONLY for the connection that presents the pinned AppBuilder cert over mTLS
        # (_hub_request_authorized), so ab MUST present it to read hub data.
        # AB_HUB_MTLS=0 force-disables (debug). But an UNTRUSTED cert breaks
        # the wss handshake entirely, so _present_cert is a runtime fallback: a
        # handshake failure while presenting flips it off for the next attempt
        # (recover the session-key connection so a corrected cert can be
        # re-deployed), and INSTALL_CERT flips it back on to try the fresh cert.
        self._hub_mtls = os.environ.get("AB_HUB_MTLS", "1") != "0"
        self._present_cert = True
        self._presented_cert = False   # was a cert presented on the current attempt?
        self._cert_ever_worked = False  # did a handshake ever SUCCEED while presenting the cert?
        self._last_conn_error = ""      # last hub-connection error (surfaced on Diagnostics)
        self._cert_rejected = False     # did the hub reject our client cert (handshake died)?
        self._cert_rejected_at = None   # unix ts of the last cert rejection

        self.signer = MessageSigner(self.secret) if self.secret else None
        # Pending request Futures keyed by the request header.message_id.
        self._pending: Dict[str, asyncio.Future] = {}
        # Long-running handlers dispatched off the receive loop (see _SLOW_CMDS).
        # Held so they aren't garbage-collected mid-flight, and so the in-flight
        # count can be capped.
        self._inflight: set = set()
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
                logger.debug("AppBuilder log relay send failed: %s", e)

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

    async def _handle_analyze_logs(self, msg, data):
        """The LM hub has no LLM of its own, so it delegates Log Analysis to us (we
        own the models + already read hub logs). Run analyze_logs off the event-loop
        thread and reply with the result; correlation_id lets the hub's
        request_response match the answer. The hub must use a long timeout (LLM).

        latency_sensitive=False here (unlike routes.py's synchronous Log Analysis
        panel caller): this runs on a background executor thread with no human
        blocked on the reply, only the hub's own long request timeout."""
        import asyncio as _aio
        from llm_client import analyze_logs, parse_log_verdict, is_llm_cooldown_error
        from model_selection import LlmRequirements
        title = str(data.get("title") or "logs")[:200]
        log_text = data.get("logs") or ""
        status, analysis, verdict, error = "ok", "", "none", None
        try:
            if not str(log_text).strip():
                status, error = "error", "no logs provided"
            else:
                reqs = LlmRequirements(complexity="small", latency_sensitive=False,
                                       deprioritize_local=True,
                                       min_context_tokens=len(log_text) // 4)
                raw = await _aio.get_event_loop().run_in_executor(
                    None, lambda: analyze_logs(log_text, title, requirements=reqs))
                verdict, analysis = parse_log_verdict(raw)
        except Exception as e:  # noqa: BLE001
            status = "error"
            error = (f"all LLM providers are cooling down / unavailable: {e}"
                     if is_llm_cooldown_error(e) else str(e))
            logger.warning("ANALYZE_LOGS failed: %s", e)
        if self._ws is None or self.signer is None:
            return
        reply = {
            "correlation_id": msg.get("header", {}).get("message_id"),
            "header": {"message_id": str(uuid.uuid4()), "timestamp": round(time.time(), 6),
                       "sender_id": self.spoke_id, "destination_id": "hub"},
            "payload": {"type": "COMMAND_RESULT",
                        "data": {"status": status, "analysis": analysis, "verdict": verdict,
                                 "error": error, "title": title}},
        }
        try:
            await self._ws.send(encode_frame(self.signer, reply))
        except Exception as e:  # noqa: BLE001
            logger.warning("ANALYZE_LOGS reply send failed: %s", e)

    async def _handle_escalate_log_issue(self, msg, data):
        """Tier-2: the LM hub's log sentinel flagged a real problem in a module and
        escalated it here for deep triage. File a GitHub issue (module repo, deduped)
        so the RepoScan -> fix pipeline engages; AppBuilder's fix step can pull ALL logs.
        Runs off-thread (GitHub I/O) and replies with the filing outcome."""
        import asyncio as _aio
        module = str(data.get("module") or "hub")
        log_slice = data.get("logs") or data.get("log_slice") or ""
        analysis = data.get("analysis") or ""
        verdict = data.get("verdict") or "escalate"
        status, detail = "ok", ""
        try:
            from log_scan import file_escalated_issue
            ok, detail = await _aio.get_event_loop().run_in_executor(
                None, lambda: file_escalated_issue(module, log_slice, analysis, verdict))
            status = "ok" if ok else "error"
            logger.info("ESCALATE_LOG_ISSUE module=%s -> %s (%s)", module,
                        "filed" if ok else "not filed", detail)
        except Exception as e:  # noqa: BLE001
            status, detail = "error", str(e)
            logger.warning("ESCALATE_LOG_ISSUE failed for module=%s: %s", module, e)
        if self._ws is None or self.signer is None:
            return
        reply = {
            "correlation_id": msg.get("header", {}).get("message_id"),
            "header": {"message_id": str(uuid.uuid4()), "timestamp": round(time.time(), 6),
                       "sender_id": self.spoke_id, "destination_id": "hub"},
            "payload": {"type": "COMMAND_RESULT",
                        "data": {"status": status, "detail": detail, "module": module}},
        }
        try:
            await self._ws.send(encode_frame(self.signer, reply))
        except Exception as e:  # noqa: BLE001
            logger.warning("ESCALATE_LOG_ISSUE reply send failed: %s", e)

    async def _ack(self, msg, status="SUCCESS", message=""):
        """Send a COMMAND_RESULT the hub's mailbox treats as an acknowledgement
        (correlation_id = the inbound message_id), so a DURABLE push is cleared and
        not retried to exhaustion. Signed with our session secret; a no-op if we
        have no signer/connection yet."""
        if self._ws is None or self.signer is None:
            return
        reply = {
            "correlation_id": msg.get("header", {}).get("message_id"),
            "header": {"message_id": str(uuid.uuid4()), "timestamp": round(time.time(), 6),
                       "sender_id": self.spoke_id, "destination_id": "hub"},
            "payload": {"type": "COMMAND_RESULT", "data": {"status": status, "message": message}},
        }
        try:
            await self._ws.send(encode_frame(self.signer, reply))
        except Exception as e:  # noqa: BLE001
            logger.warning("ack send failed: %s", e)

    def cert_diagnostics(self):
        """Runtime hub-connection + mTLS cert state for the Diagnostics page, so
        an operator sees WHY hub-log/update access is or isn't working (and whether
        the WebUI is still on the self-signed cert) without SSHing to the box.

        log_access is the honest signal: the current connection is presenting a
        cert the hub accepted. HUB_REQUEST also requires the hub to have SAN-pinned
        this cert as the AppBuilder identity — that half lives on the hub and can't
        be observed from here, so we report 'mTLS active', not 'authorized'."""
        def _cert_info(path):
            info = {"path": path, "exists": bool(path and os.path.exists(path))}
            if not info["exists"]:
                return info
            try:
                info["mtime"] = os.path.getmtime(path)
            except OSError:
                pass
            try:
                with open(path, "r") as f:
                    info["chain_len"] = f.read().count("-----BEGIN CERTIFICATE-----")
            except Exception:  # noqa: BLE001
                pass
            try:
                from cryptography import x509
                with open(path, "rb") as f:
                    cert = x509.load_pem_x509_certificate(f.read())
                subj = cert.subject.rfc4514_string()
                iss = cert.issuer.rfc4514_string()
                info["subject"] = subj
                info["issuer"] = iss
                info["self_signed"] = (subj == iss)
                try:
                    info["not_after"] = cert.not_valid_after_utc.isoformat()
                except Exception:  # noqa: BLE001 - older cryptography
                    info["not_after"] = cert.not_valid_after.isoformat()
                try:
                    san = cert.extensions.get_extension_for_class(
                        x509.SubjectAlternativeName).value
                    info["sans"] = [n.value for n in san]
                except Exception:  # noqa: BLE001 - no SAN ext
                    info["sans"] = []
                # Extended Key Usage — the crux of "verifies as a chain but rejected
                # as a CLIENT cert". A server verifying a client cert enforces the
                # clientAuth purpose; a serverAuth-only cert fails mTLS even though
                # its chain is valid. No EKU extension = usable for any purpose.
                _eku_names = {"1.3.6.1.5.5.7.3.1": "serverAuth",
                              "1.3.6.1.5.5.7.3.2": "clientAuth"}
                try:
                    eku = cert.extensions.get_extension_for_class(
                        x509.ExtendedKeyUsage).value
                    info["eku"] = [_eku_names.get(o.dotted_string, o.dotted_string)
                                   for o in eku]
                    info["client_auth"] = "1.3.6.1.5.5.7.3.2" in [o.dotted_string for o in eku]
                except Exception:  # noqa: BLE001 - no EKU ext = any purpose OK
                    info["eku"] = []
                    info["client_auth"] = True
                # Full chain breakdown: subject/issuer of EVERY cert in the file
                # (leaf -> intermediate(s) -> root), so the Diagnostics page shows
                # what the ROOT is — the whole question of whether the issuing CA is
                # a public one the hub's system store trusts, or a private/internal
                # CA it can never trust via the system store. Parse each PEM block
                # (load_pem_x509_certificates isn't in older cryptography).
                try:
                    with open(path, "rb") as f:
                        raw = f.read()
                    end = b"-----END CERTIFICATE-----"
                    chain = []
                    for seg in raw.split(end):
                        if b"-----BEGIN CERTIFICATE-----" not in seg:
                            continue
                        try:
                            c = x509.load_pem_x509_certificate(seg + end)
                            s = c.subject.rfc4514_string()
                            i = c.issuer.rfc4514_string()
                            chain.append({"subject": s, "issuer": i,
                                          "self_signed": s == i})
                        except Exception:  # noqa: BLE001
                            continue
                    info["chain"] = chain
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001 - cryptography missing / parse error
                info["parse_error"] = str(e)
            return info

        webui_cert = os.environ.get("AB_SSL_CERT", "/etc/ab/cert.pem")
        connected = self._ws is not None
        return {
            "available": True,
            "hub_ws_url": self.hub_ws_url,
            "connected": connected,
            "mtls_enabled": self._hub_mtls,
            "present_cert": self._present_cert,
            "presented_this_attempt": self._presented_cert,
            "cert_ever_worked": self._cert_ever_worked,
            "cert_rejected": self._cert_rejected,
            "cert_rejected_at": self._cert_rejected_at,
            "last_error": self._last_conn_error,
            # mTLS is active on the live connection only if we presented the cert
            # this attempt AND we're still connected (a rejected cert is disengaged).
            "mtls_active": bool(connected and self._presented_cert),
            "client_cert": _cert_info(self._client_cert_file),
            "webui_cert": _cert_info(webui_cert),
        }

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
            # Auto-retry a previously-rejected client cert on this reconnect once
            # enough time has passed — so mTLS log/update access self-heals after
            # the hub is taught to trust the cert, without a manual ab restart.
            if (not self._present_cert and self._cert_rejected and self._cert_rejected_at
                    and (time.time() - self._cert_rejected_at) > _CERT_RETRY_INTERVAL):
                self._present_cert = True
                logger.info("wss: re-arming the mTLS client cert to retry "
                            "(>%ds since the last rejection)", _CERT_RETRY_INTERVAL)
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
                self._last_conn_error = str(e)
                # If we presented the client cert this attempt and the handshake
                # died before we ever connected WITH the cert, it is being REJECTED
                # (untrusted CA / not chained to the hub's mTLS CA). Drop it for the
                # next attempt so the session-key connection recovers instead of
                # hard-looping offline. Gate on _cert_ever_worked (NOT the secret) —
                # ab keeps a persisted session secret across the transport-
                # layer cert, so the old `not self.secret` guard never fired and the
                # rejected cert stranded it. A restart or a fresh INSTALL_CERT
                # re-arms _present_cert to try again (e.g. after the hub is taught to
                # trust the AppBuilder cert's CA).
                # ...but ONLY when the hub actually answered. A hub that is down or
                # restarting (every LM upgrade) refuses the connection outright,
                # which is not a verdict on our certificate — treating it as one
                # disengaged mTLS and then blocked the retry for _CERT_RETRY_INTERVAL.
                if self._presented_cert and not self._cert_ever_worked and not _hub_unreachable(e):
                    self._present_cert = False
                    self._cert_rejected = True
                    self._cert_rejected_at = time.time()
                    logger.warning(
                        "wss: hub rejected our client cert (handshake failed) — "
                        "retrying WITHOUT it so the basic connection recovers. The hub "
                        "must trust the AppBuilder cert's CA before mTLS log/update "
                        "access can work; re-deploy it from the LE module (tagged "
                        "AppBuilder) or restart ab to retry.")

            # _connect_and_serve returned/raised → the socket is down. Mark the live
            # connection false so the UI reflects the drop immediately (the
            # registration status set above is intentionally left as-is).
            self.on_connection(False)
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

    async def _handle_clear_mtls_client_cert(self, msg):
        """Revocation: delete our mTLS client cert + stop presenting it (reconnect
        cert-less). The WebUI/LE cert is untouched. HUB_REQUEST access ends."""
        try:
            for p in (self._client_cert_file, self._client_key_file):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
            self._present_cert = False
            self._cert_ever_worked = False
            logger.info("SPOKE_CLEAR_MTLS_CLIENT_CERT: mTLS client cert cleared (revoked)")
        except Exception as e:  # noqa: BLE001
            logger.warning("SPOKE_CLEAR_MTLS_CLIENT_CERT failed: %s", e)
        await self._ack(msg, "SUCCESS", "mtls client cert cleared")
        if self._ws is not None:
            try:
                await self._ws.close(code=1012, reason="reconnect cert-less (revoked)")
            except Exception:  # noqa: BLE001
                pass

    async def _handle_set_mtls_client_cert(self, msg, data):
        """Install the Hub-Local-CA clientAuth cert the hub minted for our mTLS
        CLIENT identity (public CAs no longer issue clientAuth). Written to the mTLS
        client paths ONLY — the WebUI/server cert stays the LE cert. Ack, then
        reconnect to present it. This is what finally makes ab's HUB_REQUEST
        channel (hub logs + fleet updates) authorize: the hub trusts its own CA and
        SAN-pins us."""
        status, message = "SUCCESS", "mtls client cert installed"
        try:
            cert = data.get("cert") or ""
            key = data.get("key") or ""
            if not cert or not key:
                status, message = "ERROR", "missing cert/key"
            else:
                fc = cert if cert.endswith("\n") else cert + "\n"
                pk = key if key.endswith("\n") else key + "\n"
                os.makedirs(os.path.dirname(self._client_cert_file) or ".", exist_ok=True)
                with open(self._client_cert_file, "w") as f:
                    f.write(fc)
                with open(self._client_key_file, "w") as f:
                    f.write(pk)
                try:
                    os.chmod(self._client_key_file, 0o600)
                except OSError:
                    pass
                self._present_cert = True
                self._cert_ever_worked = False
                self._cert_rejected = False
                message = "hub-CA mTLS client cert installed — reconnecting to present it"
                logger.info("SPOKE_SET_MTLS_CLIENT_CERT: %s", message)
        except Exception as e:  # noqa: BLE001
            status, message = "ERROR", str(e)
            logger.warning("SPOKE_SET_MTLS_CLIENT_CERT failed: %s", e)
        await self._ack(msg, status, message)
        if status == "SUCCESS" and self._ws is not None:
            try:
                await self._ws.close(code=1012, reason="reconnect to present hub-CA mTLS cert")
            except Exception:  # noqa: BLE001
                pass

    async def _handle_install_cert(self, msg, data):
        """Install a hub-distributed (LE) cert as the WebUI SERVER cert
        (AB_SSL_CERT/KEY, default /etc/ab/cert.pem+key.pem) — so browsers AND
        GitHub webhooks reach the ab WebUI over a PUBLICLY-TRUSTED cert instead of
        the install-time self-signed one (GitHub rejects self-signed webhook URLs).

        This does NOT touch the hub mTLS CLIENT identity. That is a SEPARATE,
        Hub-Local-CA clientAuth cert delivered by SPOKE_SET_MTLS_CLIENT_CERT
        (hub-client-cert.pem) — public/LE CAs can't issue a cert that chains to the
        hub's client CA, so writing the LE cert there would clobber the valid
        local-CA cert and make the hub REJECT our wss handshake, killing the
        HUB_REQUEST channel (hub-log reads + fleet update triggers).

        Write the WebUI cert, reply the COMMAND_RESULT the hub's request_response
        awaits, then trigger a graceful full-service restart so uvicorn reloads the
        new cert (a running listener can't hot-swap its cert). Mirrors the
        INSTALL_CERT contract other cert-capable spokes implement
        (cert_distribution.py)."""
        status, message = "SUCCESS", "cert installed"
        try:
            fullchain = data.get("fullchain") or ""
            privkey = data.get("privkey") or ""
            if not fullchain or not privkey:
                status, message = "ERROR", "missing fullchain/privkey"
            else:
                fc = fullchain if fullchain.endswith("\n") else fullchain + "\n"
                pk = privkey if privkey.endswith("\n") else privkey + "\n"
                # Ensure the intermediate is bundled. The system trust store holds
                # only ROOTS (ISRG), not LE intermediates, so a verifier can only
                # build leaf -> intermediate -> root if WE present the intermediate.
                # If the hub/LE sent the chain separately and fullchain didn't
                # already include it, append it — otherwise browsers show an
                # incomplete chain for the WebUI cert.
                chain = data.get("chain") or ""
                if chain.strip() and chain.strip() not in fc:
                    fc = fc + (chain if chain.endswith("\n") else chain + "\n")
                # WebUI server cert ONLY (so GitHub webhooks + browsers trust it).
                # Do NOT write the hub mTLS client cert here — see docstring.
                webui_cert = os.environ.get("AB_SSL_CERT", "/etc/ab/cert.pem")
                webui_key = os.environ.get("AB_SSL_KEY", "/etc/ab/key.pem")
                os.makedirs(os.path.dirname(webui_cert) or ".", exist_ok=True)
                with open(webui_cert, "w") as f:
                    f.write(fc)
                with open(webui_key, "w") as f:
                    f.write(pk)
                try:
                    os.chmod(webui_key, 0o600)
                except OSError:
                    pass
                message = (f"WebUI cert installed for {data.get('domain') or '?'}"
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
        # ping_interval/ping_timeout: without these, websockets uses ~20s defaults —
        # so if ab's event loop stalls during heavy work (a scan/fix/LLM call),
        # the keepalive pong is late and the hub connection is dropped, causing the
        # "keeps going offline" flap. Generous values tolerate transient stalls while
        # still detecting a truly dead socket within ~2 min.
        async with websockets.connect(self.hub_ws_url, max_size=16 * 1024 * 1024,
                                      ping_interval=30, ping_timeout=90, ssl=_ssl) as websocket:
            self._ws = websocket
            # The wss handshake completed. If we presented the client cert, the hub
            # ACCEPTED it — remember that, so a later transient network drop doesn't
            # get mistaken for a cert rejection and disengage a working cert.
            if self._presented_cert:
                self._cert_ever_worked = True
                self._cert_rejected = False
            self._last_conn_error = ""   # handshake succeeded — clear the last error
            self.on_connection(True)     # socket is LIVE now (distinct from approval)

            # 1. Spoke authentication handshake.
            # module_type "ab" (NOT "agent"): ab is a STANDALONE
            # module, not a generic multi-role node. Connecting as "agent" made
            # the hub treat it as a role-hosting agent — it hid from the plain
            # approval list, tried to LOAD_ROLE a (non-existent) ab role
            # (UI stuck on "activating"), and diverted the session-key push so it
            # never armed. The hub reaches ab by spoke_id ("ab",
            # HUB_AGENT_ID) and broadcasts SET_LOG_LEVEL to ALL spokes, so a
            # distinct type keeps every integration working while it registers as
            # an ordinary approvable module.
            auth_payload = {"spoke_id": self.spoke_id, "module_type": "ab"}
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
            # ab would keep suppressing its hub-log scans as if unapproved. A
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
                    except Exception as e:  # noqa: BLE001
                        # A heartbeat that can't be sent means the socket is gone.
                        # Returning SILENTLY (the old behaviour) was the "ab
                        # is offline after every LM upgrade" hang: the task simply
                        # stopped, so the hub's last_seen froze and aged forever
                        # while ab kept scanning repos, believing it was
                        # connected — and nothing logged a thing. Say so, and close
                        # the socket so the receive loop raises, _connect_and_serve
                        # returns, and the reconnect loop actually runs.
                        logger.warning(
                            "Heartbeat send failed (%s) — hub connection is dead; "
                            "closing it to force a reconnect.", e)
                        self._last_conn_error = "heartbeat send failed: %s" % e
                        try:
                            await websocket.close()
                        except Exception:  # noqa: BLE001 — already tearing down
                            pass
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
                    # Slow handlers run as their own task so this loop keeps
                    # draining the socket; everything else stays inline (ordering).
                    if (msg.get("payload") or {}).get("type") in _SLOW_CMDS:
                        self._dispatch_slow(msg)
                    else:
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

    def _dispatch_slow(self, msg: dict) -> None:
        """Run a long-running handler off the receive loop.

        Fire-and-forget swallows exceptions and lets the task be garbage-collected
        mid-flight, so the task is held in ``_inflight`` and its result inspected in
        a done-callback. Over the cap we DROP: stalling the reader is the failure
        mode being fixed, and the hub's durable mailbox redelivers.
        """
        cmd = (msg.get("payload") or {}).get("type")
        if len(self._inflight) >= _MAX_INFLIGHT_HANDLERS:
            logger.error(
                "Dropping %s — %d handlers already in flight (cap %d). The hub will "
                "redeliver from its mailbox; the receive loop stays responsive.",
                cmd, len(self._inflight), _MAX_INFLIGHT_HANDLERS)
            return
        task = asyncio.create_task(self._handle_message(msg))
        self._inflight.add(task)

        def _done(t: "asyncio.Task") -> None:
            self._inflight.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("Handler for %s failed: %s", cmd, exc)

        task.add_done_callback(_done)

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

        if cmd_type == "SPOKE_SET_MTLS_CLIENT_CERT":
            await self._handle_set_mtls_client_cert(msg, data)
            return

        if cmd_type == "SPOKE_CLEAR_MTLS_CLIENT_CERT":
            await self._handle_clear_mtls_client_cert(msg)
            return

        if cmd_type == "SPOKE_SET_MTLS_MATERIALS":
            # The hub's wildcard mTLS-materials fan-out. ab keeps its OWN
            # dedicated cert (ab.<domain>) as its mTLS client identity — it
            # must NOT adopt the fanned-out wildcard client cert, which would
            # replace its SAN-pinned identity and break HUB_REQUEST authorization.
            # So ignore the payload and just ACK, so the hub's durable mailbox
            # clears it instead of retrying to exhaustion ("failed after max
            # retries"). The hub also skips us once our cert is a claimed target.
            await self._ack(msg, "SUCCESS",
                            "ignored — ab uses its own dedicated cert, not the wildcard")
            logger.info("SPOKE_SET_MTLS_MATERIALS ignored (ab keeps its own cert) — acked")
            return

        if cmd_type == "SPOKE_SET_HUB_SECRET":
            new_hub_secret = data.get("hub_secret")
            if new_hub_secret:
                self.hub_secrets.insert(0, new_hub_secret)
                self.hub_secrets = self.hub_secrets[:3]
                self.on_hub_secret(new_hub_secret)
                logger.info("Hub secret stored for %s", self.spoke_id)
            return

        if cmd_type == "ANALYZE_LOGS":
            await self._handle_analyze_logs(msg, data)
            return

        if cmd_type == "ESCALATE_LOG_ISSUE":
            await self._handle_escalate_log_issue(msg, data)
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
            # the doc corpus, the tools, and the agentic loop; AppBuilder just runs
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
                from model_selection import LlmRequirements
                msgs = data.get("messages") or []
                tools = data.get("tools") or None
                system = data.get("system") or "You are the Lab Manager help assistant."
                # A human is waiting on the hub's chat UI for this reply, and the
                # tool set (if any) is caller-supplied by the hub itself, not fixed
                # ahead of time -- so needs_tools mirrors whether the hub actually
                # sent any this turn.
                _help_ask_reqs = LlmRequirements(
                    complexity="medium", needs_tools=bool(tools), latency_sensitive=True)
                # call_llm does NOT return a (value, error) tuple — it returns the
                # result directly (a {"text", "tool_calls"} dict when `tools` is
                # passed, per _request_ollama et al.; a bare string otherwise) and
                # RAISES on failure (see llm_client.call_llm's final `return result`
                # / `raise Exception(...)`). The previous `result, err = call_llm(...)`
                # unpacked the returned dict's KEYS via tuple-assignment — result
                # became the literal string "text", err became "tool_calls" — so
                # `if err:` was ALWAYS true on a successful tool-turn and every
                # working call surfaced as {"status": "ERROR", "message":
                # "tool_calls"}. This is what "Ask AI gives a tool_calls error" was.
                result = await asyncio.to_thread(
                    call_llm, "", system_prompt=system, messages=msgs, tools=tools,
                    requirements=_help_ask_reqs)
                if isinstance(result, dict):
                    rdata = {"status": "SUCCESS", "assistant": {
                        "content": result.get("text") or "",
                        "tool_calls": result.get("tool_calls") or [],
                    }}
                else:
                    rdata = {"status": "SUCCESS", "assistant": {
                        "content": str(result or ""), "tool_calls": [],
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
            # spoke (any module_type), so ab receives it while in
            # active_connections; without a handler here it silently dropped to
            # "Unhandled"
            # and the AppBuilder/HubAgent loggers never flipped to DEBUG.
            enabled = bool(data.get("enabled", False))
            level = set_log_level(enabled)
            logger.info("Log level set to %s", logging.getLevelName(level))
            return

        logger.debug("Unhandled Hub message type: %s", cmd_type)


# Module-level singleton, started by AppBuilder at app startup if HUB_WS_URL is set.
hub_agent_client: Optional[HubAgentClient] = None


def start_agent_from_config(config: dict, on_status=None, on_secret=None, on_hub_secret=None, on_connection=None) -> Optional[HubAgentClient]:
    """Build and start the Hub agent from a AppBuilder config dict.

    Returns the client (also stored as the module singleton), or None if
    HUB_WS_URL is not configured.
    """
    global hub_agent_client
    hub_ws_url = (config.get("HUB_WS_URL") or "").strip()
    if not hub_ws_url:
        return None
    spoke_id = (config.get("HUB_AGENT_ID") or "ab").strip() or "ab"
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
        on_connection=on_connection,
    )
    hub_agent_client = client
    client.start()
    return client
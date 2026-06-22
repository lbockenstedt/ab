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
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosedError

logger = logging.getLogger("HubAgent")

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
        self.hub_ws_url = hub_ws_url
        self.spoke_id = spoke_id
        self.secret = secret or ""
        self.hub_secrets = [hub_secret] if hub_secret else []
        self.on_status = on_status or (lambda _s, _m: None)
        self.on_secret = on_secret or (lambda _s: None)
        self.on_hub_secret = on_hub_secret or (lambda _s: None)

        self.signer = MessageSigner(self.secret) if self.secret else None
        # Pending request Futures keyed by the request header.message_id.
        self._pending: Dict[str, asyncio.Future] = {}
        self._ws = None
        self._approved = bool(self.secret)  # optimistic; corrected by Hub messages
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

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
        msg["signature"] = self.signer.sign(msg)

        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps(msg, separators=(",", ":")))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    # ------------------------------------------------------------------ loop

    async def _run(self):
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

            if self._stop.is_set():
                break
            await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_serve(self):
        async with websockets.connect(self.hub_ws_url) as websocket:
            self._ws = websocket

            # 1. Spoke authentication handshake.
            auth_payload = {"spoke_id": self.spoke_id, "module_type": "agent"}
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
                        if self.signer:
                            msg["signature"] = self.signer.sign(msg)
                        await websocket.send(json.dumps(msg, separators=(",", ":")))
                    except Exception:
                        return
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)

            hb_task = asyncio.create_task(heartbeat())
            try:
                async for message in websocket:
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    self._handle_message(msg)
            finally:
                hb_task.cancel()
                await asyncio.gather(hb_task, return_exceptions=True)
                self._ws = None

    # ------------------------------------------------------------------ dispatch

    def _verify(self, msg: dict) -> bool:
        """Bootstrap-allow unsigned messages until we have a secret; otherwise
        verify with the per-spoke signer (matches control_plane._verify_signature)."""
        if not self.secret or not self.signer:
            return True
        return self.signer.verify(msg)

    def _handle_message(self, msg: dict) -> None:
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
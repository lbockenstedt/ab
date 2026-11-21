"""AB is an agent, not a console spoke: an inbound console-accounts push is a no-op.

The LM hub owns the console module and fans accounts out to its own console
spokes. AB used to keep a ``_console_spokes`` set that nothing ever filled,
plus a fan-out method that could never run. That dead path is gone; these
tests pin that both message types are still accepted quietly, send nothing
back, and are not logged as unhandled.
"""
import asyncio
import logging

import pytest

from hub_agent import HubAgentClient, MessageSigner


class _FakeWs:
    """Records every frame the agent tries to send."""

    def __init__(self):
        self.sent = []

    async def send(self, frame):
        """Record the frame instead of sending it."""
        self.sent.append(frame)


def _client():
    """A connected, approved client built without __init__ (no network, no threads)."""
    c = HubAgentClient.__new__(HubAgentClient)
    c._ws = _FakeWs()
    c.signer = MessageSigner("secret")
    c.spoke_id = "ab"
    c._approved = True
    c._verify = lambda _msg: True
    return c


@pytest.mark.parametrize("cmd_type", ["CONSOLE_SET_ACCOUNTS", "CONSOLE_SET_CREDENTIALS"])
def test_inbound_console_accounts_is_a_quiet_no_op(cmd_type, caplog):
    c = _client()
    msg = {"header": {"message_id": "m1"},
           "payload": {"type": cmd_type, "data": {"accounts": ["admin", "operator"]}}}

    with caplog.at_level(logging.DEBUG, logger="HubAgent"):
        asyncio.run(c._handle_message(msg))

    assert c._ws.sent == []
    assert "Unhandled Hub message" not in caplog.text


def test_dead_console_fan_out_is_removed():
    assert not hasattr(HubAgentClient, "_distribute_console_credentials")

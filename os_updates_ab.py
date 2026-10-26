"""Answer the hub's OS Updates commands for the AppBuilder host — read-only.

The hub's OS Updates panel sends OS_UPDATE_CHECK to every connected node. ab
has its own hub client (hub_agent.py) rather than lm's shared control plane, so
it used to drop these as "Unhandled" and the hub waited out its full 240s
timeout on every check, showing ab as unreachable.

ab can only REPORT pending updates, not apply them: ab.service runs as svc_bg
with ``CapabilityBoundingSet=CAP_NET_BIND_SERVICE``, so it cannot escalate
through sudo to apt. Checks list the packages apt already knows about (the
host's apt-daily timer refreshes the lists); apply and reboot are refused with
an explicit reason so the hub fails fast instead of timing out.

``os_update.py`` is a verbatim copy of lm's core/src/os_update.py (pxmx keeps
the same copy) — keep it in sync rather than editing it here.
"""
from typing import Any, Dict

import os_update

COMMANDS = ("OS_UPDATE_CHECK", "OS_UPDATE_APPLY", "OS_UPDATE_APPLY_PACKAGES", "OS_REBOOT")

APPLY_BLOCKER = ("AppBuilder runs capability-locked and cannot run apt itself — "
                 "patch this host from its own console (LM-AB)")


def handle_command(cmd_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if cmd_type != "OS_UPDATE_CHECK":
        return {"status": "ERROR", "message": f"refusing {cmd_type}: {APPLY_BLOCKER}"}
    res = os_update.check_updates(refresh=False)
    if res.get("eligible"):
        res["apply_blocker"] = APPLY_BLOCKER
        res["features"] = []
    res.setdefault("status", "SUCCESS")
    return res

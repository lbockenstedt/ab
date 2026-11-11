"""Requests slower than SLOW_REQUEST_SECONDS are logged at WARNING with method and
path, so a UI stall (close/merge/save taking minutes) can be traced to a route."""
import ast
import asyncio
import logging
import time
from types import SimpleNamespace

SRC = "main.py"


def _load_log_requests(logger):
    src = open(SRC).read()
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "log_requests")
    node.decorator_list = []
    ns = {"time": time, "logger": logger, "Request": object, "SLOW_REQUEST_SECONDS": 0.05,
          "_is_malformed_request_body": lambda e: False}
    exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    return ns["log_requests"]


def _call(log_requests, delay):
    request = SimpleNamespace(method="POST", url=SimpleNamespace(path="/merge/5", query=""))

    async def call_next(_req):
        await asyncio.sleep(delay)
        return SimpleNamespace(status_code=200)

    return asyncio.run(log_requests(request, call_next))


def test_slow_request_is_logged_with_route(caplog):
    logger = logging.getLogger("ab-slow-test")
    log_requests = _load_log_requests(logger)
    with caplog.at_level(logging.DEBUG, logger="ab-slow-test"):
        _call(log_requests, 0.0)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        _call(log_requests, 0.1)
    slow = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(slow) == 1 and "POST /merge/5" in slow[0] and "status 200" in slow[0]

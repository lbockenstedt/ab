"""Exercise real ASGI dispatch without real config, client, git or delay effects."""

import ast
import asyncio
import builtins
import json
import logging
import os
from pathlib import Path
import threading
from types import SimpleNamespace as N

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
REGISTER = "/api/hub-agent/reregister"
CLAIM = "hub-agent:reregister"


def _case(tmp_path, *, pause=None, error=None):
    """Bind the actual endpoints and claim helpers to isolated operation traces."""
    events, failure = [], [error]

    def effect(name, value=None):
        """Record execution context and inject only an explicitly selected error."""
        events.append((name, threading.get_ident()))
        if failure[0] == name:
            raise RuntimeError(f"unexpected {name} failure")
        if name == "pause" and pause:
            pause()
        return value

    def read_stamp(*args, **kwargs):
        """Trace the actual temporary-file read, never a service state file."""
        effect("stamp")
        return builtins.open(*args, **kwargs)

    # All production effects are replaced before either route can execute.
    stamp = tmp_path / "startup.json"
    stamp.write_text(json.dumps({"commit": "running", "version": "running-version"}))
    repo = N(head=N(commit=N(hexsha="disk")),
             git=N(show=lambda ref: effect("git-show", "previous-version")))
    ns = dict(
        router=APIRouter(), JSONResponse=JSONResponse, asyncio=asyncio,
        logger=logging.getLogger("operational-route-test"), os=os, json=json,
        _inflight_lock=threading.Lock(), _inflight_issues=set(),
        _persist_config_key=lambda *args: effect("persist"),
        _get_hub_agent_client=lambda: effect("client", N(stop=lambda: effect("stop"))),
        _start_hub_agent=lambda: effect("start"), time=N(sleep=lambda seconds: effect("pause")),
        open=read_stamp, STARTUP_STAMP_FILE=str(stamp), CONFIG_DIR=str(tmp_path), state={},
        load_config=lambda: effect("config", {}),
        load_update_state=lambda: effect("update", {"last_known_good_commit": "previous"}),
        git=N(Repo=lambda path: effect("git", repo)),
        _diag_origin_head=lambda: effect("origin", "origin"),
        _diag_origin_version=lambda: effect("origin-version", "origin-version"),
        get_version=lambda: effect("version", "disk-version"),
        _endpoint_perf_rows=lambda config: effect("providers", []),
        _hub_connection_diag=lambda: effect("connection", {}),
    )
    # Execute source, not a copied implementation or the application's startup.
    wanted = {
        "fix_engine.py": {"_issue_claim_key", "_claim_issues", "_release_issues"},
        "routes.py": {"_issue_busy_response", "_run_issue_action",
                      "hub_agent_reregister", "_reregister_hub_agent", "diagnostics"},
    }
    for filename, names in wanted.items():
        nodes = [node for node in ast.parse((ROOT / filename).read_text()).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), ns)
    app = FastAPI()
    app.include_router(ns["router"])
    return N(app=app, ns=ns, events=events, failure=failure)


def test_registration_work_runs_off_the_asgi_loop_with_an_unrelated_fix_active(tmp_path):
    """Offload every setup effect without globally blocking unrelated issue claims."""
    case = _case(tmp_path)

    async def exercise():
        """Dispatch the real endpoint and compare operation threads to its ASGI loop."""
        loop_thread = threading.get_ident()
        assert case.ns["_claim_issues"](["o/r:1"])[0]
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
                response = await client.post(REGISTER)
            assert response.status_code == 200 and response.json()["status"] == "restarted"
            assert [name for name, _ in case.events] == ["persist", "client", "stop", "pause", "start"]
            assert all(thread != loop_thread for _, thread in case.events)
            assert case.ns["_inflight_issues"] == {"o/r:1"}
        finally:
            case.ns["_release_issues"](["o/r:1"])

    asyncio.run(exercise())


def test_diagnostics_io_runs_off_the_asgi_loop(tmp_path):
    """Run the actual startup-file and git paths through native FastAPI dispatch."""
    case = _case(tmp_path)

    async def exercise():
        """Keep response metadata unchanged while checking where I/O executes."""
        loop_thread = threading.get_ident()
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
            response = await client.get("/api/diagnostics")
        assert response.status_code == 200
        assert response.json()["versions"]["disk"] == "disk"
        assert response.json()["update"]["last_known_good_version"] == "previous-version"
        assert {"stamp", "git", "git-show"} <= {name for name, _ in case.events}
        assert all(thread != loop_thread for _, thread in case.events)

    asyncio.run(exercise())


def test_busy_registration_has_no_setup_effects(tmp_path):
    """Refuse the same action before clearing or restarting anything."""
    case = _case(tmp_path)
    assert case.ns["_claim_issues"]([CLAIM])[0]

    async def exercise():
        """Exercise the actual busy HTTP envelope rather than invoking a helper alone."""
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
            response = await client.post(REGISTER)
        assert response.status_code == 409 and response.json()["status"] == "error"
        assert response.json()["message"] == "Hub agent re-registration already in progress"
        assert case.events == []
        assert case.ns["_inflight_issues"] == {CLAIM}
        assert case.ns["_claim_issues"](["o/r:1"])[0]
        case.ns["_release_issues"](["o/r:1"])
        assert case.ns["_inflight_issues"] == {CLAIM}

    try:
        asyncio.run(exercise())
    finally:
        case.ns["_release_issues"]([CLAIM])


def test_existing_handled_startup_error_contract_is_unchanged(tmp_path):
    """Do not invent a synchronous startup/connection guarantee while moving execution."""
    case = _case(tmp_path)
    original_start = case.ns["_start_hub_agent"]

    def handled_start():
        """Model the existing helper recording its own error without raising to the route."""
        original_start()
        case.ns["state"]["hub_agent_status"] = "error"

    case.ns["_start_hub_agent"] = handled_start

    async def exercise():
        """Preserve the route's existing result and the helper's separate status report."""
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
            response = await client.post(REGISTER)
        assert response.status_code == 200 and response.json()["status"] == "restarted"
        assert case.ns["state"]["hub_agent_status"] == "error"
        assert not case.ns["_inflight_issues"]

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_owner", [False, True])
def test_registration_keeps_its_claim_through_overlap_and_http_cancellation(tmp_path, cancel_owner):
    """Only the live worker can release admission, even after its HTTP task disappears."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread, pauses = [], []

    def pause():
        """Pause the first worker, failing fast on the old blocking-loop implementation."""
        pauses.append(threading.get_ident())
        if len(pauses) != 1:
            return
        started.set()
        if pauses[0] == loop_thread[0]:
            raise RuntimeError("blocking setup executed on the ASGI loop")
        if not release.wait(5):
            raise RuntimeError("test did not release its setup worker")

    case = _case(tmp_path, pause=pause)
    real_release = case.ns["_release_issues"]

    def release_claim(ids):
        """Signal only after the actual source helper has released worker ownership."""
        real_release(ids)
        if CLAIM in ids:
            finished.set()

    case.ns["_release_issues"] = release_claim

    async def exercise():
        """Hold one live worker, reject overlap, then prove a fresh request can run."""
        loop_thread.append(threading.get_ident())
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
            owner = asyncio.create_task(client.post(REGISTER))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                assert pauses[0] != loop_thread[0]
                if cancel_owner:
                    owner.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await owner
                response = await client.post(REGISTER)
                assert response.status_code == 409
                assert [name for name, _ in case.events].count("persist") == 1
                assert not finished.is_set()
            finally:
                # Cancellation of the HTTP await must not strand the real worker.
                release.set()
                await asyncio.gather(owner, return_exceptions=True)
                if pauses and pauses[0] != loop_thread[0]:
                    assert await asyncio.to_thread(finished.wait, 5)
            response = await client.post(REGISTER)
            assert response.status_code == 200 and response.json()["status"] == "restarted"
            assert [name for name, _ in case.events].count("persist") == 2
            assert not case.ns["_inflight_issues"]

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["persist", "start"])
def test_unexpected_setup_failure_releases_registration_claim(tmp_path, phase):
    """Retain the existing explicit failure response and permit a later fresh attempt."""
    case = _case(tmp_path, error=phase)

    async def exercise():
        """Fail the worker once, then retry after removing only the injected failure."""
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
            response = await client.post(REGISTER)
            assert response.status_code == 500 and response.json()["status"] == "error"
            assert not case.ns["_inflight_issues"]
            case.failure[0] = None
            response = await client.post(REGISTER)
            assert response.status_code == 200 and response.json()["status"] == "restarted"
            assert not case.ns["_inflight_issues"]

    asyncio.run(exercise())

"""Focused lifecycle, atomicity, and race tests for local and external auth."""
import base64
import ast
import hashlib
import hmac
import importlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from starlette.responses import JSONResponse


@pytest.fixture
def auth_module(tmp_path, monkeypatch):
    """Import auth against isolated temporary config and user-store paths."""
    fake_config = ModuleType("config_store")
    fake_config.CONFIG_DIR = str(tmp_path)
    monkeypatch.setitem(sys.modules, "config_store", fake_config)
    sys.modules.pop("auth", None)
    auth = importlib.import_module("auth")
    monkeypatch.setattr(auth, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "USERS_FILE", str(tmp_path / "users.json"))
    return auth


def _read(auth):
    """Read the test store without exposing it through production APIs."""
    return json.loads(open(auth.USERS_FILE, encoding="utf-8").read())


def test_local_login_and_password_reset_revoke_old_sessions(auth_module):
    """A local password produces a usable token, then reset invalidates all old tokens."""
    auth = auth_module
    assert auth.create_user("admin", "old-password")[0]
    old_token = auth.authenticate_session("admin", "old-password")
    assert old_token and auth.verify_session(old_token) == "admin"

    assert auth.set_password("admin", "new-password")[0]
    assert auth.verify_session(old_token) is None
    new_token = auth.authenticate_session("admin", "new-password")
    assert new_token and auth.verify_session(new_token) == "admin"


def test_external_accounts_cannot_use_or_gain_local_passwords(auth_module):
    """SSO refresh repairs only same-provider legacy records and blocks collisions."""
    auth = auth_module
    assert auth.upsert_external_user("sso@example.test", provider="entra")[0]
    old_token = auth.issue_session("sso@example.test")
    assert auth.set_password("sso@example.test", "local-password")[0] is False
    assert auth.verify_credentials("sso@example.test", "local-password") is False
    assert auth.upsert_external_user("sso@example.test", provider="other")[0] is False
    assert auth.verify_session(old_token) == "sso@example.test"  # ordinary refresh is stable

    assert auth.create_user("local", "local-password")[0]
    assert auth.upsert_external_user("local", provider="entra")[0] is False

    store = _read(auth)
    store["users"]["sso@example.test"].update({
        "salt": "ab" * 16, "hash": "cd" * 32, "algo": "scrypt", "iters": 0})
    with open(auth.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f)
    stale = auth.issue_session("sso@example.test")
    ok, _ = auth.upsert_external_user("sso@example.test", provider="entra", name="refreshed")
    assert ok
    rec = _read(auth)["users"]["sso@example.test"]
    assert not set(("salt", "hash", "algo", "iters")) & rec.keys()
    assert auth.verify_session(stale) is None
    assert auth.verify_credentials("sso@example.test", "anything") is False


def test_external_legacy_hash_is_not_a_local_login(auth_module):
    """An old mixed SSO record is rejected by password auth and password reset."""
    auth = auth_module
    assert auth.upsert_external_user("legacy@example.test", provider="entra")[0]
    store = _read(auth)
    salt = os.urandom(16)
    digest, algo, iters = auth._hash("some-password", salt)
    store["users"]["legacy@example.test"].update(
        salt=salt.hex(), hash=digest, algo=algo, iters=iters)
    with open(auth.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f)

    assert auth.verify_credentials("legacy@example.test", "some-password") is False
    assert auth.set_password("legacy@example.test", "new-password")[0] is False


def test_delete_and_recreate_username_does_not_revive_old_cookie(auth_module):
    """The delete transaction rotates the global signing key before a name is reused."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    assert auth.create_user("second", "second-password")[0]
    old = auth.issue_session("second")
    assert auth.delete_user("second")[0]
    assert auth.verify_session(old) is None
    assert auth.create_user("second", "replacement-password")[0]
    assert auth.verify_session(old) is None
    new = auth.authenticate_session("second", "replacement-password")
    assert new and auth.verify_session(new) == "second"


def _auth_mutation_routes():
    """Load only the account mutation route functions without importing main."""
    import asyncio

    route_file = Path(__file__).resolve().parents[1] / "routes.py"
    wanted = {"auth_create_user", "auth_set_password", "auth_delete_user"}
    nodes = [node for node in ast.parse(route_file.read_text()).body
             if isinstance(node, ast.AsyncFunctionDef) and node.name in wanted]
    for node in nodes:
        node.decorator_list = []
    namespace = {"Request": object, "JSONResponse": JSONResponse, "asyncio": asyncio}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "routes.py", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("store_state", ["missing", "empty", "corrupt", "invalid", "unreadable"])
def test_bootstrap_gate_distinguishes_unreadable_store(
        auth_module, monkeypatch, store_state):
    """Only a genuinely empty installation exposes setup; unreadable stores stay closed."""
    import asyncio
    from fastapi import HTTPException
    from starlette.responses import RedirectResponse

    auth = auth_module
    if store_state in {"empty", "corrupt", "invalid"}:
        contents = {"empty": '{"users": {}, "session_secret": "test-key"}',
                    "corrupt": "{", "invalid": '{"users": []}'}[store_state]
        Path(auth.USERS_FILE).write_text(contents)
    if store_state == "unreadable":
        def refuse_read():
            """Model an existing store the service cannot read."""
            raise PermissionError("test user store unavailable")
        monkeypatch.setattr(auth, "_read_store_raw", refuse_read)

    source = (Path(__file__).resolve().parents[1] / "routes.py").read_text()
    wanted = {"login_page", "setup_admin_page", "setup_admin_submit", "_login_redirect_path"}
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in wanted]
    for node in nodes:
        node.decorator_list = []
    namespace = {
        "Request": object, "HTTPException": HTTPException,
        "RedirectResponse": RedirectResponse, "asyncio": asyncio,
        "templates": SimpleNamespace(TemplateResponse=lambda **kwargs: kwargs),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "routes.py", "exec"), namespace)
    request = SimpleNamespace(cookies={})
    setup_closed = store_state in {"corrupt", "invalid", "unreadable"}
    assert auth.any_users() is setup_closed
    login = asyncio.run(namespace["login_page"](request))
    if setup_closed:
        assert login["context"]["setup_mode"] is False
        for route in ("setup_admin_page", "setup_admin_submit"):
            with pytest.raises(HTTPException) as error:
                asyncio.run(namespace[route](request))
            assert error.value.status_code == 404
    else:
        assert login.headers["location"] == "/setup-admin"
        assert asyncio.run(namespace["setup_admin_page"](request))["context"]["setup_mode"]


def _oidc_callback_route():
    """Load only the OIDC error helper and callback without importing main."""
    import asyncio

    route_file = Path(__file__).resolve().parents[1] / "routes.py"
    wanted = {"_sso_error_page", "oidc_callback"}
    nodes = [node for node in ast.parse(route_file.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in wanted]
    for node in nodes:
        node.decorator_list = []
    namespace = {
        "Request": object, "asyncio": asyncio,
        "logger": SimpleNamespace(warning=lambda *_args: None,
                                  exception=lambda *_args, **_kwargs: None),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "routes.py", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("operation", ["password", "delete"])
def test_admin_mutation_refreshes_surviving_caller_cookie(auth_module, operation):
    """Password reset and account deletion keep a surviving initiating admin signed in."""
    import asyncio
    from http.cookies import SimpleCookie

    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    assert auth.create_user("other", "other-password")[0]
    old = auth.issue_session("admin")
    if operation == "password":
        route = _auth_mutation_routes()["auth_set_password"]
        data = {"username": "admin", "password": "replacement-password"}
    else:
        route = _auth_mutation_routes()["auth_delete_user"]
        data = {"username": "other"}

    async def body():
        return data

    request = SimpleNamespace(
        json=body, state=SimpleNamespace(user="admin"),
        cookies={"ab_session": old},
        url=SimpleNamespace(scheme="https"),
    )
    response = asyncio.run(route(request))
    assert isinstance(response, JSONResponse)
    cookie = SimpleCookie(response.headers["set-cookie"])["ab_session"].value
    assert auth.verify_session(old) is None
    assert auth.verify_session(cookie) == "admin"


def test_delete_route_refuses_actor_cookie_target(auth_module):
    """Self-delete is refused based on the verified cookie, not request.state.user."""
    import asyncio

    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    actor_cookie = auth.issue_session("admin")
    route = _auth_mutation_routes()["auth_delete_user"]

    async def body():
        return {"username": "admin"}

    request = SimpleNamespace(
        json=body, state=SimpleNamespace(user="different-name"),
        cookies={"ab_session": actor_cookie},
        url=SimpleNamespace(scheme="https"),
    )
    response = asyncio.run(route(request))
    assert response["status"] == "error"
    assert "cannot delete" in response["message"].lower()
    assert auth.verify_session(actor_cookie) == "admin"
    assert auth.list_users()[0]["username"] == "admin"


@pytest.mark.parametrize("concurrent_change", ["reset", "delete-recreate"])
def test_stale_actor_cannot_create_account_after_hash(
        auth_module, monkeypatch, concurrent_change):
    """The create route revalidates the original cookie in its final store lock."""
    import asyncio

    auth = auth_module
    assert auth.create_user("actor", "actor-password")[0]
    assert auth.create_user("spare", "spare-password")[0]
    original_cookie = auth.issue_session("actor")
    entered, release = threading.Event(), threading.Event()
    original_hash = auth._hash

    def controlled_hash(password, *args, **kwargs):
        if password == "new-created-password":
            entered.set()
            assert release.wait(5)
        return original_hash(password, *args, **kwargs)

    monkeypatch.setattr(auth, "_hash", controlled_hash)
    namespace = _auth_mutation_routes()
    route = namespace["auth_create_user"]

    async def body():
        return {"username": "new-admin", "password": "new-created-password"}

    request = SimpleNamespace(
        json=body, state=SimpleNamespace(user="actor"),
        cookies={"ab_session": original_cookie},
        url=SimpleNamespace(scheme="https"),
    )
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(asyncio.run, route(request))
    try:
        assert entered.wait(5)
        if concurrent_change == "reset":
            assert auth.set_password("actor", "replacement-actor-password")[0]
        else:
            assert auth.delete_user("actor")[0]
            assert auth.create_user("actor", "replacement-actor-password")[0]
        release.set()
        response = future.result(timeout=5)
        assert response.status_code == 401
        assert 'ab_session=""' in response.headers["set-cookie"]
        assert "new-admin" not in _read(auth)["users"]
        assert auth.authenticate_session("actor", "replacement-actor-password")
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("concurrent_change", ["reset", "delete-recreate"])
@pytest.mark.parametrize("mutation", ["password", "delete"])
def test_stale_before_mutation_is_401_and_clears_cookie(
        auth_module, concurrent_change, mutation):
    """A prior reset/delete-recreate makes middleware's earlier cookie stale for either mutation."""
    import asyncio

    auth = auth_module
    assert auth.create_user("target", "target-password")[0]
    assert auth.create_user("caller", "caller-password")[0]
    original_cookie = auth.issue_session("caller")
    assert auth.verify_session(original_cookie) == "caller"  # middleware accepted it
    if concurrent_change == "reset":
        assert auth.set_password("caller", "new-caller-password")[0]
    else:
        assert auth.delete_user("caller")[0]
        assert auth.create_user("caller", "replacement-caller-password")[0]
    route = _auth_mutation_routes()[
        "auth_set_password" if mutation == "password" else "auth_delete_user"]

    async def body():
        if mutation == "password":
            return {"username": "target", "password": "replacement-password"}
        return {"username": "target"}

    request = SimpleNamespace(
        json=body, state=SimpleNamespace(user="caller"),
        cookies={"ab_session": original_cookie},
        url=SimpleNamespace(scheme="https"),
    )
    response = asyncio.run(route(request))
    assert response.status_code == 401
    assert 'ab_session=""' in response.headers["set-cookie"]
    assert "caller" in _read(auth)["users"]
    if mutation == "password":
        assert auth.authenticate_session("target", "target-password")
        assert auth.authenticate_session("target", "replacement-password") is None
    else:
        assert "target" in _read(auth)["users"]


@pytest.mark.parametrize("concurrent_change", ["reset", "delete-recreate"])
@pytest.mark.parametrize("mutation", ["password", "delete"])
def test_delayed_route_cookie_cannot_survive_later_actor_change(
        auth_module, monkeypatch, concurrent_change, mutation):
    """A token minted atomically by the route is invalidated by a later actor change."""
    import asyncio
    from http.cookies import SimpleCookie

    auth = auth_module
    assert auth.create_user("target", "target-password")[0]
    assert auth.create_user("actor", "actor-password")[0]
    assert auth.create_user("spare", "spare-password")[0]
    original_cookie = auth.issue_session("actor")
    namespace = _auth_mutation_routes()
    route = namespace[
        "auth_set_password" if mutation == "password" else "auth_delete_user"]
    entered, release = threading.Event(), threading.Event()
    real_to_thread = asyncio.to_thread

    async def delayed_to_thread(function, *args, **kwargs):
        result = await real_to_thread(function, *args, **kwargs)
        if function in (auth.set_password_and_issue_actor_session,
                        auth.delete_user_and_issue_actor_session):
            entered.set()
            await real_to_thread(release.wait, 5)
        return result

    namespace["asyncio"] = SimpleNamespace(to_thread=delayed_to_thread)

    async def body():
        if mutation == "password":
            return {"username": "target", "password": "replacement-password"}
        return {"username": "target"}

    request = SimpleNamespace(
        json=body, state=SimpleNamespace(user="not-the-cookie-actor"),
        cookies={"ab_session": original_cookie},
        url=SimpleNamespace(scheme="https"),
    )
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(asyncio.run, route(request))
    try:
        assert entered.wait(5)
        if concurrent_change == "reset":
            assert auth.set_password("actor", "new-actor-password")[0]
        else:
            assert auth.delete_user("actor")[0]
            assert auth.create_user("actor", "replacement-actor-password")[0]
        release.set()
        response = future.result(timeout=5)
        cookie = SimpleCookie(response.headers["set-cookie"])["ab_session"].value
        assert auth.verify_session(cookie) is None
        assert auth.verify_session(original_cookie) is None
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_password_mutation_write_failure_returns_no_replacement_token(
        auth_module, monkeypatch):
    """A failed atomic write cannot return a token signed with an unpersisted key."""
    auth = auth_module
    assert auth.create_user("actor", "actor-password")[0]
    assert auth.create_user("target", "target-password")[0]
    actor_cookie = auth.issue_session("actor")
    before = open(auth.USERS_FILE, "rb").read()

    def fail_replace(*_args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(auth.os, "replace", fail_replace)
    ok, _message, token = auth.set_password_and_issue_actor_session(
        "target", "replacement-password", actor_cookie)
    assert not ok and token is None
    assert open(auth.USERS_FILE, "rb").read() == before
    assert auth.verify_session(actor_cookie) == "actor"
    assert auth.authenticate_session("target", "target-password")


def test_first_setup_has_exactly_one_creator_and_session(auth_module, monkeypatch):
    """Concurrent bootstrap requests create exactly one account and one valid token."""
    auth = auth_module
    barrier = threading.Barrier(2)
    original_hash = auth._hash

    def synchronized_hash(password, *args, **kwargs):
        barrier.wait(timeout=5)
        return original_hash(password, *args, **kwargs)

    monkeypatch.setattr(auth, "_hash", synchronized_hash)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(auth.create_first_user_and_issue_session, "first", "first-password"),
            pool.submit(auth.create_first_user_and_issue_session, "second", "second-password"),
        ]
        results = [future.result(timeout=10) for future in futures]
    winners = [result for result in results if result[0]]
    assert len(winners) == 1
    assert winners[0][2] and auth.verify_session(winners[0][2]) == (
        "first" if results[0][0] else "second")
    assert len(auth.list_users()) == 1


def test_oidc_collision_returns_error_page_without_session_cookie(auth_module, monkeypatch):
    """A refused OIDC collision stops callback completion before any cookie is set."""
    import asyncio
    import oidc

    auth = auth_module
    assert auth.create_user("collision@example.test", "local-password")[0]
    monkeypatch.setattr(oidc, "_auth", auth)
    monkeypatch.setattr(oidc, "get_oidc_config", lambda: SimpleNamespace(
        enabled=True, ready=True, allowed_group=""))
    monkeypatch.setattr(oidc, "verify_state_cookie",
                        lambda _cookie: ("state", "nonce", "verifier"))
    monkeypatch.setattr(oidc, "hmac_eq", lambda left, right: left == right)

    async def discover(_cfg):
        return {"jwks_uri": "synthetic"}

    async def exchange_code(*_args):
        return {"id_token": "synthetic"}

    async def fetch_jwks(_uri):
        return []

    monkeypatch.setattr(oidc, "discover", discover)
    monkeypatch.setattr(oidc, "exchange_code", exchange_code)
    monkeypatch.setattr(oidc, "fetch_jwks", fetch_jwks)
    monkeypatch.setattr(oidc, "verify_id_token", lambda *_args: {
        "oid": "synthetic-oid", "email": "collision@example.test"})
    monkeypatch.setattr(oidc, "enforce_allowed_group", lambda *_args: None)
    monkeypatch.setattr(oidc, "extract_member_groups", lambda _claims: [])
    route = _oidc_callback_route()["oidc_callback"]
    request = SimpleNamespace(
        cookies={oidc.STATE_COOKIE: "synthetic-state"},
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(scheme="https"),
    )
    response = asyncio.run(route(request, code="synthetic", state="state"))
    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert auth.verify_credentials("collision@example.test", "local-password")


def test_password_reset_during_hash_rejects_old_login(auth_module, monkeypatch):
    """A password authenticated before reset cannot return a usable late token."""
    auth = auth_module
    assert auth.create_user("admin", "old-password")[0]
    entered, release = threading.Event(), threading.Event()
    original_hash = auth._hash

    def controlled_hash(password, *args, **kwargs):
        if password == "old-password":
            entered.set()
            assert release.wait(5)
        return original_hash(password, *args, **kwargs)

    monkeypatch.setattr(auth, "_hash", controlled_hash)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(auth.authenticate_session, "admin", "old-password")
    try:
        assert entered.wait(5)
        assert auth.set_password("admin", "new-password")[0]
        release.set()
        assert future.result(timeout=5) is None
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_delete_during_hash_does_not_resurrect_account(auth_module, monkeypatch):
    """Deleting the authenticating account while hashing rejects without stale writes."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    assert auth.create_user("second", "old-password")[0]
    entered, release = threading.Event(), threading.Event()
    original_hash = auth._hash

    def controlled_hash(password, *args, **kwargs):
        if password == "old-password":
            entered.set()
            assert release.wait(5)
        return original_hash(password, *args, **kwargs)

    monkeypatch.setattr(auth, "_hash", controlled_hash)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(auth.authenticate_session, "second", "old-password")
    try:
        assert entered.wait(5)
        assert auth.delete_user("second")[0]
        release.set()
        assert future.result(timeout=5) is None
        assert "second" not in _read(auth)["users"]
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_reset_after_validated_snapshot_invalidates_late_token(auth_module, monkeypatch):
    """If reset follows password validation but precedes signing, the old-key token fails."""
    auth = auth_module
    assert auth.create_user("admin", "old-password")[0]
    validated, release = threading.Event(), threading.Event()
    original_sign = auth._sign_session

    def delayed_sign(username, ttl_s, secret_bytes):
        validated.set()
        assert release.wait(5)
        return original_sign(username, ttl_s, secret_bytes)

    monkeypatch.setattr(auth, "_sign_session", delayed_sign)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(auth.authenticate_session, "admin", "old-password")
    try:
        assert validated.wait(5)
        assert auth.set_password("admin", "new-password")[0]
        release.set()
        late_token = future.result(timeout=5)
        assert late_token and auth.verify_session(late_token) is None
        assert "admin" in _read(auth)["users"]
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_login_stamp_merges_unrelated_account_changes(auth_module, monkeypatch):
    """The last-login stamp writes a fresh snapshot rather than an old read-modify-write."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    entered, release = threading.Event(), threading.Event()
    original_hash = auth._hash

    def controlled_hash(password, *args, **kwargs):
        if password == "admin-password":
            entered.set()
            assert release.wait(5)
        return original_hash(password, *args, **kwargs)

    monkeypatch.setattr(auth, "_hash", controlled_hash)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(auth.authenticate_session, "admin", "admin-password")
    try:
        assert entered.wait(5)
        assert auth.create_user("new-account", "new-password")[0]
        release.set()
        assert future.result(timeout=5)
        assert "new-account" in _read(auth)["users"]
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_missing_secret_repair_rereads_after_concurrent_rotation(auth_module, monkeypatch):
    """A stale missing-secret read cannot overwrite a concurrently rotated store."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    store = _read(auth)
    store.pop("session_secret")
    with open(auth.USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f)
    entered, release = threading.Event(), threading.Event()
    original_read = auth._read_store_raw

    def controlled_read():
        data = original_read()
        if (threading.current_thread().name.startswith("store-reader")
                and not entered.is_set()):
            entered.set()
            assert release.wait(5)
        return data

    monkeypatch.setattr(auth, "_read_store_raw", controlled_read)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-reader")
    future = pool.submit(auth._read_store)
    try:
        assert entered.wait(5)
        assert auth.rotate_session_secret() is None
        rotated = _read(auth)["session_secret"]
        release.set()
        repaired = future.result(timeout=5)
        assert repaired["session_secret"] == rotated
        assert _read(auth)["session_secret"] == rotated
        assert "admin" in _read(auth)["users"]
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_concurrent_same_name_creation_has_one_winner(auth_module):
    """Serialized store writes prevent concurrent account creation from clobbering."""
    auth = auth_module
    barrier = threading.Barrier(3)

    def create(password):
        barrier.wait(timeout=5)
        return auth.create_user("same-name", password)[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, "first-password"),
                   pool.submit(create, "second-password")]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == [False, True]
    assert len(auth.list_users()) == 1


def test_atomic_store_uses_unique_0600_temp_and_cleans_failed_replace(
        auth_module, monkeypatch):
    """Writes use unique private temp files and remove their own temp on failure."""
    auth = auth_module
    real_replace = auth.os.replace
    temp_paths = []
    modes = []

    def inspect_replace(source, destination):
        temp_paths.append(source)
        modes.append(os.stat(source).st_mode & 0o777)
        return real_replace(source, destination)

    monkeypatch.setattr(auth.os, "replace", inspect_replace)
    assert auth.create_user("one", "password-one")[0]
    assert auth.create_user("two", "password-two")[0]
    assert len(set(temp_paths)) == 2
    assert modes == [0o600, 0o600]
    assert not list(os.scandir(auth.CONFIG_DIR)) or all(
        not entry.name.endswith(".tmp") for entry in os.scandir(auth.CONFIG_DIR))

    before = open(auth.USERS_FILE, "rb").read()

    def fail_replace(_source, _destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(auth.os, "replace", fail_replace)
    ok, _ = auth.create_user("three", "password-three")
    assert not ok
    assert open(auth.USERS_FILE, "rb").read() == before
    assert all(not entry.name.endswith(".tmp") for entry in os.scandir(auth.CONFIG_DIR))


def test_unreadable_store_is_preserved_by_all_mutations(auth_module, monkeypatch):
    """Corrupt bytes remain untouched by password, account, secret, and login paths."""
    auth = auth_module
    corrupt = b'{"users": [synthetic-corrupt-content'
    with open(auth.USERS_FILE, "wb") as f:
        f.write(corrupt)
    monkeypatch.setattr(auth, "_hash", lambda *args, **kwargs: ("x", "scrypt", 0))

    assert auth.create_user("x", "password-x")[0] is False
    assert auth.set_password("x", "password-x")[0] is False
    assert auth.delete_user("x")[0] is False
    assert auth.upsert_external_user("x")[0] is False
    assert auth.verify_credentials("x", "password-x") is False
    with pytest.raises(RuntimeError):
        auth.ensure_session_secret()
    with pytest.raises(RuntimeError):
        auth.rotate_session_secret()
    assert open(auth.USERS_FILE, "rb").read() == corrupt
    assert not any(entry.name.endswith(".tmp") for entry in os.scandir(auth.CONFIG_DIR))


def test_last_user_cannot_be_deleted(auth_module):
    """Deleting the final admin remains refused and leaves its session valid."""
    auth = auth_module
    assert auth.create_user("only", "only-password")[0]
    token = auth.issue_session("only")
    assert auth.delete_user("only") == (False, "Cannot delete the last remaining user.")
    assert auth.verify_session(token) == "only"


def _signed_payload(auth, payload):
    """Create a synthetic token with intentionally malformed payload fields."""
    store = _read(auth)
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    key = store["session_secret"].encode()
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


@pytest.mark.parametrize("cookie", [
    "é.bad", "not-a-token", "a.b.c", ".",
])
def test_malformed_cookies_fail_closed(auth_module, cookie):
    """Non-ASCII and malformed cookie shapes never raise or authenticate."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    assert auth.verify_session(cookie) is None
    assert auth.verify_session(None) is None


@pytest.mark.parametrize("payload", [
    {"u": ["admin"], "exp": 9999999999},
    {"u": "admin", "exp": "9999999999"},
    {"u": "admin", "exp": True},
    {"u": "", "exp": 9999999999},
    [],
])
def test_signed_malformed_payload_fails_closed(auth_module, payload):
    """Correctly signed but wrongly typed payloads are rejected."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    assert auth.verify_session(_signed_payload(auth, payload)) is None


@pytest.mark.parametrize("valid_signature", [False, True])
def test_deep_cookie_payload_is_rejected_without_raising(auth_module, valid_signature):
    """Malformed deep JSON cannot crash verification, even with a valid test signature."""
    auth = auth_module
    assert auth.create_user("admin", "admin-password")[0]
    payload = ("[" * 1500 + "0" + "]" * 1500).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        _read(auth)["session_secret"].encode(), encoded.encode(), hashlib.sha256).hexdigest()
    assert auth.verify_session(f"{encoded}.{signature if valid_signature else '0' * 64}") is None


def test_oidc_provisioning_refuses_local_collision(auth_module, monkeypatch):
    """The OIDC helper propagates refused account provisioning before session issue."""
    auth = auth_module
    assert auth.create_user("collision@example.test", "local-password")[0]
    sys.modules.pop("oidc", None)
    oidc = importlib.import_module("oidc")
    monkeypatch.setattr(oidc, "_auth", auth, raising=False)
    with pytest.raises(oidc.OidcError, match="Could not provision Entra account"):
        oidc.provision_entra_user({
            "oid": "synthetic-oid", "email": "collision@example.test"})

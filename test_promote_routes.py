#!/usr/bin/env python3
"""Self-tests for the WebUI branch-promotion buttons (dev->qa, qa->main and
the dev->main override) and for the branch-flow guard that backs them.

Run:  python3 -m pytest -q test_promote_routes.py

routes.py cannot be imported directly (main.py's app-init side effects), so
promote_branch is extracted via ast and exec'd against a stub namespace --
the same harness test_feature_settings_roundtrip.py uses for save_settings.

What matters here, and why:

  * Each button must dispatch the workflow with the RIGHT (source, target).
    A mislabelled button that silently promotes the wrong pair is the worst
    possible failure of this feature.
  * dev->main must be distinctly flagged (`override: true`) so the UI can
    style/confirm it differently and so the log records it as an override.
  * A GitHub API failure must surface as an error. PyGithub's create_dispatch
    returns False rather than raising when GitHub rejects the request, so a
    naive implementation reports a cheerful success while nothing happened --
    exactly the "false fix" class of bug this repo has been bitten by before.
  * The route allowlist must stay exactly three entries. Widening it here (or
    in branch-flow.yml) is how the dev->qa->main order quietly stops meaning
    anything -- AppBuilder's token is the owner's PAT and bypasses rulesets,
    so these checks ARE the enforcement.
"""
import ast
import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------
def _extract(name, src):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in routes.py")


class _NoLog:
    def __init__(self):
        self.records = []

    def _rec(self, *a, **k):
        if a:
            self.records.append(str(a[0]) % tuple(a[1:]) if len(a) > 1 else str(a[0]))

    def __getattr__(self, _):
        return self._rec


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _JSONResponse:
    def __init__(self, status_code=200, content=None):
        self.status_code = status_code
        self.content = content or {}


class _Workflow:
    def __init__(self, result=True, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def create_dispatch(self, ref, inputs):
        self.calls.append((ref, inputs))
        if self.raises:
            raise self.raises
        return self.result


class _Repo:
    def __init__(self, wf, default_branch="main"):
        self._wf = wf
        self.default_branch = default_branch

    def get_workflow(self, name):
        assert name == "promote.yml", name
        return self._wf


class _Github:
    def __init__(self, wf, get_repo_raises=None):
        self._wf = wf
        self._raises = get_repo_raises

    def __call__(self, token):
        return self

    def get_repo(self, name):
        if self._raises:
            raise self._raises
        return _Repo(self._wf)


def _load(workflow=None, token="tok", config=None):
    """Build a namespace and return (promote_branch, workflow, logger, ns)."""
    src = open(os.path.join(ROOT, "routes.py")).read()
    seg = _extract("promote_branch", src)
    wf = workflow if workflow is not None else _Workflow()
    logger = _NoLog()

    # PROMOTE_ROUTES is module-level in routes.py; parse it from source rather
    # than duplicating it here, so this test tracks the real allowlist.
    routes_seg = re.search(r"^PROMOTE_ROUTES\s*=\s*\{.*?^\}", src, re.S | re.M)
    assert routes_seg, "PROMOTE_ROUTES literal not found in routes.py"
    ns_pre = {}
    exec(routes_seg.group(0), ns_pre)

    ns = {
        "asyncio": asyncio,
        "os": os,
        "logger": logger,
        "load_config": lambda: dict(config if config is not None else {"GITHUB_TOKEN": token}),
        "clean_repo_name": lambda r: (r or "").strip(),
        "Github": _Github(wf),
        "JSONResponse": _JSONResponse,
        "Request": object,
        "PROMOTE_ROUTES": ns_pre["PROMOTE_ROUTES"],
    }
    exec(seg, ns)
    return ns["promote_branch"], wf, logger, ns


def _run(coro):
    """Run a coroutine on a private event loop.

    The suite contains tests that leave the main thread without a current
    event loop, so asyncio.get_event_loop() here raises depending on test
    ORDER — these tests passed standalone and failed in the full run. Use a
    fresh loop and restore whatever was set before, so this file neither
    depends on nor perturbs global asyncio state.
    """
    try:
        prev = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:  # noqa: BLE001
        prev = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(prev)


def _call(payload, **kw):
    fn, wf, logger, ns = _load(**kw)
    result = _run(fn(_Req(payload)))
    return result, wf, logger


# --------------------------------------------------------------------------
# Each button dispatches the right (source, target)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("source,target", [("dev", "qa"), ("qa", "main"), ("dev", "main")])
def test_each_button_dispatches_its_own_route(source, target):
    res, wf, _ = _call({"repo": "o/r", "source": source, "target": target})
    assert not isinstance(res, _JSONResponse), f"{source}->{target} was rejected: {res}"
    assert res["status"] == "success"
    assert len(wf.calls) == 1, "expected exactly one workflow dispatch"
    ref, inputs = wf.calls[0]
    assert inputs["source"] == source
    assert res["source"] == source and res["target"] == target


@pytest.mark.parametrize("source,target", [("dev", "qa"), ("qa", "main")])
def test_normal_routes_omit_the_target_input(source, target):
    """The sibling repos' promote.yml still declares only `source`, and GitHub
    rejects a dispatch carrying an undeclared input. Sending `target` on the
    normal routes would therefore break these buttons on every repo but this
    one; omitting it reproduces the historical call exactly."""
    _, wf, _ = _call({"repo": "o/r", "source": source, "target": target})
    _ref, inputs = wf.calls[0]
    assert inputs == {"source": source}, (
        f"normal route {source}->{target} must send only 'source', got {inputs}")


def test_override_sends_the_explicit_target_input():
    """dev->main is not derivable from `source` alone, so it must be explicit."""
    _, wf, _ = _call({"repo": "o/r", "source": "dev", "target": "main"})
    _ref, inputs = wf.calls[0]
    assert inputs == {"source": "dev", "target": "main"}, inputs


def test_dispatch_uses_the_repo_default_branch_as_ref():
    """The workflow file's logic must be read from the default branch — a
    dispatch against the *source* branch would run whatever promote.yml
    happens to look like there."""
    _, wf, _ = _call({"repo": "o/r", "source": "dev", "target": "qa"})
    ref, _inputs = wf.calls[0]
    assert ref == "main"


# --------------------------------------------------------------------------
# The dev->main override is distinctly flagged
# --------------------------------------------------------------------------
def test_dev_to_main_is_flagged_as_override():
    res, _, logger = _call({"repo": "o/r", "source": "dev", "target": "main"})
    assert res["override"] is True
    assert any("OVERRIDE" in r for r in logger.records), logger.records


@pytest.mark.parametrize("source,target", [("dev", "qa"), ("qa", "main")])
def test_normal_routes_are_not_flagged_as_override(source, target):
    res, _, logger = _call({"repo": "o/r", "source": source, "target": target})
    assert res["override"] is False
    assert not any("OVERRIDE" in r for r in logger.records), logger.records


def test_success_message_states_it_does_not_merge():
    """The UI copy must never imply a merge — these buttons only open PRs."""
    for source, target in [("dev", "qa"), ("qa", "main"), ("dev", "main")]:
        res, _, _ = _call({"repo": "o/r", "source": source, "target": target})
        assert "does not merge" in res["message"].lower(), res["message"]


# --------------------------------------------------------------------------
# Failures surface as errors, never as a silent success
# --------------------------------------------------------------------------
def test_github_api_exception_surfaces_as_error():
    wf = _Workflow(raises=RuntimeError("GitHub exploded"))
    res, _, _ = _call({"repo": "o/r", "source": "dev", "target": "qa"}, workflow=wf)
    assert isinstance(res, _JSONResponse)
    assert res.status_code == 500
    assert "GitHub exploded" in res.content["message"]
    assert res.content["status"] == "error"


def test_create_dispatch_returning_false_is_an_error_not_a_success():
    """PyGithub returns False (no exception) when GitHub rejects a dispatch.
    Reporting success there would tell the operator a promotion started when
    none did."""
    wf = _Workflow(result=False)
    res, _, _ = _call({"repo": "o/r", "source": "dev", "target": "main"}, workflow=wf)
    assert isinstance(res, _JSONResponse), "a rejected dispatch must not report success"
    assert res.status_code == 500
    assert res.content["status"] == "error"


def test_missing_token_is_rejected_before_any_dispatch():
    res, wf, _ = _call({"repo": "o/r", "source": "dev", "target": "qa"},
                       config={"GITHUB_TOKEN": ""})
    assert isinstance(res, _JSONResponse) and res.status_code == 400
    assert wf.calls == [], "must not dispatch without a token"


def test_missing_repo_is_rejected_before_any_dispatch():
    res, wf, _ = _call({"repo": "", "source": "dev", "target": "qa"})
    assert isinstance(res, _JSONResponse) and res.status_code == 400
    assert wf.calls == []


def test_malformed_body_is_a_400():
    res, wf, _ = _call(ValueError("not json"))
    assert isinstance(res, _JSONResponse) and res.status_code == 400
    assert wf.calls == []


# --------------------------------------------------------------------------
# The route allowlist cannot be used to aim anything else at main
# --------------------------------------------------------------------------
@pytest.mark.parametrize("source,target", [
    ("main", "dev"),        # backwards
    ("qa", "dev"),          # backwards
    ("dev", "dev"),         # nonsense
    ("feature/x", "main"),  # arbitrary branch straight at production
    ("main", "main"),
    ("qa", "qa"),
    ("", "main"),
    ("dev", ""),
])
def test_disallowed_routes_are_rejected_without_dispatching(source, target):
    res, wf, _ = _call({"repo": "o/r", "source": source, "target": target})
    assert isinstance(res, _JSONResponse), f"{source}->{target} should be rejected"
    assert res.status_code == 400
    assert wf.calls == [], f"{source}->{target} must not reach GitHub"


def test_promote_routes_allowlist_is_exactly_the_three_known_routes():
    _, _, _, ns = _load()
    assert ns["PROMOTE_ROUTES"] == {
        ("dev", "qa"): False,
        ("qa", "main"): False,
        ("dev", "main"): True,
    }, "the promotion allowlist changed — widening it breaks dev -> qa -> main"


# --------------------------------------------------------------------------
# branch-flow.yml: the guard that backs button 3
# --------------------------------------------------------------------------
def _branch_flow_allowlists():
    text = open(os.path.join(ROOT, ".github", "workflows", "branch-flow.yml")).read()
    out = {}
    for base in ("qa", "main"):
        m = re.search(rf'^\s*{base}\)\s*allowed="([^"]+)"', text, re.M)
        assert m, f"no allowlist found for base '{base}' in branch-flow.yml"
        out[base] = m.group(1).split()
    return out


def test_main_allowlist_is_exactly_qa_and_the_two_promote_branches():
    """Pins the ONE deliberate widening. promote/dev-to-main is the override;
    anything more (especially a promote/* glob) is a bypass."""
    assert sorted(_branch_flow_allowlists()["main"]) == sorted(
        ["qa", "promote/qa-to-main", "promote/dev-to-main"])


def test_qa_allowlist_is_exactly_dev_the_promote_branch_and_the_backmerge():
    """backmerge/main-to-qa is the one addition, and it cannot skip a step: it
    runs the REVERSE direction, carrying main back down to qa."""
    assert sorted(_branch_flow_allowlists()["qa"]) == sorted(
        ["dev", "promote/dev-to-qa", "backmerge/main-to-qa"])


def test_nothing_is_ever_backmerged_into_main():
    assert not [b for b in _branch_flow_allowlists()["main"] if b.startswith("backmerge/")], (
        "main takes changes only by promotion; a backmerge/* head into main "
        "would be a route around dev -> qa -> main")


def test_main_allowlist_uses_exact_names_not_globs():
    for name in _branch_flow_allowlists()["main"]:
        assert "*" not in name and "?" not in name, (
            f"'{name}' is a pattern; branch-flow.yml must match exact branch names")


def test_dev_itself_is_still_rejected_into_main():
    assert "dev" not in _branch_flow_allowlists()["main"], (
        "plain 'dev' into main would skip the promote branch and its VERSION pinning")


# --------------------------------------------------------------------------
# promote.yml must stay in step with the endpoint's allowlist
# --------------------------------------------------------------------------
def _promote_yml():
    import yaml
    return yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "promote.yml")))


def _dispatch_inputs(doc):
    # YAML 1.1 parses a bare `on:` key as the boolean True, so the trigger
    # block is keyed by True, not "on".
    trigger = doc.get("on", doc.get(True))
    assert trigger, "no trigger block found in promote.yml"
    return trigger["workflow_dispatch"]["inputs"]


def test_promote_yml_declares_the_target_input_the_override_needs():
    inputs = _dispatch_inputs(_promote_yml())
    assert "target" in inputs, "the dev -> main override cannot be expressed without a 'target' input"
    assert inputs["target"].get("required") is not True, (
        "'target' must stay optional so callers passing only 'source' keep working")
    assert "auto" in inputs["target"]["options"]


def test_promote_yml_route_allowlist_matches_the_endpoint():
    """The endpoint, promote.yml and branch-flow.yml are three independent
    gates on the same decision; if they drift, one of them is not enforcing
    what it appears to."""
    step = [s for s in _promote_yml()["jobs"]["promote"]["steps"] if s.get("id") == "route"][0]
    run = step["run"]
    _, _, _, ns = _load()
    for source, target in ns["PROMOTE_ROUTES"]:
        assert f"{source}:{target}" in run, (
            f"promote.yml does not allow {source} -> {target}, but the endpoint does")


def test_promote_yml_case_pattern_has_no_shell_redirection():
    """A case pattern like `dev->qa` is a bash SYNTAX error ('>' is parsed as a
    redirection), which YAML validation cannot catch and which would break
    every promotion at runtime."""
    step = [s for s in _promote_yml()["jobs"]["promote"]["steps"] if s.get("id") == "route"][0]
    for line in step["run"].splitlines():
        stripped = line.strip()
        if stripped.endswith(")") and "|" in stripped and not stripped.startswith("#"):
            assert "->" not in stripped, f"unquoted '->' in a case pattern is a syntax error: {stripped}"


# --------------------------------------------------------------------------
# Placement: the promotion controls live in the Release Management view,
# not the footer action bar.
# --------------------------------------------------------------------------
def _index_html():
    return open(os.path.join(ROOT, "templates", "index.html")).read()


def _release_view_block(text):
    """The markup between `{% if view == 'release' %}` and its `{% endif %}`."""
    start = text.index("{% if view == 'release' %}")
    end = text.index("{% endif %}", start)
    return text[start:end]


@pytest.mark.parametrize("btn_id", ["promote-dev-qa", "promote-qa-main", "promote-dev-main"])
def test_each_promote_button_exists_exactly_once(btn_id):
    """A stale duplicate left behind in the footer would give two elements the
    same id -- getElementById would then drive only the first, and the visible
    button would silently stop responding."""
    assert _index_html().count(f'id="{btn_id}"') == 1


@pytest.mark.parametrize("btn_id", ["promote-dev-qa", "promote-qa-main", "promote-dev-main"])
def test_promote_buttons_live_in_the_release_view(btn_id):
    assert f'id="{btn_id}"' in _release_view_block(_index_html()), (
        f"{btn_id} is no longer inside the Release Management view block")


def test_repo_selector_lives_in_the_release_view():
    """loadPromoteRepos() early-returns when #promote-repo is absent, so a
    selector left in the footer would populate on every page while the real
    one on this view stayed empty."""
    assert 'id="promote-repo"' in _release_view_block(_index_html())
    assert _index_html().count('id="promote-repo"') == 1


def test_footer_no_longer_carries_the_promotion_controls():
    text = _index_html()
    footer = text[text.index("<footer"):text.index("</footer>")]
    for needle in ("promoteBranch(", 'id="promote-repo"'):
        assert needle not in footer, (
            f"{needle} is still in the footer action bar; it belongs in the Release view")


def test_release_route_is_registered_and_renders_the_release_view():
    src = open(os.path.join(ROOT, "routes.py")).read()
    assert '@router.get("/release")' in src
    m = re.search(r'@router\.get\("/release"\).*?"view":\s*"([a-z-]+)"', src, re.S)
    assert m and m.group(1) == "release", "/release must render view='release'"


def test_sidebar_links_to_release_management():
    text = _index_html()
    assert 'href="/release"' in text
    assert "Release Management" in text
    # The nav item must light up on the matching view, like every other entry.
    assert "'active' if view == 'release'" in text

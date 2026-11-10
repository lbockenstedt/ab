#!/usr/bin/env python3
"""Self-test: self-update syncs dependencies before restarting.

install.sh and update.sh both run `pip install -r requirements.txt`, but the
hourly self-update path in workers.check_for_updates went straight from
`git pull` to a restart. A commit that ADDED a dependency therefore restarted
AppBuilder into a process that had never installed it — a silent degradation
(exactly how `ruff` ended up effectively missing) or a crash loop.

workers.py imports the live app (main -> workers -> main is circular and only
resolves when main.py is the real entrypoint), so the two helpers are extracted
by source via ast and exec'd against fakes — same approach as
test_automerge_refusal_reason.py.
"""
import ast
import os
import subprocess

_WANT_FUNCS = {"_requirements_changed", "_sync_dependencies"}


def _extract(path, funcs):
    src = open(path, encoding="utf-8").read()
    segs = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in funcs:
            segs.append(ast.get_source_segment(src, node))
    return "\n\n".join(segs)


class _Logger:
    def __init__(self):
        self.calls = []

    def _rec(self, level):
        return lambda msg, *a: self.calls.append((level, msg % a if a else msg))

    def __getattr__(self, name):
        return self._rec(name)


def _load(run_impl=None, cwd=None):
    """exec the extracted helpers against fake subprocess/sys/os/logger."""
    path = os.path.join(os.path.dirname(__file__), "workers.py")
    ns = {}

    class _FakeSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(cmd, **kw):
            return run_impl(cmd, **kw)

    class _FakeSys:
        executable = "/opt/ab/venv/bin/python3"

    class _FakeOsPath:
        @staticmethod
        def isfile(p):
            return True

        @staticmethod
        def join(*a):
            return os.path.join(*a)

    class _FakeOs:
        path = _FakeOsPath

        @staticmethod
        def getcwd():
            return cwd or "/opt/ab"

    logger = _Logger()
    ns.update({"subprocess": _FakeSubprocess, "sys": _FakeSys, "os": _FakeOs,
               "logger": logger})
    exec(compile(_extract(path, _WANT_FUNCS), "<workers-extract>", "exec"), ns)
    ns["_logger_obj"] = logger
    return ns


class _FakeRepo:
    def __init__(self, diff_out=None, raises=False):
        self._diff_out = diff_out
        self._raises = raises
        self.git = self

    def diff(self, *args):
        if self._raises:
            raise RuntimeError("bad object")
        return self._diff_out


# --- _requirements_changed ---------------------------------------------------

def test_detects_requirements_change():
    ns = _load()
    repo = _FakeRepo("routes.py\nrequirements.txt\ntemplates/index.html")
    assert ns["_requirements_changed"](repo, "old", "new") is True


def test_ignores_unrelated_changes():
    ns = _load()
    repo = _FakeRepo("routes.py\ntemplates/index.html")
    assert ns["_requirements_changed"](repo, "old", "new") is False


def test_empty_diff_is_no_change():
    ns = _load()
    assert ns["_requirements_changed"](_FakeRepo(""), "old", "new") is False


def test_git_error_fails_safe_and_syncs_anyway():
    """A diff failure must not silently SKIP the dependency sync."""
    ns = _load()
    assert ns["_requirements_changed"](_FakeRepo(raises=True), "old", "new") is True


# --- _sync_dependencies ------------------------------------------------------

class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_sync_uses_the_running_interpreter_not_bare_pip():
    """Must be `sys.executable -m pip`, never a bare "pip" — the systemd unit's
    PATH does not include the venv's bin dir."""
    seen = {}

    def run_impl(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc(0)

    ns = _load(run_impl=run_impl)
    ok, msg = ns["_sync_dependencies"]()
    assert ok is True and msg == "ok"
    assert seen["cmd"][0] == "/opt/ab/venv/bin/python3"
    assert seen["cmd"][1:3] == ["-m", "pip"]
    assert "install" in seen["cmd"] and "-r" in seen["cmd"]
    assert any(str(c).endswith("requirements.txt") for c in seen["cmd"])


def test_sync_reports_failure_with_detail():
    def run_impl(cmd, **kw):
        return _Proc(1, stderr="ERROR: could not find a version\nboom")

    ns = _load(run_impl=run_impl)
    ok, msg = ns["_sync_dependencies"]()
    assert ok is False
    assert "boom" in msg


def test_sync_reports_timeout_rather_than_raising():
    def run_impl(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 600)

    ns = _load(run_impl=run_impl)
    ok, msg = ns["_sync_dependencies"]()
    assert ok is False and "timed out" in msg


def test_sync_never_raises_on_unexpected_error():
    def run_impl(cmd, **kw):
        raise OSError("no pip")

    ns = _load(run_impl=run_impl)
    ok, msg = ns["_sync_dependencies"]()
    assert ok is False and "could not run" in msg


def test_selfupdate_calls_dependency_sync_before_restart():
    """Guard the wiring: the restart signal must come AFTER the sync, otherwise
    the process reloads before its dependencies exist."""
    src = open(os.path.join(os.path.dirname(__file__), "workers.py"),
               encoding="utf-8").read()
    body = src.split("def check_for_updates", 1)[1]
    assert "_requirements_changed(" in body
    assert "_sync_dependencies()" in body
    assert body.index("_sync_dependencies()") < body.index('state["restart_pending"] = True')


def test_ruff_is_declared_in_requirements():
    """lint_python's undefined-name pass depends on it."""
    req = os.path.join(os.path.dirname(__file__), "requirements.txt")
    names = [ln.strip().split("[")[0].split("==")[0].lower()
             for ln in open(req, encoding="utf-8") if ln.strip()
             and not ln.startswith("#")]
    assert "ruff" in names


def test_service_unit_puts_venv_bin_on_path():
    """The unit must export a PATH containing the venv bin dir so tools
    installed by requirements.txt (ruff) are reachable by bare name."""
    inst = open(os.path.join(os.path.dirname(__file__), "install.sh"),
                encoding="utf-8").read()
    path_lines = [ln for ln in inst.splitlines() if ln.startswith("Environment=PATH=")]
    assert len(path_lines) >= 2, "both ab.service and ab-watchdog.service need PATH"
    for ln in path_lines:
        assert "${INSTALL_DIR}/venv/bin" in ln
        assert ln.index("${INSTALL_DIR}/venv/bin") < ln.index("/usr/bin")

"""Tests for the non-Docker (bubblewrap) sandbox backend in fix_engine.

fix_engine imports heavy third-party modules at import time, so — as with
test_head_ci_conclusion.py — the functions under test are extracted from the
source with `ast` and executed in a minimal namespace. That keeps these tests
runnable on a dev machine with no Linux namespaces and no bubblewrap.
"""
import ast
import os
import tempfile
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TARGETS = {
    "_sandbox_backend",
    "_bwrap_usable",
    "_bwrap_argv",
    "_sandbox_infra_failure",
    "is_verification_infra_failure",
    "_exclude_sandbox_venv",
    "_python_requirement_files",
    "_worktree_state",
    "discard_verification_artifacts",
    "run_sandboxed_command",
    "prepare_environment",
}

# Pulled from the source rather than restated here: a test that hard-codes the
# /etc allowlist or the timeout would keep passing after the real value drifted,
# which is exactly the regression these tests exist to catch.
CONSTANTS = {
    "SANDBOX_VENV_DIRNAME", "SANDBOX_TEST_TIMEOUT", "SANDBOX_INSTALL_TIMEOUT",
    "SANDBOX_TMP_BYTES", "SANDBOX_ETC_ALLOWLIST", "SANDBOX_ENV_PASSTHROUGH",
    "SANDBOX_UNAVAILABLE_RC", "SANDBOX_TIMEOUT_RC",
    "_SANDBOX_UNAVAILABLE_MARKER", "_SANDBOX_TIMEOUT_MARKER",
    "VERIFICATION_INFRA_PREFIX",
}


def _load():
    with open(os.path.join(HERE, "fix_engine.py")) as f:
        tree = ast.parse(f.read())
    def _is_const(node):
        return (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in CONSTANTS)

    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name in TARGETS)
              or _is_const(n)]
    found_fns = {n.name for n in wanted if isinstance(n, ast.FunctionDef)}
    assert found_fns == TARGETS, f"missing: {TARGETS - found_fns}"
    found_consts = {n.targets[0].id for n in wanted if isinstance(n, ast.Assign)}
    assert found_consts == CONSTANTS, f"missing: {CONSTANTS - found_consts}"

    logger = types.SimpleNamespace(
        info=lambda *a, **k: None, error=lambda *a, **k: None,
        warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    ns = {"os": os, "logger": logger}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<fix_engine>", "exec"), ns)
    return ns


MOD = _load()


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Replaces the `subprocess` module that the functions import locally."""
    calls = []

    def make(available, runner=None, bwrap_usable=True):
        mod = types.ModuleType("subprocess")
        mod.TimeoutExpired = __import__("subprocess").TimeoutExpired

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            name = argv[0]
            if argv[1:2] == ["--version"]:
                if name in available:
                    return FakeCompleted()
                raise FileNotFoundError(name)
            if argv[0] == "bwrap" and argv[-1] == "/bin/true":
                # _bwrap_usable's namespace probe; usable unless a test says so.
                return FakeCompleted(returncode=0 if bwrap_usable else 1)
            if runner:
                return runner(argv, kwargs)
            return FakeCompleted("out", "err", 0)

        mod.run = run
        monkeypatch.setitem(__import__("sys").modules, "subprocess", mod)
        return calls

    return make


# ── _sandbox_backend ────────────────────────────────────────────────────────

def test_bwrap_is_preferred_over_docker(fake_subprocess):
    fake_subprocess({"bwrap", "docker"})
    assert MOD["_sandbox_backend"]() == "bwrap"


def test_docker_used_when_bwrap_absent(fake_subprocess):
    fake_subprocess({"docker"})
    assert MOD["_sandbox_backend"]() == "docker"


def test_no_backend_returns_none(fake_subprocess):
    fake_subprocess(set())
    assert MOD["_sandbox_backend"]() is None


# ── _bwrap_argv ─────────────────────────────────────────────────────────────

def test_network_available_by_default_to_match_ci():
    """A verifier that disagrees with CI about a green commit is worse than none:
    lm's mDNS test asserts a non-loopback address, which --unshare-net removes."""
    argv = MOD["_bwrap_argv"]("/repo", "pytest")
    assert "--unshare-net" not in argv


def test_network_can_be_severed():
    argv = MOD["_bwrap_argv"]("/repo", "pytest", network=False)
    assert "--unshare-net" in argv


def test_resolver_bound_only_when_network_enabled():
    """/etc/resolv.conf is a symlink into systemd-resolved's runtime dir."""
    on = MOD["_bwrap_argv"]("/repo", "c", network=True)
    off = MOD["_bwrap_argv"]("/repo", "c", network=False)
    assert "/run/systemd/resolve" in on
    assert "/run/systemd/resolve" not in off


def test_tmpfs_is_explicitly_sized():
    """The tmpfs default is half of RAM; on a 1 GB host that is too small for
    repos that guard on free disk space."""
    argv = MOD["_bwrap_argv"]("/repo", "c")
    assert argv[argv.index("--tmpfs") - 2] == "--size"
    assert int(argv[argv.index("--tmpfs") - 1]) >= 2 * 1024 ** 3


def test_usr_is_read_only_and_repo_is_writable():
    argv = MOD["_bwrap_argv"]("/repo", "c")
    assert argv[argv.index("--ro-bind") + 1] == "/usr"
    i = argv.index("--bind")
    assert argv[i + 1] == "/repo" and argv[i + 2] == "/repo"


def test_usrmerge_dirs_are_symlinked_not_bound():
    argv = MOD["_bwrap_argv"]("/repo", "c")
    pairs = [(argv[i + 1], argv[i + 2])
             for i, a in enumerate(argv) if a == "--symlink"]
    assert ("usr/bin", "/bin") in pairs
    assert ("usr/lib", "/lib") in pairs
    assert ("usr/lib64", "/lib64") in pairs
    assert ("usr/sbin", "/sbin") in pairs


def test_all_namespaces_unshared():
    argv = MOD["_bwrap_argv"]("/repo", "c")
    for ns in ("user", "pid", "ipc", "uts", "cgroup"):
        assert f"--unshare-{ns}" in argv


def test_venv_bin_is_prepended_to_path():
    argv = MOD["_bwrap_argv"]("/repo", "c", venv_bin="/repo/.ab-venv/bin")
    path = argv[argv.index("PATH") + 1]
    assert path.startswith("/repo/.ab-venv/bin:")


def test_path_has_no_venv_when_absent():
    path = MOD["_bwrap_argv"]("/repo", "c")[
        MOD["_bwrap_argv"]("/repo", "c").index("PATH") + 1]
    assert ".ab-venv" not in path


def test_command_runs_under_shell_last():
    argv = MOD["_bwrap_argv"]("/repo", "pytest -q")
    assert argv[-3:] == ["/bin/sh", "-c", "pytest -q"]


def test_chdir_is_the_repo():
    argv = MOD["_bwrap_argv"]("/repo", "c")
    assert argv[argv.index("--chdir") + 1] == "/repo"


# ── run_sandboxed_command ───────────────────────────────────────────────────

def test_fails_closed_with_no_backend(fake_subprocess, tmp_path):
    fake_subprocess(set())
    res = MOD["run_sandboxed_command"]("rm -rf /", str(tmp_path))
    assert res.returncode == 127
    assert "No sandbox backend available" in res.stderr


def test_no_backend_never_executes_the_command(fake_subprocess, tmp_path):
    calls = fake_subprocess(set())
    MOD["run_sandboxed_command"]("touch /tmp/pwned", str(tmp_path))
    assert all(c[0][1:2] == ["--version"] for c in calls), \
        "command must never reach the host when unsandboxed"


def test_bwrap_backend_invokes_bwrap(fake_subprocess, tmp_path):
    calls = fake_subprocess({"bwrap"})
    res = MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    argv = calls[-1][0]
    assert argv[0] == "bwrap" and argv[-1] == "pytest"
    assert res.returncode == 0


def test_bwrap_run_has_a_timeout(fake_subprocess, tmp_path):
    calls = fake_subprocess({"bwrap"})
    MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    assert calls[-1][1].get("timeout") == 1800


def test_install_gets_a_longer_timeout_than_tests(tmp_path):
    """A cold multi-component venv downloads far more than a test run executes."""
    seen = []

    def runner(c, p, network=False, timeout=None):
        seen.append(timeout)
        return FakeCompleted("out", "", 0)

    ns = dict(MOD)
    ns["run_sandboxed_command"] = runner
    (tmp_path / "requirements.txt").write_text("")
    os.makedirs(tmp_path / ".git" / "info", exist_ok=True)
    types.FunctionType(MOD["prepare_environment"].__code__, ns,
                       "prepare_environment")(str(tmp_path))
    assert seen == [MOD["SANDBOX_INSTALL_TIMEOUT"]]
    assert MOD["SANDBOX_INSTALL_TIMEOUT"] >= MOD["SANDBOX_TEST_TIMEOUT"]


def test_existing_venv_is_placed_on_path(fake_subprocess, tmp_path):
    os.makedirs(tmp_path / ".ab-venv" / "bin")
    calls = fake_subprocess({"bwrap"})
    MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    argv = calls[-1][0]
    assert argv[argv.index("PATH") + 1].startswith(
        str(tmp_path / ".ab-venv" / "bin") + ":")


def test_unreadable_cwd_is_an_error_not_a_host_run(fake_subprocess):
    fake_subprocess({"bwrap"})
    res = MOD["run_sandboxed_command"]("pytest", "/does/not/exist")
    assert res.returncode == 1


def test_sandbox_exception_is_captured(fake_subprocess, tmp_path):
    def boom(argv, kwargs):
        raise OSError("kaboom")
    fake_subprocess({"bwrap"}, runner=boom)
    res = MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    assert res.returncode == 1 and "kaboom" in res.stderr


def test_docker_fallback_can_sever_network(fake_subprocess, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    calls = fake_subprocess({"docker"})
    MOD["run_sandboxed_command"]("pytest", str(tmp_path), network=False)
    argv = calls[-1][0]
    assert argv[0] == "docker" and "none" in argv


# ── _sandbox_unavailable ────────────────────────────────────────────────────

def test_infra_failure_is_distinguished_from_test_failure():
    infra = FakeCompleted("", "No sandbox backend available (looked for ...)", 127)
    assert MOD["_sandbox_infra_failure"](infra) is True


def test_real_test_failure_is_not_infra():
    """A test suite exiting 127 on its own must not be excused as infra."""
    real = FakeCompleted("", "sh: pytest: command not found", 127)
    assert MOD["_sandbox_infra_failure"](real) is False


def test_passing_result_is_not_infra():
    assert MOD["_sandbox_infra_failure"](FakeCompleted("ok", "", 0)) is False


def test_sandbox_timeout_is_infra_not_a_test_failure():
    """A blown wall-clock budget is capacity, not a defect the model can fix."""
    t = FakeCompleted("", "Sandbox timed out after 1800s", 124)
    assert MOD["_sandbox_infra_failure"](t) is True


def test_a_suite_exiting_124_on_its_own_is_not_infra():
    assert MOD["_sandbox_infra_failure"](
        FakeCompleted("", "some test exited 124", 124)) is False


def test_timeout_is_reported_with_the_timeout_rc(fake_subprocess, tmp_path):
    import subprocess as real_sp

    def slow(argv, kwargs):
        raise real_sp.TimeoutExpired(argv, kwargs.get("timeout"))
    fake_subprocess({"bwrap"}, runner=slow)
    res = MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    assert res.returncode == 124
    assert MOD["_sandbox_infra_failure"](res) is True


# ── _exclude_sandbox_venv ───────────────────────────────────────────────────

def test_venv_is_excluded_from_git():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".git", "info"))
        MOD["_exclude_sandbox_venv"](d)
        with open(os.path.join(d, ".git", "info", "exclude")) as f:
            assert "/.ab-venv/" in f.read()


def test_exclude_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".git", "info"))
        for _ in range(3):
            MOD["_exclude_sandbox_venv"](d)
        with open(os.path.join(d, ".git", "info", "exclude")) as f:
            assert f.read().count("/.ab-venv/") == 1


def test_exclude_preserves_existing_entries():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".git", "info"))
        p = os.path.join(d, ".git", "info", "exclude")
        with open(p, "w") as f:
            f.write("*.log\n")
        MOD["_exclude_sandbox_venv"](d)
        with open(p) as f:
            body = f.read()
        assert "*.log" in body and "/.ab-venv/" in body


def test_exclude_never_raises_on_a_bad_path():
    MOD["_exclude_sandbox_venv"]("/nonexistent/repo/path")


# ── prepare_environment ─────────────────────────────────────────────────────

def _invoke_prepare(tmp_path, files, results=None):
    """Runs prepare_environment with a stubbed runner.

    Returns (calls, return_value) where calls is a list of (cmd, network).
    `results` optionally supplies the FakeCompleted for each successive
    sandbox call, so a test can drive the install-failure branches."""
    for name in files:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    os.makedirs(tmp_path / ".git" / "info", exist_ok=True)
    seen = []
    queued = list(results or [])

    def runner(c, p, network=False, timeout=None):
        seen.append((c, network))
        return queued.pop(0) if queued else FakeCompleted("out", "", 0)

    ns = dict(MOD)
    ns["run_sandboxed_command"] = runner
    fn = types.FunctionType(MOD["prepare_environment"].__code__, ns,
                            "prepare_environment")
    return seen, fn(str(tmp_path))


def _capture_prepare(tmp_path, files):
    """Runs prepare_environment with a stubbed runner, returning (cmd, network)."""
    return _invoke_prepare(tmp_path, files)[0]


def test_install_phase_gets_network(tmp_path):
    seen = _capture_prepare(tmp_path, ["requirements.txt"])
    assert seen and seen[0][1] is True, "dependency install needs network"


def test_python_install_builds_a_venv(tmp_path):
    cmd = _capture_prepare(tmp_path, ["requirements.txt"])[0][0]
    assert "python3 -m venv .ab-venv" in cmd
    assert ".ab-venv/bin/pip install -r requirements.txt" in cmd


def test_pytest_is_always_installed(tmp_path):
    """verify_fix runs pytest; a repo that does not pin it must still verify."""
    cmd = _capture_prepare(tmp_path, ["pyproject.toml"])[0][0]
    assert "pip install pytest pytest-timeout" in cmd


def test_component_requirements_are_found_without_a_root_file(tmp_path):
    """lm has no root requirements.txt — only per-component ones."""
    cmd = _capture_prepare(
        tmp_path, ["agent/requirements.txt", "core/requirements-dev.txt"])[0][0]
    assert "agent/requirements.txt" in cmd
    assert "core/requirements-dev.txt" in cmd


def test_requirement_installs_are_non_fatal(tmp_path):
    """A transient index failure must not gate the merge (as in CI)."""
    cmd = _capture_prepare(tmp_path, ["requirements.txt"])[0][0]
    assert "pip install -r requirements.txt || true" in cmd


def test_pytest_install_is_fatal(tmp_path):
    """Unlike the repo's own deps, a missing test runner cannot be tolerated."""
    cmd = _capture_prepare(tmp_path, ["requirements.txt"])[0][0]
    assert not cmd.rstrip().endswith("|| true")


def test_venv_and_vendor_dirs_are_not_scanned(tmp_path):
    for d in (".ab-venv", "node_modules", "venv", "_lm"):
        p = tmp_path / d / "requirements.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    assert MOD["_python_requirement_files"](str(tmp_path)) == []


def test_requirement_scan_is_sorted_and_relative(tmp_path):
    for name in ("z/requirements.txt", "a/requirements.txt"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    assert MOD["_python_requirement_files"](str(tmp_path)) == [
        "a/requirements.txt", "z/requirements.txt"]


def test_prepare_excludes_venv_from_git(tmp_path):
    _capture_prepare(tmp_path, ["requirements.txt"])
    with open(tmp_path / ".git" / "info" / "exclude") as f:
        assert "/.ab-venv/" in f.read()


def test_node_project_still_uses_npm(tmp_path):
    cmd, net = _capture_prepare(tmp_path, ["package.json"])[0]
    assert cmd == "npm install" and net is True


def test_unknown_project_installs_nothing(tmp_path):
    assert _capture_prepare(tmp_path, ["README.md"]) == []


# ── discard_verification_artifacts ──────────────────────────────────────────

import subprocess as _sp


def _git_repo(tmp_path):
    _sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    _sp.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    _sp.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "src.py").write_text("original\n")
    (tmp_path / "cache.json").write_text("{}\n")
    _sp.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    _sp.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    return tmp_path


def test_test_artifacts_are_reverted(tmp_path):
    """lm's agent tests rewrite pxmx_agent_cache.json during the run."""
    r = _git_repo(tmp_path)
    before = MOD["_worktree_state"](str(r))
    (r / "cache.json").write_text("touched by tests\n")
    MOD["discard_verification_artifacts"](str(r), before)
    assert (r / "cache.json").read_text() == "{}\n"


def test_the_fix_itself_is_never_reverted(tmp_path):
    """Only paths clean BEFORE verification may be reverted."""
    r = _git_repo(tmp_path)
    (r / "src.py").write_text("the fix\n")
    before = MOD["_worktree_state"](str(r))
    (r / "cache.json").write_text("artifact\n")
    MOD["discard_verification_artifacts"](str(r), before)
    assert (r / "src.py").read_text() == "the fix\n"
    assert (r / "cache.json").read_text() == "{}\n"


def test_untracked_artifacts_are_removed(tmp_path):
    r = _git_repo(tmp_path)
    before = MOD["_worktree_state"](str(r))
    (r / "__pycache__").mkdir()
    (r / "__pycache__" / "x.pyc").write_text("x")
    (r / "stray.log").write_text("x")
    MOD["discard_verification_artifacts"](str(r), before)
    assert not (r / "__pycache__").exists()
    assert not (r / "stray.log").exists()


def test_new_untracked_files_from_the_fix_survive(tmp_path):
    """A fix that ADDS a file must keep it."""
    r = _git_repo(tmp_path)
    (r / "new_module.py").write_text("added by the fix\n")
    before = MOD["_worktree_state"](str(r))
    MOD["discard_verification_artifacts"](str(r), before)
    assert (r / "new_module.py").exists()


def test_unreadable_state_is_a_no_op(tmp_path):
    r = _git_repo(tmp_path)
    (r / "cache.json").write_text("changed\n")
    MOD["discard_verification_artifacts"](str(r), None)
    assert (r / "cache.json").read_text() == "changed\n"


def test_worktree_state_on_a_non_repo_is_none(tmp_path):
    assert MOD["_worktree_state"](str(tmp_path / "nope")) is None


# ── Secret containment ──────────────────────────────────────────────────────
# The sandbox runs UNTRUSTED repository code with the network on. A read-only
# mount stops writes, not reads, so isolation here has to mean "the secret is
# not present", not "the secret is not writable".

def test_clearenv_is_set(tmp_path):
    """Without --clearenv the jail inherits GITHUB_TOKEN and the LLM keys."""
    argv = MOD["_bwrap_argv"](str(tmp_path), "pytest")
    assert "--clearenv" in argv


def test_clearenv_precedes_every_setenv(tmp_path):
    """bwrap applies options in argv order, so a --clearenv after a --setenv
    would wipe the value it was meant to keep."""
    argv = MOD["_bwrap_argv"](str(tmp_path), "pytest")
    first_setenv = argv.index("--setenv")
    assert argv.index("--clearenv") < first_setenv


def test_service_environment_does_not_reach_the_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecret")
    argv = MOD["_bwrap_argv"](str(tmp_path), "pytest")
    joined = " ".join(argv)
    assert "ghp_supersecret" not in joined
    assert "sk-supersecret" not in joined
    assert "GITHUB_TOKEN" not in argv


def test_etc_is_not_bound_wholesale(tmp_path):
    """/etc holds /etc/ab/config.json (GITHUB_TOKEN + hub secrets), /etc/shadow
    and /etc/ssh; binding all of it hands them to repository code."""
    argv = MOD["_bwrap_argv"](str(tmp_path), "pytest")
    pairs = list(zip(argv, argv[1:]))
    assert ("--ro-bind", "/etc") not in pairs
    assert ("--ro-bind-try", "/etc") not in pairs
    assert ("--bind", "/etc") not in pairs


def test_ab_config_is_never_exposed(tmp_path):
    """/etc/ab/config.json is the single highest-value secret on the host."""
    assert not any(e == "ab" or e.startswith("ab/")
                   for e in MOD["SANDBOX_ETC_ALLOWLIST"])
    argv = MOD["_bwrap_argv"](str(tmp_path), "pytest")
    assert not any(a.startswith("/etc/ab") for a in argv)


def test_host_credential_files_are_not_allowlisted(tmp_path):
    forbidden = {"shadow", "gshadow", "ssh", "sudoers", "sudoers.d",
                 "ab", "systemd", "cron.d"}
    assert not (set(MOD["SANDBOX_ETC_ALLOWLIST"]) & forbidden)


def test_etc_allowlist_keeps_name_and_tls_resolution(tmp_path):
    """Dropping these would break DNS or TLS inside the jail, which looks like
    a failing test suite rather than a sandbox misconfiguration."""
    allow = set(MOD["SANDBOX_ETC_ALLOWLIST"])
    for needed in ("passwd", "group", "nsswitch.conf", "hosts",
                   "resolv.conf", "ssl", "ca-certificates.conf"):
        assert needed in allow, needed


def test_allowlisted_etc_entries_use_ro_bind_try(tmp_path):
    """An entry absent on a given host must not abort the whole jail."""
    argv = MOD["_bwrap_argv"](str(tmp_path), "pytest")
    for i, arg in enumerate(argv):
        if arg.startswith("/etc/") and argv[i - 1] not in ("--ro-bind-try",):
            assert argv[i - 2] == "--ro-bind-try", arg


# ── Unusable bubblewrap ─────────────────────────────────────────────────────

def test_installed_but_unusable_bwrap_falls_back_to_docker(fake_subprocess):
    """bwrap --version succeeding proves only that the binary exists; a host
    with user namespaces disabled must not select it."""
    fake_subprocess({"bwrap", "docker"}, bwrap_usable=False)
    assert MOD["_sandbox_backend"]() == "docker"


def test_unusable_bwrap_with_no_docker_is_no_backend(fake_subprocess):
    fake_subprocess({"bwrap"}, bwrap_usable=False)
    assert MOD["_sandbox_backend"]() is None


def test_bwrap_usable_probe_actually_starts_a_jail(fake_subprocess):
    calls = fake_subprocess({"bwrap"})
    MOD["_bwrap_usable"]()
    argv = calls[-1][0]
    assert argv[0] == "bwrap" and "--unshare-user" in argv


def test_bwrap_startup_failure_is_infra_not_a_test_failure(fake_subprocess, tmp_path):
    """bwrap exits non-zero with a `bwrap:` diagnostic when it cannot build the
    jail; as a plain rc that is indistinguishable from a failing suite and
    would be fed to the model as a code defect it can never fix."""
    def runner(argv, kwargs):
        return FakeCompleted("", "bwrap: No permissions to creating new namespace", 1)

    fake_subprocess({"bwrap"}, runner=runner)
    res = MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    assert res.returncode == MOD["SANDBOX_UNAVAILABLE_RC"]
    assert MOD["_sandbox_infra_failure"](res)


def test_real_test_failure_is_not_mistaken_for_a_bwrap_startup_failure(
        fake_subprocess, tmp_path):
    def runner(argv, kwargs):
        return FakeCompleted("1 failed, 2 passed", "bwrap: whatever", 1)

    fake_subprocess({"bwrap"}, runner=runner)
    res = MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    assert res.returncode == 1
    assert not MOD["_sandbox_infra_failure"](res)


# ── Docker parity ───────────────────────────────────────────────────────────

def test_docker_timeout_is_infra_like_the_bwrap_path(fake_subprocess, tmp_path, monkeypatch):
    """The Docker fallback used to swallow TimeoutExpired in its generic
    handler and return rc 1, so verify_fix reported a timeout as a failing
    test -- the exact conflation the bwrap path was fixed to avoid."""
    real_timeout = __import__("subprocess").TimeoutExpired

    def runner(argv, kwargs):
        raise real_timeout(cmd=argv, timeout=1800)

    fake_subprocess({"docker"}, runner=runner)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    res = MOD["run_sandboxed_command"]("pytest", str(tmp_path))
    assert res.returncode == MOD["SANDBOX_TIMEOUT_RC"]
    assert MOD["_sandbox_infra_failure"](res)


# ── Infra-failure signalling (return value, never an exception) ─────────────

def test_infra_predicate_matches_the_prefix():
    msg = MOD["VERIFICATION_INFRA_PREFIX"] + " no backend"
    assert MOD["is_verification_infra_failure"](msg)


def test_infra_predicate_rejects_ordinary_test_output():
    assert not MOD["is_verification_infra_failure"]("2 failed, 5 passed")
    assert not MOD["is_verification_infra_failure"](None)
    assert not MOD["is_verification_infra_failure"]("")


def test_prepare_reports_infra_failure_without_raising(tmp_path):
    """Raising would land in each caller's broad `except Exception`, which
    records a generic code error -- a REPLAYABLE failure kind that comes back
    later as 'a verified finding about the CODE'."""
    unavailable = FakeCompleted(
        "", MOD["_SANDBOX_UNAVAILABLE_MARKER"] + " ...", MOD["SANDBOX_UNAVAILABLE_RC"])
    _, out = _invoke_prepare(tmp_path, ["requirements.txt"], results=[unavailable])
    assert MOD["is_verification_infra_failure"](out)


def test_failed_venv_or_pytest_install_is_infra(tmp_path):
    """The chain is &&-joined and ends with the pytest install, so a non-zero
    rc means there is no test runner; letting it through yields a
    'pytest: not found' failure reported to the model as a code defect."""
    _, out = _invoke_prepare(tmp_path, ["requirements.txt"],
                             results=[FakeCompleted("", "no module venv", 1)])
    assert MOD["is_verification_infra_failure"](out)


def test_optional_bootstrap_failure_is_not_fatal(tmp_path):
    """Many fleet repos have a Makefile with no `install` target (make exits
    2); that was tolerated before and must stay tolerated."""
    _, out = _invoke_prepare(tmp_path, ["Makefile"],
                             results=[FakeCompleted("", "No rule to make target", 2)])
    assert out is None


def test_optional_bootstrap_still_reports_a_broken_sandbox(tmp_path):
    unavailable = FakeCompleted(
        "", MOD["_SANDBOX_UNAVAILABLE_MARKER"] + " ...", MOD["SANDBOX_UNAVAILABLE_RC"])
    _, out = _invoke_prepare(tmp_path, ["Makefile"], results=[unavailable])
    assert MOD["is_verification_infra_failure"](out)


def test_successful_prepare_returns_none(tmp_path):
    _, out = _invoke_prepare(tmp_path, ["requirements.txt"])
    assert out is None


def test_repo_with_no_dependency_file_returns_none(tmp_path):
    _, out = _invoke_prepare(tmp_path, ["README.md"])
    assert out is None


# ── Worktree parsing ────────────────────────────────────────────────────────

def _state_from(records, tmp_path, monkeypatch):
    mod = types.ModuleType("subprocess")
    mod.run = lambda argv, **kw: FakeCompleted("\0".join(records), "", 0)
    monkeypatch.setitem(__import__("sys").modules, "subprocess", mod)
    return MOD["_worktree_state"](str(tmp_path))


def test_worktree_state_handles_paths_with_spaces(tmp_path, monkeypatch):
    """Default porcelain QUOTES such a path; -z never does, so a quoted name
    would make discard_verification_artifacts miss the artifact entirely."""
    state = _state_from(["?? my notes.txt", ""], tmp_path, monkeypatch)
    assert state == {"my notes.txt": "??"}


def test_worktree_state_handles_renames(tmp_path, monkeypatch):
    """A rename emits TWO -z records; consuming only the first would treat the
    old path as a separate untracked entry and delete it."""
    state = _state_from(["R  new.py", "old.py", " M other.py", ""],
                        tmp_path, monkeypatch)
    assert state == {"new.py": "R ", "other.py": " M"}


def test_worktree_state_uses_z(tmp_path, monkeypatch):
    seen = {}
    mod = types.ModuleType("subprocess")

    def run(argv, **kw):
        seen["argv"] = argv
        return FakeCompleted("", "", 0)

    mod.run = run
    monkeypatch.setitem(__import__("sys").modules, "subprocess", mod)
    MOD["_worktree_state"](str(tmp_path))
    assert "-z" in seen["argv"]

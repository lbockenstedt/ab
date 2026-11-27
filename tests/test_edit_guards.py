"""Guards on pool-written edits: a closest-line hint for a missed anchor, and refusing an edit that
only adds names the file already holds."""
import main  # noqa: F401 - import first: fix_engine has a circular import without it
import fix_engine

SRC = 'KEEP = {\n    "GET_SYSTEM_STATS", "GET_AGENTS", "RUN_COMMAND",\n}\n\ndef handler(cmd_type, data):\n    return cmd_type\n'


def test_closest_line_hint_names_a_real_line():
    """A near-miss anchor gets the file's real line number and text; nothing close gives no hint."""
    hint = fix_engine._closest_line_hint(SRC, "def handler(cmd, data):\n    pass", "x.py")
    assert "closest line in the file is 5" in hint and "def handler(cmd_type, data):" in hint
    assert fix_engine._closest_line_hint(SRC, "zzzzzzzzzzzzzzzz unrelated", "x.py") == ""
    assert fix_engine._closest_line_hint(SRC, "x", "x.py") == ""


def test_set_duplicate_noop_only_when_nothing_changes():
    """Repeating an item a set already holds is a no-op; adding to a different set or a new item is not."""
    two = 'A = {"GET_AGENTS"}\nB = {"OTHER"}\n'
    assert fix_engine._set_duplicate_noop(two, 'A = {"GET_AGENTS", "GET_AGENTS"}\nB = {"OTHER"}\n')
    assert not fix_engine._set_duplicate_noop(two, 'A = {"GET_AGENTS"}\nB = {"OTHER", "GET_AGENTS"}\n')
    assert not fix_engine._set_duplicate_noop(two, 'A = {"GET_AGENTS", "GET_AGENTS", "NEW"}\nB = {"OTHER"}\n')
    assert not fix_engine._set_duplicate_noop(two, two + "# note\n")
    assert not fix_engine._set_duplicate_noop("A = {f()}\n", "A = {f(), f()}\n")
    assert not fix_engine._set_duplicate_noop(two, "A = {")


def test_closest_line_hint_never_echoes_a_secret():
    """No hint for config files or credential-named lines, so a value never reaches a log."""
    cfg = 'JWT_SIGNING_KEY = "hunter2hunter2"\nother = 1\n'
    assert fix_engine._closest_line_hint(cfg, 'JWT_SIGNING_KEY = "hunter3"', "a.py") == ""
    assert fix_engine._closest_line_hint("password: hunter2\n", "password: hunter3", "a.yml") == ""


def test_timeout_findings_are_performance_findings():
    """The four live timeout titles are performance findings, so perf_reject_to_ops covers them."""
    import log_scan
    for t in ("🤖 Log Alert: Hub log alert (hypervisor): Health check: PXMX_LIST_VMS keeps timing out on x",
              "🤖 Log Alert: Hub log alert (hub): Health check: slow command AGENT_OS_UPDATE_CHECK on x"):
        assert log_scan.is_performance_finding(t.split("Log Alert: ")[-1].split("): ", 1)[-1])


def test_repo_grep_context_finds_symbols_in_other_files(tmp_path):
    """Code-like symbols are grepped across the checkout, skipping prompt files, plain words and config files."""
    import subprocess
    (tmp_path / "a.py").write_text('KEEP = {"GET_AGENTS"}\n')
    (tmp_path / "b.py").write_text("def use():\n    return 'GET_AGENTS'\n")
    (tmp_path / "c.json").write_text('{"GET_AGENTS": "secret"}\n')
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    out = fix_engine._repo_grep_context(str(tmp_path), ["GET_AGENTS", "timeout"], skip_paths={"a.py"})
    assert "b.py:2" in out and "a.py" not in out and "c.json" not in out
    assert fix_engine._repo_grep_context(str(tmp_path), ["timeout"]) == ""

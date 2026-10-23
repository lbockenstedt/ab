"""repo_tools.MutatingRepoToolExecutor / run_mutating_agent — the tool-calling
feature-build loop used when no claude_cli mutating agent is approved (Copilot
models build through edit_file/create_file/check_syntax)."""
import atexit
import json
import shutil
import tempfile
from pathlib import Path

import repo_tools

_SCRATCH_ROOT = tempfile.mkdtemp(prefix="ab-build-agent-tools-")
atexit.register(shutil.rmtree, _SCRATCH_ROOT, True)


def _repo(name):
    p = Path(_SCRATCH_ROOT) / name
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    (p / "app.py").write_text("def a():\n    return 1\n\n\ndef b():\n    return 1\n", encoding="utf-8")
    (p / ".git").mkdir()
    (p / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return p


def _tc(name, idx=0, **args):
    return {"id": f"c{idx}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_edit_file_requires_unique_exact_match():
    root = _repo("unique")
    ex = repo_tools.MutatingRepoToolExecutor(str(root))
    out = json.loads(ex.execute("edit_file", {"path": "app.py", "search": "return 1", "replace": "return 2"}))
    assert "occurs 2 times" in out["error"]
    out = json.loads(ex.execute("edit_file", {"path": "app.py", "search": "return 9", "replace": "x"}))
    assert "not found" in out["error"]
    out = json.loads(ex.execute("edit_file", {"path": "app.py", "search": "def b():\n    return 1",
                                              "replace": "def b():\n    return 2"}))
    assert out["edited"] is True and out["syntax"]["ok"] is True
    assert "return 2" in (root / "app.py").read_text()
    assert ex.edits == 1


def test_edit_reports_syntax_error():
    root = _repo("syntax")
    ex = repo_tools.MutatingRepoToolExecutor(str(root))
    out = json.loads(ex.execute("edit_file", {"path": "app.py", "search": "def a():", "replace": "def a(:"}))
    assert out["syntax"]["ok"] is False


def test_writes_refused_outside_sandbox_and_in_protected_dirs():
    root = _repo("protect")
    ex = repo_tools.MutatingRepoToolExecutor(str(root))
    for path in ("../evil.py", ".git/hooks/pre-commit", "_lm/x.py", "/tmp/abs.py"):
        out = json.loads(ex.execute("create_file", {"path": path, "content": "x"}))
        assert "error" in out, path
    out = json.loads(ex.execute("create_file", {"path": "app.py", "content": "x"}))
    assert "already exists" in out["error"]
    assert not (root.parent / "evil.py").exists()


def test_rollback_restores_edits_and_removes_created_files():
    root = _repo("rollback")
    ex = repo_tools.MutatingRepoToolExecutor(str(root))
    original = (root / "app.py").read_text()
    ex.execute("edit_file", {"path": "app.py", "search": "def a():", "replace": "def a2():"})
    ex.execute("create_file", {"path": "pkg/new.py", "content": "X = 1\n"})
    assert ex.changed_paths() == ["app.py", "pkg/new.py"]
    ex.rollback()
    assert (root / "app.py").read_text() == original
    assert not (root / "pkg" / "new.py").exists()


def test_check_file_syntax_json_and_unknown(tmp_path=None):
    root = _repo("chk")
    (root / "bad.json").write_text("{nope", encoding="utf-8")
    (root / "x.md").write_text("# hi", encoding="utf-8")
    assert repo_tools.check_file_syntax(str(root / "bad.json"))["ok"] is False
    assert repo_tools.check_file_syntax(str(root / "x.md"))["ok"] is True


def test_run_mutating_agent_edits_then_returns_summary():
    root = _repo("loop")
    replies = [
        {"text": "", "tool_calls": [_tc("read_file", 0, path="app.py")]},
        {"text": "", "tool_calls": [_tc("edit_file", 1, path="app.py", search="def a():\n    return 1",
                                        replace="def a():\n    return 3")]},
        {"text": '{"pr_body": "done"}', "tool_calls": []},
    ]
    seen_tools = []

    def call(messages, tools):
        seen_tools.append([t["function"]["name"] for t in tools] if tools else None)
        return replies.pop(0)

    out = repo_tools.run_mutating_agent(call, [{"role": "system", "content": "S"},
                                               {"role": "user", "content": "U"}], str(root))
    assert out == '{"pr_body": "done"}'
    assert "return 3" in (root / "app.py").read_text()
    assert "edit_file" in seen_tools[0] and "create_file" in seen_tools[0]


def test_summary_before_any_edit_is_nudged():
    root = _repo("nudge")
    replies = [
        {"text": '{"pr_body": "I would change app.py"}', "tool_calls": []},
        {"text": "", "tool_calls": [_tc("create_file", 0, path="docs/x.md", content="# x\n")]},
        {"text": '{"pr_body": "ok"}', "tool_calls": []},
    ]
    prompts = []

    def call(messages, tools):
        prompts.append(messages[-1].get("content"))
        return replies.pop(0)

    out = repo_tools.run_mutating_agent(call, [{"role": "user", "content": "U"}], str(root))
    assert out == '{"pr_body": "ok"}'
    assert "not changed any file" in prompts[1]
    assert (root / "docs" / "x.md").exists()


def test_provider_error_rolls_back_partial_work():
    root = _repo("err")
    original = (root / "app.py").read_text()
    state = {"n": 0}

    def call(messages, tools):
        state["n"] += 1
        if state["n"] == 1:
            return {"text": "", "tool_calls": [_tc("edit_file", 0, path="app.py",
                                                   search="def a():", replace="def zz():")]}
        raise RuntimeError("HTTP 500")

    try:
        repo_tools.run_mutating_agent(call, [{"role": "user", "content": "U"}], str(root))
        assert False, "expected the provider error to propagate"
    except RuntimeError:
        pass
    assert (root / "app.py").read_text() == original


def test_iteration_cap_forces_final_summary_without_tools():
    root = _repo("cap")
    calls = []

    def call(messages, tools):
        calls.append(tools)
        if tools is None:
            return {"text": '{"pr_body": "partial", "touchpoints_skipped": ["docs"]}'}
        return {"text": "", "tool_calls": [_tc("list_dir", len(calls), path=".")]}

    out = repo_tools.run_mutating_agent(call, [{"role": "user", "content": "U"}], str(root), max_iterations=3)
    assert "partial" in out
    assert calls[-1] is None and len(calls) == 4


def test_history_compaction_elides_old_tool_output():
    msgs = [{"role": "user", "content": "U"}] + [
        {"role": "tool", "content": "x" * 1000} for _ in range(20)]
    repo_tools._compact_history(msgs, budget_chars=10000, keep_recent=4)
    assert sum(len(m["content"]) for m in msgs) <= 10000
    assert all(m["content"] == "x" * 1000 for m in msgs[-4:])

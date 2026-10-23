"""fix_engine.push_or_raise — a rejected push must raise.

Regression for lm#1247: GitPython's Remote.push() returns REJECTED PushInfo
flags instead of raising, so AppBuilder reported "pushed directly to dev",
closed the issue and recorded a commit that never reached GitHub."""
import ast
import os
import subprocess
import tempfile

import git
import pytest


def _load():
    src = open("fix_engine.py").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == "push_or_raise":
            ns = {"git": git}
            exec(ast.get_source_segment(src, node), ns)
            return ns["push_or_raise"]
    raise AssertionError("push_or_raise not found")


push_or_raise = _load()


def _run(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repos():
    with tempfile.TemporaryDirectory() as tmp:
        origin = os.path.join(tmp, "origin.git")
        _run("init", "-q", "--bare", "-b", "dev", origin, cwd=tmp)
        clones = []
        for name in ("a", "b"):
            path = os.path.join(tmp, name)
            _run("clone", "-q", origin, path, cwd=tmp)
            _run("config", "user.email", "t@t", cwd=path)
            _run("config", "user.name", "t", cwd=path)
            _run("checkout", "-q", "-B", "dev", cwd=path)
            clones.append(path)
        a, b = clones
        open(os.path.join(a, "f.txt"), "w").write("base\n")
        _run("add", "-A", cwd=a)
        _run("commit", "-qm", "base", cwd=a)
        _run("push", "-q", "origin", "dev", cwd=a)
        _run("pull", "-q", "origin", "dev", cwd=b)
        yield a, b


def _commit(path, name):
    open(os.path.join(path, name), "w").write(name)
    _run("add", "-A", cwd=path)
    _run("commit", "-qm", name, cwd=path)


def test_fast_forward_push_succeeds(repos):
    a, _ = repos
    _commit(a, "one.txt")
    push_or_raise(git.Repo(a).remotes.origin, "HEAD:dev")


def test_non_fast_forward_push_raises(repos):
    a, b = repos
    _commit(a, "moved.txt")
    _run("push", "-q", "origin", "dev", cwd=a)  # dev moves (like lm:1256 landing first)
    _commit(b, "stale.txt")
    remote = git.Repo(b).remotes.origin
    with pytest.raises(git.GitCommandError):
        push_or_raise(remote, "HEAD:dev")


def test_rebase_then_push_succeeds_after_rejection(repos):
    a, b = repos
    _commit(a, "moved.txt")
    _run("push", "-q", "origin", "dev", cwd=a)
    _commit(b, "stale.txt")
    repo = git.Repo(b)
    with pytest.raises(git.GitCommandError):
        push_or_raise(repo.remotes.origin, "HEAD:dev")
    repo.remotes.origin.pull("dev", rebase=True)
    push_or_raise(repo.remotes.origin, "HEAD:dev")

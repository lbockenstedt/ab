"""install.sh's sudo root helpers validate their arguments before acting.

svc_bg may run /usr/local/bin/ab-sandbox and ab-ollama-setup as root with any
arguments (sudoers allows `*`), so each helper is the security boundary:
- ab-sandbox must only bind-mount a real directory under /opt/ab, so `..` or a
  symlink must not reach a host directory, and the image must not be an option;
- ab-ollama-setup writes its arguments into a root systemd override, so a
  newline must not add a directive such as ExecStartPre=.
Each helper is extracted from install.sh and run with fake binaries on PATH.
"""
import os
import re
import subprocess

INSTALL_SH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "install.sh")


def _helper(name):
    """The body of the heredoc that install.sh writes to /usr/local/bin/<name>."""
    src = open(INSTALL_SH, encoding="utf-8").read()
    pat = rf"cat > /usr/local/bin/{re.escape(name)} <<'HELPER'\n(.*?)\nHELPER\n"
    return re.search(pat, src, re.S).group(1)


def _fake_bin(tmp_path, log, names):
    """A directory of fake commands that log their argv and succeed."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for n in names:
        f = d / n
        out = "echo active" if n == "systemctl" else ":"
        f.write_text(f'#!/bin/bash\necho "{n} $*" >> {log}\n{out}\n')
        f.chmod(0o755)
    return str(d)


def _run(script, args, path_dir, **extra_env):
    """Run a helper script with bash and the fake bin dir first on PATH."""
    env = dict(os.environ, PATH=f"{path_dir}:{os.environ['PATH']}", **extra_env)
    return subprocess.run(["bash", "-c", script, "helper", *args], env=env,
                          capture_output=True, text=True)


def _sandbox(tmp_path):
    """ab-sandbox with /opt/ab replaced by a temp root; returns (script, root, log, bin)."""
    root = os.path.realpath(tmp_path) + "/optab"
    os.makedirs(root + "/repo")
    log = tmp_path / "calls.log"
    script = _helper("ab-sandbox").replace("/opt/ab", root)
    return script, root, log, _fake_bin(tmp_path, log, ["docker"])


def test_sandbox_mounts_a_directory_under_the_root(tmp_path):
    """The normal call runs docker with the resolved checkout mounted at /app."""
    script, root, log, bindir = _sandbox(tmp_path)
    r = _run(script, ["python:3.9-slim", root + "/repo", "pytest -q"], bindir,
             SUDO_UID="1234", SUDO_GID="5678")
    assert r.returncode == 0, r.stderr
    # The container runs as the sudo caller, never as root, so it cannot leave
    # a setuid-root file in the mounted checkout.
    assert log.read_text().strip() == (
        "docker run --rm --user 1234:5678 --security-opt no-new-privileges "
        f"--cap-drop ALL -e HOME=/tmp -v {root}/repo:/app -w /app python:3.9-slim "
        "sh -c pytest -q")


def test_sandbox_refuses_escaping_cwd_and_option_image(tmp_path):
    """'..', a symlink out of the root, the root itself, a ':' path and an
    option-shaped image are refused before docker runs."""
    script, root, log, bindir = _sandbox(tmp_path)
    os.symlink("/", root + "/escape")
    os.makedirs(root + "/a:b")
    bad = [
        ("ubuntu:latest", root + "/../.."),
        ("ubuntu:latest", root + "/repo/../../.."),
        ("ubuntu:latest", root + "/escape"),
        ("ubuntu:latest", root),
        ("ubuntu:latest", root + "/missing"),
        ("ubuntu:latest", root + "/a:b"),
        ("--privileged", root + "/repo"),
        ("-v=/:/host", root + "/repo"),
    ]
    for image, cwd in bad:
        r = _run(script, [image, cwd, "id"], bindir)
        assert r.returncode == 2, (image, cwd, r.stdout, r.stderr)
    assert not log.exists()


def _ollama(tmp_path):
    """ab-ollama-setup with its override dir moved to a temp dir."""
    over = tmp_path / "override.d"
    log = tmp_path / "calls.log"
    script = _helper("ab-ollama-setup").replace(
        "/etc/systemd/system/ollama.service.d", str(over))
    return script, over, log, _fake_bin(tmp_path, log, ["zstd", "curl", "systemctl"])


def test_ollama_setup_writes_override_for_valid_args(tmp_path):
    """Numeric counts and a host:port bind address are written as given."""
    script, over, log, bindir = _ollama(tmp_path)
    r = _run(script, ["4", "3", "172.16.1.100:11434"], bindir)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (over / "override.conf").read_text()
    assert 'Environment="OLLAMA_NUM_THREAD=4"' in text
    assert 'Environment="OLLAMA_HOST=172.16.1.100:11434"' in text


def test_ollama_setup_refuses_injected_args(tmp_path):
    """A newline, quote or non-number in any argument is refused before any
    command runs or the override is written."""
    script, over, log, bindir = _ollama(tmp_path)
    bad = [
        ["4\nExecStartPre=/bin/id", "3"],
        ["4", "3\nExecStartPre=/bin/id"],
        ["4", "3", "0.0.0.0:11434\nExecStartPre=/bin/id"],
        ["4", "3", '0.0.0.0:11434"'],
        ["-1", "3"],
        ["4", "x"],
    ]
    for args in bad:
        r = _run(script, args, bindir)
        assert r.returncode == 2, (args, r.stdout)
        assert "ERROR" in r.stdout
    assert not log.exists()
    assert not over.exists()

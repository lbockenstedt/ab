"""install.sh bakes the hub URL into config.json without destroying an unreadable file.

The snippet used to turn any read or parse failure into an empty config and then
rewrite the file with just the two hub keys, so a corrupt config.json plus a
routine `install.sh --hub ...` re-run wiped every LLM endpoint and credential.
"""
import json
import os
import re
import subprocess
import sys

INSTALL_SH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "install.sh")


def _bake(config_file, ws="lm-hub.example", query="", prelude=""):
    """Run install.sh's hub-bake heredoc against `config_file`; return the CompletedProcess."""
    src = open(INSTALL_SH, encoding="utf-8").read()
    snippet = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", src, re.S).group(1)
    env = dict(os.environ, CONFIG_FILE=str(config_file), HUB_WS_URL=ws, HUB_QUERY_URL=query)
    return subprocess.run([sys.executable, "-c", prelude + snippet], env=env,
                          capture_output=True, text=True)


def test_rewrite_keeps_the_old_owner_and_mode(tmp_path):
    """The replacement file takes the old file's owner and mode before it goes live.

    As root, mkstemp makes a root-owned 0600 file, and the installer's later chown -R may
    never be reached: the service user would be locked out of its config (the outage this
    change exists to prevent). Not root here, so the chown call is recorded instead.
    """
    cf = tmp_path / "config.json"
    cf.write_text(json.dumps({"llm_entries": [{"id": "a"}]}))
    cf.chmod(0o640)
    log = tmp_path / "chown.log"
    prelude = (
        "import os\n"
        "_chown = os.chown\n"
        f"def _rec(p, u, g):\n"
        f"    open({str(log)!r}, 'a').write(f'{{u}} {{g}}\\n')\n"
        f"    _chown(p, u, g)\n"
        "os.chown = _rec\n"
    )
    r = _bake(cf, prelude=prelude)
    assert r.returncode == 0, r.stderr
    st = os.stat(cf)
    assert log.read_text().split() == [str(st.st_uid), str(st.st_gid)]
    assert oct(st.st_mode & 0o777) == oct(0o640)


def test_existing_keys_are_kept_and_hub_keys_set(tmp_path):
    """A re-run updates the two hub keys and keeps everything else, leaving no temp file."""
    cf = tmp_path / "config.json"
    cf.write_text(json.dumps({"llm_entries": [{"id": "a"}], "HUB_WS_URL": "old"}))
    r = _bake(cf)
    assert r.returncode == 0, r.stderr
    cfg = json.loads(cf.read_text())
    assert cfg["llm_entries"] == [{"id": "a"}]
    assert cfg["HUB_WS_URL"] == "lm-hub.example"
    assert cfg["HUB_QUERY_URL"] == "https://lm-hub.example"
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_missing_and_empty_files_are_created(tmp_path):
    """Nothing to lose: a missing or zero-byte config gets just the hub keys."""
    missing = tmp_path / "missing.json"
    empty = tmp_path / "empty.json"
    empty.write_text("")
    for cf in (missing, empty):
        assert _bake(cf).returncode == 0
        assert json.loads(cf.read_text())["HUB_WS_URL"] == "lm-hub.example"


def test_unreadable_config_is_refused_and_left_untouched(tmp_path):
    """Corrupt JSON and non-object JSON make the installer fail without writing."""
    for body in ('{"llm_entries": [1, 2', "[1, 2]", '"text"'):
        cf = tmp_path / "config.json"
        cf.write_text(body)
        r = _bake(cf)
        assert r.returncode != 0
        assert "not overwriting" in r.stderr
        assert cf.read_text() == body
        assert [p.name for p in tmp_path.iterdir()] == ["config.json"]

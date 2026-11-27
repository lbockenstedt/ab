"""Guards on pool-written edits: a closest-line hint for a missed anchor, and refusing an edit that
only adds names the file already holds."""
import main  # noqa: F401 - import first: fix_engine has a circular import without it
import fix_engine

SRC = 'KEEP = {\n    "GET_SYSTEM_STATS", "GET_AGENTS", "RUN_COMMAND",\n}\n\ndef handler(cmd_type, data):\n    return cmd_type\n'


def test_closest_line_hint_names_a_real_line():
    """A near-miss anchor gets the file's real line number and text; nothing close gives no hint."""
    hint = fix_engine._closest_line_hint(SRC, "def handler(cmd, data):\n    pass")
    assert "closest line in the file is 5" in hint and "def handler(cmd_type, data):" in hint
    assert fix_engine._closest_line_hint(SRC, "zzzzzzzzzzzzzzzz unrelated") == ""
    assert fix_engine._closest_line_hint(SRC, "x") == ""


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
    """A near-miss anchor beside a credential line names its line and shape but never the value."""
    cfg = 'JWT_SIGNING_KEY = "hunter2hunter2"\nother = 1\n'
    hint = fix_engine._closest_line_hint(cfg, 'JWT_SIGNING_KEY = "hunter3"')
    assert "line in the file is 1" in hint and "hunter2" not in hint and "JWT_SIGNING_KEY" in hint

"""Concurrent fix attempts must retain their own retry classification and evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from test_fix_apply import _load_funcs


@pytest.mark.parametrize("first_empty", [True, False])
def test_overlapping_callers_keep_independent_parser_feedback(tmp_path, first_empty):
    """Interleave a barren reply and rejected edit before the first caller reads feedback."""
    apply = _load_funcs()["parse_and_apply"]
    rejected = json.dumps({"edits": [{"file": "anchor.py", "search": "", "replace": "other"}]})
    contents = ("", rejected) if first_empty else (rejected, "")
    feedback = ({}, {})
    returned, other_done = threading.Event(), threading.Event()

    def first():
        """Return from parsing before another attempt completes, then consume diagnostics."""
        try:
            result = apply(contents[0], str(tmp_path), feedback_out=feedback[0])
        finally:
            returned.set()
        assert other_done.wait(5)
        return result

    def second():
        """Replace the old shared diagnostics in the first caller's consumption window."""
        assert returned.wait(5)
        try:
            return apply(contents[1], str(tmp_path), feedback_out=feedback[1])
        finally:
            other_done.set()

    with ThreadPoolExecutor(2) as pool:
        one, two = pool.submit(first), pool.submit(second)
        assert one.result(5) == two.result(5) == (False, {}, 0.0)

    for content, diagnostic in zip(contents, feedback):
        assert diagnostic["last_raw_prefix"] == content[:240]
        assert diagnostic["last_reason"] == ("edit_anchor_miss" if content else "empty")
        if content:
            assert len(diagnostic["last_failures"]) == 1
            assert "anchor.py" in diagnostic["last_failures"][0]
            assert "search must be non-empty" in diagnostic["last_failures"][0]
        else:
            assert diagnostic["last_failures"] == []
    assert not list(tmp_path.iterdir())

#!/usr/bin/env python3
"""Self-test for chat.py's _tool_read_file pagination + pattern search.

Run:  python3 test_read_file_pagination.py

chat.py can't be imported directly (main.py circular chain), so _tool_read_file
and _trunc are ast-extracted and exec'd with a fake Github client (per this
repo's convention — see test_chat_requirements.py).

Covers:
1. offset paging: has_more true on a big file; next_offset advances; re-reading
   at next_offset returns the following window; full file is reconstructable.
2. pattern search: the returned window contains the match even when the match
   is far past max_bytes from the start (the exact simulation.sh case where the
   wired path lives ~byte 35k in a 63k file, past the 20k cap).
"""
import ast
import re


def _load(names):
    src = open("chat.py").read()
    tree = ast.parse(src)
    segs = [ast.get_source_segment(src, n) for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {"re": re}
    exec("\n\n".join(segs), ns)
    return ns


class _FakeContents:
    def __init__(self, text):
        self.decoded_content = text.encode("utf-8")


class _FakeRepo:
    def __init__(self, text):
        self._text = text

    def get_contents(self, path):
        return _FakeContents(self._text)


class _FakeGh:
    def __init__(self, text):
        self._text = text

    def get_repo(self, name):
        return _FakeRepo(self._text)


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def main():
    print("Running _tool_read_file pagination self-test...")
    ok = True
    ns = _load({"_tool_read_file", "_trunc"})
    read_file = ns["_tool_read_file"]

    # A ~30 KB file with a unique marker well past the 8000-byte first window.
    body = "".join(f"line {i} filler content\n" for i in range(1200))
    marker_line = "NEEDLE_wired_dhcp_bug_here\n"
    text = body[:25000] + marker_line + body[25000:]
    gh = _FakeGh(text)

    # 1. paging
    r0 = read_file(gh, {}, {"repo": "o/r", "path": "f", "max_bytes": 8000, "offset": 0})
    ok &= _check("first window: offset 0, has_more true", r0.get("offset") == 0 and r0.get("has_more") is True)
    ok &= _check("next_offset advances by window length",
                 r0.get("next_offset") == len(r0.get("content", "")))
    r1 = read_file(gh, {}, {"repo": "o/r", "path": "f", "max_bytes": 8000, "offset": r0["next_offset"]})
    ok &= _check("second window starts at prior next_offset", r1.get("offset") == r0["next_offset"])

    # reconstruct the whole file by paging
    chunks, off, guard = [], 0, 0
    while True:
        r = read_file(gh, {}, {"repo": "o/r", "path": "f", "max_bytes": 8000, "offset": off})
        chunks.append(r["content"])
        if not r.get("has_more"):
            break
        off = r["next_offset"]
        guard += 1
        if guard > 100:
            break
    ok &= _check("paging reconstructs the full file", "".join(chunks) == text)

    # 2. pattern jumps straight to the match, even past the first window
    rp = read_file(gh, {}, {"repo": "o/r", "path": "f", "max_bytes": 8000, "pattern": "NEEDLE_wired_dhcp_bug_here"})
    ok &= _check("pattern search reports match_found", rp.get("match_found") is True)
    ok &= _check("pattern window actually contains the far-off match",
                 "NEEDLE_wired_dhcp_bug_here" in rp.get("content", ""))
    ok &= _check("pattern reports the matching line number(s)", bool(rp.get("match_lines")))

    # 2b. pattern not found is reported cleanly
    rn = read_file(gh, {}, {"repo": "o/r", "path": "f", "max_bytes": 8000, "pattern": "ZZZ_not_present"})
    ok &= _check("missing pattern -> match_found False, no crash", rn.get("match_found") is False)

    print("\n" + ("ALL CASES PASSED" if ok else "ONE OR MORE CASES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

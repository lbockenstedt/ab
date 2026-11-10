#!/usr/bin/env python3
"""Self-test for ab/attr_definition_lookup.py.

Run:  python3 ab/test_attr_definition_lookup.py

Standalone: imports only attr_definition_lookup (stdlib-only, no app init,
no real GitHub/network — fake gh/repo/content objects stand in for PyGithub).

Motivating case: cs#74's reviewer flagged getattr(deploy, "proxmox_states",
{}) and getattr(cp, "connected_agents", {}) as unverifiable from the diff —
both turned out to be correct on manual inspection of files the diff never
touched. These tests mirror that exact shape.
"""
import sys

from attr_definition_lookup import (
    extract_getattr_names, find_attr_definitions, format_wiring_context)


class _F:
    def __init__(self, filename, patch):
        self.filename = filename
        self.patch = patch


class _ContentItem:
    def __init__(self, path, text):
        self.path = path
        self.decoded_content = text.encode("utf-8")


class _FakeGh:
    """Fake PyGithub Github client: search_code(query) returns a canned list
    per query, keyed on the exact pattern substring so tests can control
    which pattern (assignment vs def) "hits" and which repo it's scoped to."""
    def __init__(self, by_pattern):
        self._by_pattern = by_pattern
        self.calls = []

    def search_code(self, query):
        self.calls.append(query)
        for pattern, items in self._by_pattern.items():
            if pattern in query:
                return items
        return []


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


_GETATTR_PATCH = """@@ -1,0 +1,4 @@
+cp = getattr(spoke, "control_plane", None)
+states = getattr(deploy, "proxmox_states", {})
+agents = getattr(cp, "connected_agents", {})
+states = getattr(deploy, "proxmox_states", {})
"""

_NO_GETATTR_PATCH = """@@ -1,0 +1,2 @@
+def foo():
+    return 1
"""


class _Repo:
    full_name = "lbockenstedt/cs"


def main():
    print("Running ab attr_definition_lookup self-test...")
    ok = True

    # (1) extract_getattr_names dedupes and preserves first-seen order.
    names = extract_getattr_names([_F("lm-spoke/src/stale_client_reclone.py", _GETATTR_PATCH)])
    ok &= _check("extracts each distinct getattr name once, in order",
                names == ["control_plane", "proxmox_states", "connected_agents"])

    # (2) no getattr calls at all -> empty list, no crash.
    ok &= _check("no getattr calls yields empty list",
                extract_getattr_names([_F("x.py", _NO_GETATTR_PATCH)]) == [])

    # (3) no patch (binary/oversized) -> skipped.
    ok &= _check("file with no patch is skipped without raising",
                extract_getattr_names([_F("bin", None)]) == [])

    # (4) find_attr_definitions: assignment pattern hit, def pattern not tried
    # once the assignment already found something.
    gh = _FakeGh({
        'self.proxmox_states =': [_ContentItem(
            "lm-spoke/src/proxmox_deploy.py",
            "class ProxmoxDeploy:\n    def __init__(self):\n        self.proxmox_states = {}\n")],
    })
    defs = find_attr_definitions(gh, _Repo(), ["proxmox_states"])
    ok &= _check("finds the real assignment site",
                "proxmox_states" in defs and defs["proxmox_states"][0]["path"] == "lm-spoke/src/proxmox_deploy.py")
    ok &= _check("snippet contains the matched line",
                "self.proxmox_states = {}" in defs["proxmox_states"][0]["snippet"])

    # (5) falls back to the `def name(` pattern when the assignment pattern
    # finds nothing (e.g. a @property, not a plain attribute).
    gh2 = _FakeGh({
        'def connected_agents(': [_ContentItem(
            "lm-spoke/src/control_plane.py",
            "class ControlPlane:\n    @property\n    def connected_agents(self):\n        return self._agents\n")],
    })
    defs2 = find_attr_definitions(gh2, _Repo(), ["connected_agents"])
    ok &= _check("falls back to def-pattern search",
                "connected_agents" in defs2)
    ok &= _check("both search patterns were tried (assignment first, then def)",
                len(gh2.calls) == 2)

    # (6) a hit inside a file the diff already changed is excluded — that
    # file is already shown in full by fix_engine's proactive embedding, so
    # repeating it here would just be noise.
    gh3 = _FakeGh({
        'self.control_plane =': [_ContentItem(
            "lm-spoke/src/cs_spoke.py", "self.control_plane = None\n")],
    })
    defs3 = find_attr_definitions(gh3, _Repo(), ["control_plane"],
                                  changed_paths=["lm-spoke/src/cs_spoke.py"])
    ok &= _check("a hit inside an already-changed file is excluded",
                defs3 == {})

    # (7) an attr name that finds nothing anywhere is simply absent from the
    # result dict — NOT reported as "confirmed missing" (code search can miss
    # dynamic assignment; absence of a hit isn't proof of absence).
    gh4 = _FakeGh({})
    defs4 = find_attr_definitions(gh4, _Repo(), ["nonexistent_attr"])
    ok &= _check("an attr with no hits is left out of the result",
                "nonexistent_attr" not in defs4 and defs4 == {})

    # (8) gh=None / empty attr list -> {} without ever calling search.
    ok &= _check("gh=None returns {} without raising",
                find_attr_definitions(None, _Repo(), ["x"]) == {})
    ok &= _check("empty attr_names returns {} without raising",
                find_attr_definitions(_FakeGh({}), _Repo(), []) == {})

    # (9) format_wiring_context: empty input -> empty string (no prompt bloat
    # when nothing was found); non-empty input names the file + attr.
    ok &= _check("empty defs formats to empty string", format_wiring_context({}) == "")
    rendered = format_wiring_context(defs)
    ok &= _check("rendered block names the attr and the real file",
                "proxmox_states" in rendered and "lm-spoke/src/proxmox_deploy.py" in rendered)
    ok &= _check("rendered block is framed as ground truth, not a suggestion",
                "ACTUAL definitions found" in rendered)

    # (10) a search/rate-limit error for one name doesn't take down the
    # others or raise.
    class _FlakyGh:
        def search_code(self, query):
            if "flaky" in query:
                raise RuntimeError("rate limited")
            return [_ContentItem("ok.py", "self.stable = 1\n")]
    defs5 = find_attr_definitions(_FlakyGh(), _Repo(), ["flaky", "stable"])
    ok &= _check("a raising search for one name doesn't affect another / doesn't raise",
                "flaky" not in defs5 and "stable" in defs5)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())

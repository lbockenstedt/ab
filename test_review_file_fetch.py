#!/usr/bin/env python3
"""Self-test for the reviewer's repo-file fetch path (fix_engine).

Run:  python3 ab/test_review_file_fetch.py

WHAT BROKE (lm#444, and every other attempt to fix a big file)
-------------------------------------------------------------
GitHub's Contents API REFUSES to inline any file over 1MB: it answers with
``encoding: "none"`` and an empty ``content``, so PyGithub's decoded_content
raises. lm's ``WebUI/main.js`` is 1.99MB / 30,323 lines, so every reviewer
fetch of it returned "no fetchable content". The model therefore never saw the
file and invented search anchors that could not possibly match — the observed
failure was three rounds of "search snippet not found", naming a parameter
(``setTenant(tenantName)``) that does not exist; the real signature is
``setTenant(tenant)``.

Two independent defects, so two independent guards below:

  1. FETCH — the file could not be read at all. Fixed by reading from the local
     checkout when AppBuilder already has one, and otherwise falling back to the
     Git Blobs API (100MB limit) when the Contents API declines to inline.

  2. RETRIEVAL — even with the bytes in hand, the reply is capped at
     _REVIEW_FILE_MAX_CHARS (20,000). On a 1.99MB file that is the first ~1%,
     so a symbol at line 1669 is still invisible. Fixed by the ``pattern``
     argument, which returns windows AROUND matches instead of the head.

Fixing only (1) would have left the tool still useless on exactly the file that
motivated it, so the "without pattern the symbol is absent / with pattern it is
present" pair below is the real regression guard.

fix_engine.py cannot be imported (its ``from main import …`` triggers a
circular FastAPI init), so the pure helpers are extracted via ast and exec'd —
this exercises the real code text, not a copy.
"""
import ast
import base64
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIX_ENGINE = os.path.join(HERE, "fix_engine.py")


def _load():
    src = open(FIX_ENGINE).read()
    tree = ast.parse(src)
    want = {"_safe_repo_target", "_repo_file_text", "_fetch_repo_file_for_review",
            "_targeted_file_context"}
    segs = [ast.get_source_segment(src, n) for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in want]

    def _trunc(s, n):
        if s is None:
            return ""
        s = str(s)
        return s if len(s) <= n else s[:n] + " …[truncated]"

    class _L:
        def __getattr__(self, _):
            return lambda *a, **k: None

    ns = {"os": os, "json": json, "re": re, "base64": base64, "logger": _L(),
          "_trunc": _trunc, "_REVIEW_FILE_MAX_CHARS": 20000}
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


# ---------------------------------------------------------------- fakes


class _Oversized:
    """A ContentFile for a >1MB file: the Contents API gave us no content, so
    decoded_content raises exactly as PyGithub's does."""

    def __init__(self, sha="abc123"):
        self.sha = sha

    @property
    def decoded_content(self):
        raise AssertionError("unsupported encoding: none")


class _Blob:
    def __init__(self, text):
        self.content = base64.b64encode(text.encode()).decode()


class _Repo:
    """Records access so a test can prove the API was never consulted."""

    def __init__(self, blob_text=None, content_file=None, blob_raises=False):
        self._blob_text = blob_text
        self._content_file = content_file if content_file is not None else _Oversized()
        self._blob_raises = blob_raises
        self.contents_calls = 0
        self.blob_calls = 0

    def get_contents(self, path, ref=None):
        self.contents_calls += 1
        return self._content_file

    def get_git_blob(self, sha):
        self.blob_calls += 1
        if self._blob_raises:
            raise RuntimeError("blob boom")
        return _Blob(self._blob_text or "")


def _big_js():
    """A stand-in for lm's WebUI/main.js: >1MB, with the real signature far past
    any head-truncation cutoff."""
    filler = "// padding line that exists only to push the symbol out of reach\n"
    head = filler * 1668
    body = ("async function setTenant(tenant) {\n"
            "    currentTenant = tenant;\n"
            "    localStorage.setItem('lm_tenant', tenant);\n"
            "}\n")
    tail = filler * 15000
    return head + body + tail


def main():
    ns = _load()
    fetch = ns["_fetch_repo_file_for_review"]
    read = ns["_repo_file_text"]
    ok = True

    # ---------------------------------------------------------- 1. FETCH
    big = _big_js()
    ok &= _check("test fixture really is >1MB (the Contents API refusal threshold)",
                 len(big) > 1_000_000)

    repo = _Repo(blob_text=big)
    text, err = read(repo, "deadbeef", "WebUI/main.js")
    ok &= _check("a >1MB file is recovered via the Git Blobs API, not lost",
                 err is None and text == big)
    ok &= _check("the blob fallback is only reached after the Contents API declines",
                 repo.contents_calls == 1 and repo.blob_calls == 1)

    # Local checkout must win outright — no API call at all.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "WebUI"))
        with open(os.path.join(d, "WebUI", "main.js"), "w") as fh:
            fh.write(big)
        repo2 = _Repo(blob_text="WRONG")
        text2, err2 = read(repo2, "deadbeef", "WebUI/main.js", checkout_path=d)
        ok &= _check("an existing local checkout is used instead of streaming from GitHub",
                     err2 is None and text2 == big)
        ok &= _check("using the checkout costs ZERO GitHub API calls",
                     repo2.contents_calls == 0 and repo2.blob_calls == 0)

        # Path containment still enforced on the read path. The escape target
        # must genuinely EXIST outside the checkout, otherwise dropping the
        # guard merely fails the isfile() check and falls through to the API,
        # and the test passes for the wrong reason.
        outside = os.path.join(os.path.dirname(d), "ab-secret-probe.txt")
        with open(outside, "w") as fh:
            fh.write("TOP-SECRET-OUTSIDE-THE-REPO")
        try:
            rel = os.path.relpath(outside, d)  # e.g. ../ab-secret-probe.txt
            repo_t = _Repo(blob_text="from-api")
            bad, bad_err = read(repo_t, "deadbeef", rel, checkout_path=d)
            ok &= _check("a traversal path cannot read a file outside the checkout",
                         bad != "TOP-SECRET-OUTSIDE-THE-REPO")
        finally:
            os.remove(outside)

        # A path absent from the checkout must fall through to the API, not fail.
        repo3 = _Repo(blob_text="from-api")
        t3, e3 = read(repo3, "deadbeef", "not/here.js", checkout_path=d)
        ok &= _check("a file missing from the checkout falls back to the API",
                     e3 is None and t3 == "from-api" and repo3.contents_calls == 1)

    # Failure modes stay soft — the contract is "never raises".
    t4, e4 = read(_Repo(blob_text=None, blob_raises=True), "deadbeef", "big.js")
    ok &= _check("a blob-API failure degrades to an error, not an exception",
                 t4 is None and e4 and "no fetchable content" in e4)

    class _NoSha:
        @property
        def decoded_content(self):
            raise AssertionError("unsupported encoding: none")

    t5, e5 = read(_Repo(content_file=_NoSha()), "deadbeef", "big.js")
    ok &= _check("a content object with no sha degrades to an error",
                 t5 is None and bool(e5))

    class _Binary:
        sha = "s"
        decoded_content = b"\xff\xfe\x00binary"

    t6, e6 = read(_Repo(content_file=_Binary()), "deadbeef", "x.bin")
    ok &= _check("non-UTF-8 content is reported as binary, not crashed on",
                 t6 is None and "not valid UTF-8" in (e6 or ""))

    out_err = fetch(_Repo(content_file=_NoSha()), "deadbeef", "big.js")
    ok &= _check("the tool executor still returns an error DICT (never raises)",
                 isinstance(out_err, dict) and "error" in out_err)

    # ------------------------------------------------------ 2. RETRIEVAL
    # The heart of lm#444: fetching the bytes is not enough.
    repo4 = _Repo(blob_text=big)
    plain = fetch(repo4, "deadbeef", "WebUI/main.js")
    ok &= _check("without a pattern the reply is truncated (head only)",
                 plain.get("truncated") is True)
    ok &= _check("without a pattern the real signature is NOT visible "
                 "— this is precisely why the model invented an anchor",
                 "async function setTenant(tenant)" not in plain.get("content", ""))
    ok &= _check("the reply reports total_lines so the model can tell it saw a fraction",
                 plain.get("total_lines", 0) > 16000)

    windowed = fetch(repo4, "deadbeef", "WebUI/main.js", pattern="function setTenant")
    ok &= _check("WITH a pattern the real signature IS visible",
                 "async function setTenant(tenant) {" in windowed.get("content", ""))
    ok &= _check("the windowed reply is flagged as windowed, not silently partial",
                 windowed.get("windowed") is True)
    ok &= _check("the windowed reply still respects the char budget",
                 len(windowed.get("content", "")) <= 20000 + 200)
    ok &= _check("the hallucinated signature is absent (it never existed)",
                 "setTenant(tenantName)" not in windowed.get("content", ""))

    missing = fetch(repo4, "deadbeef", "WebUI/main.js", pattern="setTenant(tenantName)")
    ok &= _check("a pattern that does not exist is reported as not-found, "
                 "not as an empty file",
                 "error" in missing and "not found" in missing["error"])
    ok &= _check("the not-found reply still reports total_lines",
                 missing.get("total_lines", 0) > 16000)

    # ------------------------------------------------- 3. CALL-SITE WIRING
    # Everything above would still pass if the production caller never passed
    # the checkout or the pattern — so pin the real call site.
    src = open(FIX_ENGINE).read()
    tree = ast.parse(src)
    turn = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_run_reviewer_turn"][0]
    call = None
    for node in ast.walk(turn):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "_fetch_repo_file_for_review"):
            call = node
    kw = {k.arg for k in call.keywords} if call else set()
    ok &= _check("the reviewer call site passes checkout_path=", "checkout_path" in kw)
    ok &= _check("the reviewer call site passes pattern=", "pattern" in kw)
    ok &= _check("checkout_path is wired to the turn's repo_checkout_path arg",
                 any(k.arg == "checkout_path"
                     and getattr(k.value, "id", "") == "repo_checkout_path"
                     for k in (call.keywords if call else [])))
    # Presence of the keyword is not enough: `pattern=None` would satisfy it
    # while silently disabling windowing, which is the whole fix. Pin that the
    # value is actually derived from the model's tool arguments.
    _pat_kw = next((k for k in (call.keywords if call else []) if k.arg == "pattern"), None)
    _pat_src = ast.get_source_segment(src, _pat_kw.value) if _pat_kw else ""
    ok &= _check("pattern= is derived from the reviewer's tool args, not hard-coded",
                 "args" in (_pat_src or ""))

    ok &= _check("the tool schema advertises the pattern argument",
                 '"pattern"' in src and "pattern" in src.split("_REVIEW_TOOLS")[1][:2000])

    review = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "review_fix"][0]
    review_src = ast.get_source_segment(src, review)
    ok &= _check("an already-existing checkout is reused (not cloned) for reviewers",
                 "_wants_clone or (repo_path and os.path.isdir(repo_path))" in review_src)

    print("\n" + ("ALL CASES PASSED" if ok else "SOME CASES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

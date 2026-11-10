"""Provider credential failures, and the claude_cli auth sentinel that fired on
the model's own words.

Two issues, one theme.

ab#190 -- "Reviewer (copilot) failed: 403 Client Error: Forbidden for url:
https://api.githubcopilot.com/chat/completions". A token without access to that
endpoint is a configuration state an operator fixes in Settings; the reviewer
panel already degrades gracefully, with the remaining reviewers carrying the
verdict. At ERROR it was harvested by AppBuilder's own log scanner and filed as
a bug against AppBuilder, which no code change could ever close.

ab#17 is worse than it looks, and is a genuine functional defect rather than
log noise. The claude_cli auth check was:

    if data.get("is_error") or ("Not logged in" in text or "/login" in text):
        raise Exception("Claude CLI not authenticated on this server. ...")

`text` is the MODEL'S OWN GENERATED CONTENT. So any review whose critique
mentions a login route -- i.e. any review of authentication code, and AppBuilder
itself serves /login -- was classified as an auth failure. The issue's own log
line proves it: the "Raw:" payload is a complete, valid
{"verdict": "Approve", "confidence": 0.91, "critique": "Verified against the
checkout..."}. A good review was thrown away, and the reviewer was reported as
unauthenticated, because of a substring in its prose.

The same contamination existed on the non-zero-exit path, which scanned
`output` -- the JSON envelope, which carries the model's content.
"""
import ast
import json
import re
import sys

LLM = "llm_client.py"
FE = "fix_engine.py"


def _load_auth():
    src = open(LLM).read()
    tree = ast.parse(src)
    ns = {"re": re}
    for node in tree.body:
        seg = ast.get_source_segment(src, node) or ""
        if isinstance(node, ast.Assign) and "_LLM_AUTH" in seg:
            exec(compile(ast.Module(body=[node], type_ignores=[]), LLM, "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name in (
                "is_llm_cooldown_error", "is_llm_auth_error"):
            exec(compile(ast.Module(body=[node], type_ignores=[]), LLM, "exec"), ns)
    assert "is_llm_auth_error" in ns, f"is_llm_auth_error missing from {LLM}"
    assert "is_llm_cooldown_error" in ns, f"is_llm_cooldown_error missing from {LLM}"
    return ns["is_llm_auth_error"]


def _claude_block():
    """The live auth-detection source from _call_claude_cli-style handling."""
    src = open(LLM).read()
    i = src.index("# Detect a CLI-level failure from the JSON envelope.")
    # Stop at the enclosing try's handler: anything past it is the
    # non-zero-exit path, which is checked separately below.
    j = src.index("except json.JSONDecodeError:", i)
    return src[i:j]


def main():
    auth = _load_auth()
    fails = []

    def check(label, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    print("Running provider-credential / claude-auth self-test...")

    # ── is_llm_auth_error ──────────────────────────────────────────────────
    check("ab#190's Copilot 403 is an auth error",
          auth(Exception("403 Client Error: Forbidden for url: "
                         "https://api.githubcopilot.com/chat/completions — forbidden")) is True)
    check("ab#17's claude message is an auth error",
          auth(Exception("Claude CLI not authenticated on this server. Go to Settings")) is True)
    check("a 401 is an auth error", auth(Exception("401 Unauthorized")) is True)
    check("an invalid API key is an auth error", auth(Exception("Invalid API key")) is True)
    check("a missing scope is an auth error", auth(Exception("missing scope: workflow")) is True)

    # A cooldown must NOT be reported as auth: it would send an operator off to
    # re-authenticate a provider that is perfectly well authenticated.
    check("a rate-limit cooldown is NOT an auth error",
          auth(Exception("rate_limited: retry later")) is False)
    check("a cooldown carrying a 403 is still NOT an auth error",
          auth(Exception("credit_cooldown 403")) is False)

    # Word-boundary matching on the status codes.
    check("a token count containing 403 is not a status code",
          auth(Exception("processed 14031 tokens ok")) is False)
    check("a commit sha containing 403 is not a status code",
          auth(Exception("commit a403bc1 failed to build")) is False)

    check("a network error is not an auth error",
          auth(Exception("connection reset by peer")) is False)
    check("a 500 is not an auth error", auth(Exception("500 Internal Server Error")) is False)
    check("None does not raise", auth(None) is False)
    check("an empty message does not raise", auth(Exception("")) is False)

    # ── the reviewer log level ─────────────────────────────────────────────
    fsrc = open(FE).read()
    ftree = ast.parse(fsrc)
    node = next(n for n in ast.walk(ftree)
                if isinstance(n, ast.FunctionDef) and n.name == "_log_hard_failure")
    body = ast.get_source_segment(fsrc, node)
    check("the reviewer failure path consults is_llm_auth_error",
          "is_llm_auth_error(e)" in body)
    check("an auth failure is logged at WARNING", "logger.warning" in body)
    check("the auth branch runs BEFORE the generic ERROR",
          body.index("is_llm_auth_error(e)") < body.index("logger.error(f\"{r['name']} failed"))
    check("a genuine failure still logs at ERROR",
          "logger.error(f\"{r['name']} failed" in body)
    check("the cooldown branch is still first",
          body.index("is_llm_cooldown_error(e)") < body.index("is_llm_auth_error(e)"))
    check("is_llm_auth_error is imported into fix_engine",
          "is_llm_auth_error," in fsrc)

    # ── ab#17: the sentinel must not read the model's content ──────────────
    block = _claude_block()
    check("the auth raise is gated on is_error only",
          'if data.get("is_error"):' in block)
    check("the OLD contaminated condition is gone",
          'data.get("is_error") or (' not in block)
    # The critical pin: a valid verdict must not be able to trigger the auth
    # raise just by containing the word /login in the critique.
    approve = json.dumps({"verdict": "Approve", "confidence": 0.91,
                          "critique": "The /login route is unchanged; Not logged in "
                                      "users are redirected correctly."})
    ns = {"data": {"is_error": False}, "text": approve, "stderr": "",
          "json": json, "Exception": Exception}
    raised = None
    try:
        exec(compile(ast.parse(block.replace("\n            ", "\n")), "<blk>", "exec"), ns)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("a valid Approve verdict mentioning /login does NOT raise an auth error",
          raised is None)

    ns_err = {"data": {"is_error": True}, "text": "Invalid API key · Please run /login",
              "stderr": "", "json": json, "Exception": Exception}
    raised2 = None
    try:
        exec(compile(ast.parse(block.replace("\n            ", "\n")), "<blk>", "exec"), ns_err)
    except Exception as exc:  # noqa: BLE001
        raised2 = exc
    check("a real is_error auth payload still raises the auth message",
          raised2 is not None and "not authenticated" in str(raised2).lower())

    ns_other = {"data": {"is_error": True}, "text": "the model refused to answer",
                "stderr": "", "json": json, "Exception": Exception}
    raised3 = None
    try:
        exec(compile(ast.parse(block.replace("\n            ", "\n")), "<blk>", "exec"), ns_other)
    except Exception as exc:  # noqa: BLE001
        raised3 = exc
    check("a NON-auth is_error is reported as itself, not as an auth failure",
          raised3 is not None and "not authenticated" not in str(raised3).lower())

    # ── the non-zero-exit path ─────────────────────────────────────────────
    lsrc = open(LLM).read()
    check("the exit path no longer scans the JSON envelope unconditionally",
          '"Not logged in" in (output + stderr)' not in lsrc)
    check("the exit path only scans output when the envelope did NOT parse",
          "exit_blob = stderr if envelope_parsed else" in lsrc)
    check("envelope_parsed is initialised before the parse attempt",
          lsrc.index("envelope_parsed = False") < lsrc.index("envelope_parsed = True"))

    print(f"RESULT: {'PASS' if not fails else 'FAIL — ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

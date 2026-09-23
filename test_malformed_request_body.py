"""A malformed client body must be 400, not an UNCAUGHT EXCEPTION.

WHY (ab#39): the log line was

    UNCAUGHT EXCEPTION: Did not find CR at end of boundary (48)

That is Starlette's multipart parser rejecting the bytes that arrived on the
wire. The body was not valid multipart -- overwhelmingly an internet scanner
probing the upload endpoints, or a truncated upload. Nothing about AppBuilder
is wrong, and no code change to AppBuilder can stop a stranger sending a
malformed POST. But it surfaced as a 500 logged at ERROR with a full traceback,
so AppBuilder's own log scanner harvested it and filed it as a bug against
AppBuilder -- one it could never close.

The ordering detail this pins: catch_exceptions_mid is registered AFTER
log_requests, and Starlette runs the most recently registered middleware
outermost. So log_requests is INNER and logs first. Downgrading only the outer
handler would have left log_requests' `logger.exception` emitting the ERROR and
traceback anyway, and the scanner would simply have filed that line instead --
a fix that looks complete and changes nothing observable.
"""
import ast
import sys

SRC = "main.py"


def _load():
    src = open(SRC).read()
    tree = ast.parse(src)
    ns = {}
    wanted = ("_MALFORMED_BODY_EXC",)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.Try)):
            seg = ast.get_source_segment(src, node) or ""
            if "_MALFORMED_BODY_EXC" in seg:
                exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
        elif isinstance(node, ast.FunctionDef) and node.name == "_is_malformed_request_body":
            exec(compile(ast.Module(body=[node], type_ignores=[]), SRC, "exec"), ns)
    assert "_is_malformed_request_body" in ns, f"_is_malformed_request_body missing from {SRC}"
    assert all(w in ns for w in wanted), f"{wanted} missing from {SRC}"
    return ns["_is_malformed_request_body"], ns["_MALFORMED_BODY_EXC"]


def _src_of(name, kind=ast.AsyncFunctionDef):
    src = open(SRC).read()
    tree = ast.parse(src)
    node = next(n for n in ast.walk(tree) if isinstance(n, kind) and n.name == name)
    return ast.get_source_segment(src, node)


def main():
    malformed, exc_tuple = _load()
    fails = []

    def check(label, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            fails.append(label)

    print("Running malformed-request-body self-test...")

    # ── the real exception types ───────────────────────────────────────────
    try:
        from starlette.formparsers import MultiPartException
        check("Starlette's MultiPartException is in the isinstance tuple",
              MultiPartException in exc_tuple)
        check("ab#39's exact exception is classified",
              malformed(MultiPartException("Did not find CR at end of boundary (48)")) is True)
    except ImportError:
        check("starlette.formparsers importable (skipped otherwise)", True)
    try:
        from starlette.requests import ClientDisconnect
        check("ClientDisconnect is in the isinstance tuple", ClientDisconnect in exc_tuple)
        check("a client that hangs up mid-upload is classified",
              malformed(ClientDisconnect()) is True)
    except ImportError:
        check("starlette.requests importable (skipped otherwise)", True)

    # ── the text fallback, for when Starlette moves these classes ──────────
    check("ab#39's message alone is enough",
          malformed(Exception("Did not find CR at end of boundary (48)")) is True)
    check("a missing boundary is classified",
          malformed(Exception("Missing boundary in multipart.")) is True)
    check("an invalid boundary is classified",
          malformed(Exception("Did not find a valid boundary")) is True)
    check("matching is case-insensitive",
          malformed(Exception("DID NOT FIND CR AT END OF BOUNDARY (48)")) is True)

    # ── genuine faults must still be 500s ──────────────────────────────────
    check("an unrelated ValueError is NOT malformed", malformed(ValueError("boom")) is False)
    check("a KeyError is NOT malformed", malformed(KeyError("body")) is False)
    check("a ZeroDivisionError is NOT malformed",
          malformed(ZeroDivisionError("division by zero")) is False)
    check("None does not raise", malformed(None) is False)
    check("an empty message does not raise", malformed(Exception("")) is False)

    # ── the outer handler ──────────────────────────────────────────────────
    outer = _src_of("catch_exceptions_mid")
    check("the outer handler consults the classifier",
          "_is_malformed_request_body(e)" in outer)
    check("the outer handler returns 400 for a malformed body",
          "status_code=400" in outer)
    check("the outer handler still returns 500 for real faults",
          "status_code=500" in outer)
    check("the malformed branch runs BEFORE the UNCAUGHT EXCEPTION log",
          outer.index("_is_malformed_request_body(e)") < outer.index("UNCAUGHT EXCEPTION"))
    check("no traceback is formatted for a malformed body",
          outer.index("_is_malformed_request_body(e)") < outer.index("traceback.format_exc()"))
    check("the malformed branch logs at WARNING", "logger.warning" in outer)
    check("the UNCAUGHT EXCEPTION ERROR log is still present for real faults",
          'logger.error(f"UNCAUGHT EXCEPTION' in outer)

    # ── the INNER handler: the half that is easy to miss ───────────────────
    inner = _src_of("log_requests")
    check("the inner middleware also consults the classifier",
          "_is_malformed_request_body(e)" in inner)
    check("the inner middleware does NOT logger.exception a malformed body",
          inner.index("_is_malformed_request_body(e)") < inner.index("logger.exception"))
    check("the inner middleware still logger.exceptions real faults",
          "logger.exception" in inner)
    check("the inner middleware still re-raises so the outer handler responds",
          "raise e" in inner)

    # Registration order is what makes the inner guard necessary at all.
    src = open(SRC).read()
    check("log_requests is registered BEFORE catch_exceptions_mid (so it is inner)",
          src.index("async def log_requests") < src.index("async def catch_exceptions_mid"))

    print(f"RESULT: {'PASS' if not fails else 'FAIL — ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Self-test for the OpenRouter model-listing branch of _fetch_models_for_provider.

Run:  python3 ab/test_openrouter_free_router.py

workers.py cannot be imported directly (it pulls in main.py's FastAPI app), so
this extracts the SOURCE of the pure function via ast and execs it with a
stubbed requests module.

Regression guard: "openrouter/free" (the Free Models Router — routes each
request to a random available :free model) does NOT appear in OpenRouter's own
/models listing (confirmed live against the real API: it returns only
"openrouter/auto-beta", a different, paid router), even though it's a valid,
documented model id. _fetch_models_for_provider must inject it and pin it
first in the returned list so it's always selectable in the Settings model
dropdown.
"""
import ast


def _load_fetch_models():
    src = open("workers.py").read()
    tree = ast.parse(src)
    want = {"_fetch_models_for_provider", "_model_fetch_reason"}
    want_assign = {"OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
                   "GOOGLE_BASE_URL", "ANTHROPIC_API_VERSION"}
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assign:
                    segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class FakeResp:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data
        def raise_for_status(self):
            pass

    # The REAL live shape confirmed against OpenRouter's /api/v1/models: no
    # "openrouter/free", but it DOES include "openrouter/auto-beta" (a
    # different router) plus ordinary vendor-prefixed models.
    live_data = {
        "data": [
            {"id": "openrouter/auto-beta", "name": "Auto Router (Beta)"},
            {"id": "anthropic/claude-3.5-sonnet", "name": "Claude 3.5 Sonnet"},
            {"id": "meta-llama/llama-3.1-70b-instruct", "name": "Llama 3.1 70B Instruct"},
        ]
    }

    class FakeRequests:
        @staticmethod
        def get(url, headers=None, timeout=10):
            return FakeResp(live_data)

    ns = {
        "requests": FakeRequests,
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com/v1",
        "GOOGLE_BASE_URL": "https://generativelanguage.googleapis.com",
        "ANTHROPIC_API_VERSION": "2023-06-01",
        "_is_lmstudio": lambda p: False,
        "_is_ollama": lambda p: False,
        "logger": _NoLog(),
    }
    exec("\n\n".join(segs), ns)
    return ns["_fetch_models_for_provider"], live_data


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    ok = True
    fetch, live_data = _load_fetch_models()

    result = fetch("openrouter", "fake-key", None)
    models = result["models"]
    names = [m["name"] for m in models]

    ok &= _check("openrouter/free is present", "openrouter/free" in names)
    ok &= _check("openrouter/free is FIRST in the list",
                 bool(models) and models[0]["name"] == "openrouter/free")
    ok &= _check("openrouter/free's details mention the Free Models Router",
                 "Free Models Router" in models[0].get("details", ""))
    ok &= _check("no duplicate openrouter/free entry", names.count("openrouter/free") == 1)
    ok &= _check("live-listed models are still present (nothing dropped)",
                 "openrouter/auto-beta" in names
                 and "anthropic/claude-3.5-sonnet" in names
                 and "meta-llama/llama-3.1-70b-instruct" in names)

    # Idempotency: if OpenRouter ever starts listing it live too, still exactly
    # one entry, still pinned first — not doubled.
    live_data["data"].append({"id": "openrouter/free", "name": "Free (already listed live)"})
    result2 = fetch("openrouter", "fake-key", None)
    models2 = result2["models"]
    names2 = [m["name"] for m in models2]
    ok &= _check("idempotent if OpenRouter starts listing it live too",
                 names2.count("openrouter/free") == 1 and models2[0]["name"] == "openrouter/free")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running OpenRouter Free Models Router self-test...")
    import sys
    sys.exit(main())

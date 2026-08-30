#!/usr/bin/env python3
"""Self-test for llm_client's per-entry health tracking (Settings' "failing"
badge on an LLM entry).

Run:  python3 test_llm_client_entry_health.py

llm_client.py can't be imported directly (same circular-import chain as
log_scan.py — see test_log_scan_requirements.py's docstring), so this
extracts the health-tracking functions via ast and execs them in a minimal
namespace, the established convention in this repo.

Regression context: lm#452/#469/#444 sat in an endless hourly review-retry
loop because one configured reviewer entry (Copilot paired with a model its
API rejects on every call) failed silently — nothing in Settings showed it
was broken. _record_llm_success/_record_llm_failure/get_llm_entry_health are
wired into _call_provider_timed (every LLM call in the app routes through
it) so a hard-failing entry becomes visible without reading ab.log."""
import ast
import logging
import threading
import time
from datetime import datetime


def _load_ns():
    src = open("llm_client.py").read()
    tree = ast.parse(src)
    names = {"_model_key", "_record_llm_success", "_record_llm_failure",
              "get_llm_entry_health", "_entry_is_unhealthy",
              "_is_unsupported_model_error"}
    ns = {"threading": threading, "datetime": datetime, "time": time,
          "logger": logging.getLogger("entry-health-selftest")}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(ast.get_source_segment(src, node), ns)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ("_ENTRY_HEALTH_LOCK", "_ENTRY_HEALTH",
                                                  "_ENTRY_UNHEALTHY_THRESHOLD",
                                                  "_ENTRY_UNHEALTHY_RETRY_SECONDS",
                                                  "_ENTRY_UNSUPPORTED_RETRY_SECONDS",
                                                  "_UNSUPPORTED_MODEL_MARKERS")
            for t in node.targets
        ):
            exec(ast.get_source_segment(src, node), ns)
    missing = names - set(ns)
    assert not missing, f"llm_client.py's shape changed — missing: {missing}"
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def main():
    print("Running llm_client per-entry health self-test...")
    ok = True
    ns = _load_ns()
    record_success = ns["_record_llm_success"]
    record_failure = ns["_record_llm_failure"]
    get_health = ns["get_llm_entry_health"]

    # ── an entry with no calls yet reads as healthy, not unhealthy ──────────
    h = get_health("copilot", "", "untouched-model")
    ok &= _check("an entry with no recorded calls is healthy (silence != failure)",
                h["unhealthy"] is False and h["consecutive_failures"] == 0)

    # ── the actual regression: an entry failing on EVERY call ───────────────
    # Uses a GENERIC error on purpose: this block guards the blip-tolerance
    # threshold, which must stay >1 for failures we cannot attribute to a
    # permanent cause. The model-rejection fast path is asserted separately
    # below (it deliberately does NOT wait for a second failure).
    key = ("copilot", "", "grok-4.6")
    ok &= _check("a single failure does not yet flag the entry (avoids one-off blips)",
                not get_health(*key)["unhealthy"])
    record_failure(key, Exception("500 Internal Server Error from the provider"))
    h1 = get_health(*key)
    ok &= _check("after 1 failure: still not flagged (threshold is >1)",
                not h1["unhealthy"] and h1["consecutive_failures"] == 1)
    record_failure(key, Exception("500 Internal Server Error (again)"))
    h2 = get_health(*key)
    ok &= _check("after 2 consecutive failures: flagged unhealthy",
                h2["unhealthy"] and h2["consecutive_failures"] == 2)
    ok &= _check("the last error message is captured for the badge tooltip",
                "Internal Server Error" in (h2["last_error"] or ""))
    ok &= _check("last_error_at is stamped",
                bool(h2["last_error_at"]))

    # ── a model the endpoint REFUSES to serve is conclusive on the first
    # failure. Waiting for a second (and then retrying hourly forever) is what
    # let one mis-set reviewer model keep being re-picked, failing the panel
    # and filing a duplicate log-alert issue every hour — ab#119/#121/#125/#135
    # are all the same fact. No retry can change the provider's answer. ──────
    unsup = ("copilot", "", "grok-4.5")
    record_failure(unsup, Exception('400 unsupported_api_for_model: model "grok-4.5" is not '
                                    "accessible via the /chat/completions endpoint"))
    hu = get_health(*unsup)
    ok &= _check("model rejection flags the entry on the FIRST failure",
                hu["unhealthy"] and hu["consecutive_failures"] == 1)
    with ns["_ENTRY_HEALTH_LOCK"]:
        _entry = ns["_ENTRY_HEALTH"][unsup]
    ok &= _check("marked as a model-rejection, not a generic failure",
                _entry.get("unsupported_model") is True)
    ok &= _check("excluded far longer than the ordinary hourly retry",
                _entry["retry_after"] - time.time()
                > ns["_ENTRY_UNHEALTHY_RETRY_SECONDS"])
    ok &= _check("cooldown matches the unsupported-model constant",
                abs((_entry["retry_after"] - time.time())
                    - ns["_ENTRY_UNSUPPORTED_RETRY_SECONDS"]) < 5)
    # An operator correcting the model yields a DIFFERENT key, which carries
    # none of this state — that is what stops the block being a dead end.
    fixed = get_health("copilot", "", "gpt-5.5-corrected")
    ok &= _check("correcting the model produces a clean, routable entry",
                not fixed["unhealthy"] and fixed["consecutive_failures"] == 0)
    # ...and a genuine success still clears it outright.
    record_success(unsup)
    ok &= _check("a later success clears the model-rejection block",
                not get_health(*unsup)["unhealthy"])

    # ── a success resets the streak — a since-fixed entry stops being flagged
    record_success(key)
    h3 = get_health(*key)
    ok &= _check("a success after failures clears the unhealthy flag",
                not h3["unhealthy"] and h3["consecutive_failures"] == 0)
    ok &= _check("a success clears the stale last_error too",
                h3["last_error"] is None)

    # ── an unrelated entry is unaffected by another entry's failures ────────
    other = get_health("ollama_cloud", "", "nemotron-3-ultra")
    ok &= _check("a distinct (provider, base_url, model) key is tracked independently",
                not other["unhealthy"] and other["consecutive_failures"] == 0)

    # ── timed recovery: unhealthy is not permanent (see _entry_is_unhealthy's
    # docstring — a one-shot exclusion would be permanent, since an excluded
    # entry can never be called again to record the success that clears it) ─
    is_unhealthy = ns["_entry_is_unhealthy"]
    retry_key = ("copilot", "", "gpt-5.5")
    record_failure(retry_key, Exception("boom"))
    record_failure(retry_key, Exception("boom again"))
    ok &= _check("2 consecutive failures -> unhealthy", is_unhealthy(retry_key))
    with ns["_ENTRY_HEALTH_LOCK"]:
        ns["_ENTRY_HEALTH"][retry_key]["retry_after"] = time.time() + 3600
    ok &= _check("still within the retry window -> stays unhealthy", is_unhealthy(retry_key))
    with ns["_ENTRY_HEALTH_LOCK"]:
        ns["_ENTRY_HEALTH"][retry_key]["retry_after"] = time.time() - 1
    ok &= _check("retry_after elapsed -> no longer reported unhealthy (one fresh attempt allowed)",
                not is_unhealthy(retry_key))

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())

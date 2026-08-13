#!/usr/bin/env python3
"""Self-test for model_selection.explain_selection() — the pure dry-run picker
that backs Diagnostics → LLM picker. Verifies every candidate is classified
(selected / alternative / excluded) with a faithful reason, so an operator can
audit routing for a requirement set without spending a token.

Run:  python3 bugfixer/test_model_selection_explain.py

Standalone: imports only model_selection (which imports model_registry).
"""
import sys

import model_selection as sel


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _caps(cost_tier="free", complexity="small", context_window=32768, **overrides):
    caps = {
        "cost_tier": cost_tier, "max_complexity": complexity, "context_window": context_window,
        "supports_tools": False, "native_agentic_tools": False, "supports_mutating_agent": False,
        "supports_structured_output": False, "supports_streaming": False,
    }
    caps.update(overrides)
    return caps


def _cand(key, provider="ollama", model="m", base_url="http://x", available=True, **caps_overrides):
    return {
        "key": key, "provider": provider, "model": model, "base_url": base_url,
        "api_key": "", "rpm": 0, "available": available, "caps": _caps(**caps_overrides),
    }


def _row(res, key):
    return next((r for r in res["rows"] if r["key"] == key), None)


def main():
    print("Running bugfixer model_selection.explain_selection self-test...")
    ok = True
    R = sel.LlmRequirements

    # --- the winner is labelled 'selected' and echoed in .selected ----------
    free_c = _cand("f", cost_tier="free")
    cheap_c = _cand("c", cost_tier="cheap")
    frontier_c = _cand("fr", cost_tier="frontier")
    res = sel.explain_selection(R(), [frontier_c, cheap_c, free_c])
    ok &= _check("selected echoes the cheapest capable candidate",
                 res["selected"] is not None and res["selected"]["key"] == "f")
    ok &= _check("the winning row is status=selected", _row(res, "f")["status"] == "selected")
    ok &= _check("selected row is first in the ordered rows", res["rows"][0]["key"] == "f")

    # --- a costlier capable candidate is an 'alternative', not excluded ------
    ok &= _check("a pricier capable tier is offered as an alternative",
                 _row(res, "c")["status"] == "alternative" and _row(res, "fr")["status"] == "alternative")

    # --- every candidate is accounted for (one row each) --------------------
    ok &= _check("explain returns exactly one row per candidate", len(res["rows"]) == 3)

    # --- excluded candidates carry a faithful reason ------------------------
    res = sel.explain_selection(R(needs_tools=True), [_cand("a", cost_tier="cheap", supports_tools=False),
                                                      _cand("b", cost_tier="cheap", supports_tools=True)])
    ok &= _check("winner is the tool-capable candidate", res["selected"]["key"] == "b")
    ra = _row(res, "a")
    ok &= _check("tool-incapable candidate is excluded with a tool reason",
                 ra["status"] == "excluded" and "tool" in ra["reason"])

    res = sel.explain_selection(R(min_context_tokens=100000),
                                [_cand("a", context_window=32768)])
    ok &= _check("context-starved candidate excluded with a context reason and no winner",
                 res["selected"] is None and "context" in _row(res, "a")["reason"])

    res = sel.explain_selection(R(complexity="large"), [_cand("a", complexity="small")])
    ok &= _check("under-complex candidate excluded with a complexity reason",
                 "max_complexity" in _row(res, "a")["reason"])

    res = sel.explain_selection(R(), [_cand("a", available=False)])
    ok &= _check("unavailable candidate excluded with its unavailable reason",
                 _row(res, "a")["status"] == "excluded" and "unavailable" in _row(res, "a")["reason"])

    res = sel.explain_selection(R(exclude_models=("a",)), [_cand("a", cost_tier="free"),
                                                           _cand("b", cost_tier="free")])
    ok &= _check("a model burned this run is excluded, the other is selected",
                 res["selected"]["key"] == "b" and "already tried" in _row(res, "a")["reason"])

    # --- the permissive pass is flagged when an unclassified model wins -----
    res = sel.explain_selection(R(), [_cand("u", cost_tier="unknown")])
    ok &= _check("an unclassified-only pool resolves on the permissive pass and is flagged",
                 res["selected"] is not None and res["permissive"] is True)

    # --- perf figures are attached per row ----------------------------------
    perf = {"f": {"n": 5, "tps": 40.0, "latency_ms": 120.0}}
    res = sel.explain_selection(R(), [_cand("f", cost_tier="free")], perf=perf)
    ok &= _check("perf samples (n/tps/latency) are surfaced on the row",
                 _row(res, "f")["n"] == 5 and _row(res, "f")["tps"] == 40.0
                 and _row(res, "f")["latency_ms"] == 120.0)

    # --- empty pool: no winner, no rows, no crash ---------------------------
    res = sel.explain_selection(R(), [])
    ok &= _check("empty candidate pool -> no selection, no rows",
                 res["selected"] is None and res["rows"] == [])
    res = sel.explain_selection(R(), None)
    ok &= _check("None candidate pool -> no crash", res["selected"] is None and res["rows"] == [])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())

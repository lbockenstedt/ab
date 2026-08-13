#!/usr/bin/env python3
"""Self-test for feature_boundary.py — the deterministic Stage A prefilter
for BugFixer's feature auto-drive classifier, and the boundary_hits() reuse
that gates auto-merge against a real PR diff.

Run:  python3 bugfixer/test_feature_boundary.py

Standalone: imports only feature_boundary (no app/main init).
"""
import sys

import feature_boundary as fb


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


def main():
    print("Running bugfixer feature_boundary self-test...")
    ok = True

    boundaries = [
        {"id": "psk", "label": "PSK", "rule": "Never hardcode a PSK.",
         "paths": ["**/hub_agent.py"], "keywords": ["psk", "pre-shared key"],
         "hard": True, "enabled": True},
        {"id": "soft-rule", "label": "Soft rule", "rule": "Prefer not to touch X.",
         "paths": ["**/soft_module.py"], "keywords": ["softword"],
         "hard": False, "enabled": True},
        {"id": "disabled-rule", "label": "Disabled", "rule": "Would match but is off.",
         "paths": ["**/anything.py"], "keywords": ["psk"],
         "hard": True, "enabled": False},
    ]

    # --- boundary_hits (path-based, used by the auto-merge gate) -----------

    ok &= _check("exact path match hits",
                bool(fb.boundary_hits(["bugfixer/hub_agent.py"], boundaries)))
    ok &= _check("exact path match reports the matched path",
                fb.boundary_hits(["bugfixer/hub_agent.py"], boundaries)[0]["_matched_paths"]
                == ["bugfixer/hub_agent.py"])
    ok &= _check("unrelated path does not hit",
                fb.boundary_hits(["bugfixer/routes.py"], boundaries) == [])
    ok &= _check("disabled boundary never hits even with a matching path",
                fb.boundary_hits(["anything.py"], boundaries) == [])
    ok &= _check("empty path list yields no hits",
                fb.boundary_hits([], boundaries) == [])
    ok &= _check("empty boundary list yields no hits",
                fb.boundary_hits(["bugfixer/hub_agent.py"], []) == [])
    ok &= _check("multiple matching paths land in the same hit's _matched_paths",
                sorted(fb.boundary_hits(
                    ["a/hub_agent.py", "lm/hub_agent.py", "unrelated.py"], boundaries
                )[0]["_matched_paths"]) == ["a/hub_agent.py", "lm/hub_agent.py"])

    # --- prefilter (text-based, Stage A on the raw issue title/body) -------

    hard = fb.prefilter("Add a button", "Please hardcode a PSK for the new spoke.", boundaries)
    ok &= _check("keyword match on 'hardcode a PSK' is a hard hit",
                hard["hard"] is True and len(hard["hits"]) == 1
                and hard["hits"][0]["id"] == "psk")

    soft = fb.prefilter("Add a button", "This mentions softword somewhere.", boundaries)
    ok &= _check("soft (non-hard) rule keyword match lands in soft_hits, not hits",
                soft["hard"] is False and soft["hits"] == []
                and len(soft["soft_hits"]) == 1 and soft["soft_hits"][0]["id"] == "soft-rule")

    clean = fb.prefilter("Add a dongle-clear button", "Clears missing USB dongles from the VM Server page.", boundaries)
    ok &= _check("an unrelated bolt-on request has no hits at all",
                clean["hard"] is False and clean["hits"] == [] and clean["soft_hits"] == [])

    disabled = fb.prefilter("x", "talks about psk", boundaries)
    ok &= _check("disabled boundary's keyword never contributes a hit",
                all(h["id"] != "disabled-rule" for h in disabled["hits"] + disabled["soft_hits"]))

    case = fb.prefilter("X", "We need a PRE-SHARED KEY hardcoded in.", boundaries)
    ok &= _check("keyword matching is case-insensitive",
                case["hard"] is True)

    path_in_body = fb.prefilter("Fix", "See bugfixer/hub_agent.py for context.", boundaries)
    ok &= _check("a path token mentioned in the body also triggers the boundary",
                path_in_body["hard"] is True)

    empty = fb.prefilter("", "", boundaries)
    ok &= _check("empty title/body yields no hits, no crash",
                empty == {"hard": False, "hits": [], "soft_hits": []})

    none_boundaries = fb.prefilter("hardcode a psk", "hardcode a psk", None)
    ok &= _check("boundaries=None does not crash, yields no hits",
                none_boundaries == {"hard": False, "hits": [], "soft_hits": []})

    # --- render_boundaries_for_prompt ---------------------------------------

    rendered = fb.render_boundaries_for_prompt(boundaries)
    ok &= _check("rendered prompt includes the enabled hard rule's id",
                "[psk]" in rendered)
    ok &= _check("rendered prompt excludes the disabled rule",
                "[disabled-rule]" not in rendered)
    ok &= _check("rendered prompt includes the soft rule too (still enabled)",
                "[soft-rule]" in rendered)
    ok &= _check("empty boundary list renders empty string",
                fb.render_boundaries_for_prompt([]) == "")
    ok &= _check("None renders empty string, no crash",
                fb.render_boundaries_for_prompt(None) == "")

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())

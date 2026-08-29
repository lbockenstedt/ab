#!/usr/bin/env python3
"""Self-test for the two Auto-Merge Settings controls that had no save path.

Run:  python3 ab/test_automerge_settings_controls.py
      (also collected by pytest via test_automerge_settings_controls below)

Two separate gaps this pins:

1. ``feature_automerge_require_allowlist`` had a ``config.setdefault`` on the
   settings GET but NO handler on the POST and no control in the template. It
   was therefore pinned on, and the only way to change it was hand-editing
   /etc/ab/config.json on the box. It is the switch that decides whether
   auto-merge is limited to docs/log/tooltip-only diffs or applies to any diff
   clearing the other gates, so it needs to be operable from Settings.

2. ``feature_automerge_target_branches`` was a free-text box whose comment
   claimed branches couldn't be cheaply enumerated. PROMOTE_ROUTES is exactly
   that enumeration, locally, with no GitHub API call — so it is now a checkbox
   selector like the repo one, and these pin the multi-value form handling.

The release-branch stripping below is belt-and-braces, NOT the primary defence:
pr_review._automerge_decision refuses main/master/default_branch structurally
(see test_feature_automerge_gate.py). This pins that the settings layer doesn't
even persist such a value, so a tampered/stale POST can't leave a misleading
"main" sitting in the config for a future reader to trust.

routes.py cannot be imported (main.py app-init side effects), so this reuses
test_feature_settings_roundtrip's ast-extraction harness rather than repeating
it — one copy of that stub set, not two that drift.
"""
import sys

from test_feature_settings_roundtrip import (
    _load_ns, _FakeRequest, _base_pairs, _run, _check,
)


def _save(ns, pairs, base_config=None):
    ns["_config_holder"]["config"] = dict(base_config or {})
    _run(ns["save_settings"](_FakeRequest(pairs)))
    return ns["_config_holder"]["config"]


def main():
    ok = True
    ns = _load_ns()

    # ── require_allowlist is now operable from the form ────────────────────
    saved = _save(ns, _base_pairs(feature_automerge_require_allowlist="on"))
    ok &= _check("require_allowlist ticked -> saved True",
                 saved.get("feature_automerge_require_allowlist") is True)

    # An unticked checkbox submits NOTHING, so absence must mean False — that
    # is the whole point of making it operable. (Same convention as
    # feature_automerge_require_clean directly above it in save_settings.)
    saved = _save(ns, _base_pairs(),
                  base_config={"feature_automerge_require_allowlist": True})
    ok &= _check("require_allowlist absent from form -> saved False "
                 "(unticking actually takes effect, not silently ignored)",
                 saved.get("feature_automerge_require_allowlist") is False)

    # ── target branches: checkbox selector, multi-value ────────────────────
    saved = _save(ns, _base_pairs() + [
        ("feature_automerge_target_branches", "dev"),
        ("feature_automerge_target_branches", "qa"),
    ])
    ok &= _check("both branch checkboxes ticked -> BOTH saved (getlist, not "
                 "last-value-wins, which would silently keep only 'qa')",
                 saved.get("feature_automerge_target_branches") == ["dev", "qa"])

    saved = _save(ns, _base_pairs() + [
        ("feature_automerge_target_branches", "dev"),
    ])
    ok &= _check("one branch ticked -> only that one saved",
                 saved.get("feature_automerge_target_branches") == ["dev"])

    saved = _save(ns, _base_pairs(),
                  base_config={"feature_automerge_target_branches": ["dev", "qa"]})
    ok &= _check("no branch ticked -> saved empty (unticking every box "
                 "disables auto-merge rather than preserving the old list)",
                 saved.get("feature_automerge_target_branches") == [])

    # ── the free-text escape hatch still works alongside the checkboxes ────
    saved = _save(ns, _base_pairs(feature_automerge_target_branches_extra="staging, hotfix") + [
        ("feature_automerge_target_branches", "dev"),
    ])
    ok &= _check("extra free-text branches are unioned with the ticked ones",
                 saved.get("feature_automerge_target_branches") == ["dev", "staging", "hotfix"])

    saved = _save(ns, _base_pairs(feature_automerge_target_branches_extra="dev") + [
        ("feature_automerge_target_branches", "dev"),
    ])
    ok &= _check("a branch both ticked and typed is stored once, not duplicated",
                 saved.get("feature_automerge_target_branches") == ["dev"])

    # ── release branches never persist, however they arrive ───────────────
    for _field, _label in (("feature_automerge_target_branches", "ticked/posted"),
                           ("feature_automerge_target_branches_extra", "typed free-text")):
        for _br in ("main", "master"):
            pairs = _base_pairs() + [("feature_automerge_target_branches", "dev")]
            pairs = [p for p in pairs if p[0] != _field] if _field.endswith("_extra") else pairs
            pairs = pairs + [(_field, _br)]
            saved = _save(ns, pairs)
            ok &= _check(f"{_br} arriving as {_label} is stripped, dev survives",
                         saved.get("feature_automerge_target_branches") == ["dev"])

    # The refusal follows default_branch, not the literal string "main".
    saved = _save(ns, _base_pairs(feature_automerge_target_branches_extra="release") + [
        ("feature_automerge_target_branches", "dev"),
    ], base_config={"default_branch": "release"})
    ok &= _check("a non-'main' default_branch is stripped too (follows config, "
                 "not a hardcoded name)",
                 saved.get("feature_automerge_target_branches") == ["dev"])

    print()
    print("ALL CASES PASSED" if ok else "SOME CASES FAILED")
    return 0 if ok else 1


def test_automerge_settings_controls():
    """pytest entry point.

    Without this the file is invisible to CI: ci.yml runs `pytest -q .`, which
    imports the module and collects nothing from a bare main(). An assertion
    that never executes is not a guard.
    """
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())

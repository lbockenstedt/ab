"""Every PR AppBuilder opens must state its intent.

AGENTS.md ("Pull Request Intent & Specification Rule") requires every PR in
this fleet to carry an explicit Intent & Problem Statement, and the skeptical
panel is told, in rule 4 of its own prompt, to "flag it and lower confidence"
when one is missing.

AppBuilder then opened its PRs like this:

    body="Automated fix for issue #123. Avg Confidence: 87.50%"

and promote.yml opened promotion PRs with boilerplate claiming the change
"carries code only". So AppBuilder instructed its reviewers to reject exactly
the pull requests AppBuilder itself filed -- and since remediation can only
edit FILES, never a PR description, nothing downstream could clear it. The
live evidence: dns#65, le#22, opnsense#54, cppm#34 and nw#128 were all
rejected with ZERO code findings, every critique some variant of "the
description doesn't match the diff", while qa#17 -- whose body did describe
what it carried -- approved at 0.90.

These tests hold the contract: the template renders, it round-trips, an
unfilled template does not masquerade as a stated intent, and no PR-opening
path regresses to a bare one-liner.
"""
import ast
import re

import pytest

import pr_template


# -- rendering ---------------------------------------------------------------

def test_render_emits_every_section_in_template_order():
    body = pr_template.render(intent="i", solution="s",
                              guardrails="g", verification="v")
    positions = [body.index(name) for name in pr_template.SECTIONS]
    assert positions == sorted(positions)


def test_render_marks_unstated_sections_explicitly():
    """"No guardrail concerns" and "nobody considered guardrails" must not
    look identical to a reviewer."""
    body = pr_template.render(intent="i", solution="s")
    assert "_Not stated._" in body


def test_rendered_body_is_recognised_as_following_the_template():
    assert pr_template.has_template(
        pr_template.render(intent="i", solution="s")) is True


def test_a_one_line_body_is_not_the_template():
    assert pr_template.has_template(
        "Automated fix for issue #123. Avg Confidence: 87.50%") is False


# -- extraction --------------------------------------------------------------

def test_round_trips_the_intent():
    body = pr_template.render(intent="Stop the hub dropping spokes.",
                              solution="s")
    assert pr_template.extract(body, pr_template.INTENT) == \
        "Stop the hub dropping spokes."


def test_extraction_stops_at_the_next_section():
    body = pr_template.render(intent="only this", solution="not this")
    assert "not this" not in pr_template.extract(body, pr_template.INTENT)


def test_extraction_tolerates_a_missing_emoji():
    body = "## Intent & Problem Statement\nplain heading\n"
    assert pr_template.extract(body, pr_template.INTENT) == "plain heading"


def test_extraction_tolerates_heading_level_and_trailing_colon():
    body = "### Intent & Problem Statement:\ndeeper heading\n"
    assert pr_template.extract(body, pr_template.INTENT) == "deeper heading"


def test_unfilled_template_comment_is_not_an_intent():
    """The shipped template pre-fills each section with an HTML comment. A PR
    author who writes nothing leaves it behind; treating it as content would
    let an empty template pass as a stated intent."""
    body = ("## \U0001F3AF Intent & Problem Statement\n"
            "<!-- What problem does this change solve? -->\n\n"
            "## \U0001F6E0\uFE0F Proposed Solution & Changes\n<!-- How? -->\n")
    assert pr_template.extract(body, pr_template.INTENT) == ""
    assert pr_template.stated_intent(body) == ""


def test_missing_section_extracts_empty():
    assert pr_template.extract("nothing here", pr_template.INTENT) == ""


def test_extract_handles_empty_body():
    assert pr_template.extract("", pr_template.INTENT) == ""
    assert pr_template.stated_intent(None) == ""


# -- stated_intent fallback --------------------------------------------------

def test_stated_intent_falls_back_to_the_first_paragraph():
    """A PR that ignores the template should still contribute whatever it did
    say, rather than presenting the reviewer with nothing."""
    assert pr_template.stated_intent(
        "Fixes a crash in the poller.\n\nMore detail here.") == \
        "Fixes a crash in the poller."


def test_stated_intent_skips_headings_in_the_fallback():
    assert pr_template.stated_intent("# Title\n\nReal content.") == "Real content."


def test_stated_intent_prefers_the_section_over_the_fallback():
    body = pr_template.render(intent="the real intent", solution="s")
    assert pr_template.stated_intent(body) == "the real intent"


# -- from_issue --------------------------------------------------------------

def test_from_issue_quotes_the_issue_because_it_is_the_intent():
    out = pr_template.from_issue(42, "Spokes drop", "They drop every hour.",
                                 "https://example.test/42")
    assert "#42" in out
    assert "Spokes drop" in out
    assert "> They drop every hour." in out
    assert "https://example.test/42" in out


def test_from_issue_survives_an_empty_issue_body():
    assert "#7" in pr_template.from_issue(7, "t", "", None)


def test_from_issue_truncates_a_huge_issue_body():
    out = pr_template.from_issue(1, "t", "x" * 5000)
    assert "truncated" in out
    assert len(out) < 2200


# -- the PR-opening paths must actually use it -------------------------------

@pytest.mark.parametrize("path,needle", [
    ("fix_engine.py", "_fix_pr_body"),
    ("feature_build.py", "pr_template.render"),
    ("pr_review.py", "stated_intent"),
])
def test_pr_paths_reference_the_template(path, needle):
    assert needle in open(path, encoding="utf-8").read()


def test_fix_pr_body_is_no_longer_a_bare_one_liner():
    src = open("fix_engine.py", encoding="utf-8").read()
    assert 'body=f"Automated fix for issue #{issue.number}. Avg Confidence' not in src


def test_fix_pr_body_is_defined_and_guarded():
    """PR creation must never fail because a body could not be rendered."""
    src = open("fix_engine.py", encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_fix_pr_body")
    assert any(isinstance(n, ast.Try) for n in ast.walk(fn))


def test_reviewer_prompt_states_when_intent_is_missing():
    src = open("pr_review.py", encoding="utf-8").read()
    assert "STATED INTENT" in src
    assert re.search(r"STATED INTENT: none", src)


def test_repo_template_matches_the_module_headings():
    """If someone edits .github/pull_request_template.md, extraction must
    still find the sections."""
    body = open(".github/pull_request_template.md", encoding="utf-8").read()
    for name in pr_template.SECTIONS:
        assert pr_template.extract(body, name) == "", name
        assert pr_template._section_pattern(name).search(body), name


# --- promote.yml must not conflate distinct "no intent" states -------------
#
# The state-logic panel rejected cs#144 for exactly this: four different
# situations (no originating PR, a FAILED fetch, no Intent section, an
# unfilled template) all printed "the originating change stated no intent
# section", which is misleading for the first and simply false for the second.
# A failed `gh pr view` means the intent is UNKNOWN, not absent.

def _promote_body_step():
    import yaml
    doc = yaml.safe_load(open(".github/workflows/promote.yml", encoding="utf-8"))
    steps = doc["jobs"]["promote"]["steps"]
    return next(s["run"] for s in steps
                if "Open or update" in (s.get("name") or ""))


@pytest.mark.parametrize("state", ["no-unit", "fetch-failed", "no-section",
                                   "unfilled", "ok"])
def test_promote_yml_tracks_each_intent_state(state):
    assert state in _promote_body_step()


def test_promote_yml_reports_a_failed_fetch_as_unknown_not_absent():
    run = _promote_body_step()
    assert "fetch\nfailed" in run or "GitHub fetch" in run
    assert "UNKNOWN -- not absent" in run


def test_promote_yml_does_not_mask_a_rev_list_failure_as_zero():
    """`|| echo 0` turned a failed rev-list into a truthful-looking '0
    commit(s)'. The count must be reported as unknown instead."""
    run = _promote_body_step()
    code = "\n".join(ln for ln in run.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "|| echo 0" not in code
    assert "ncommits_ok" in run
    assert "not zero" in run


def test_promote_yml_does_not_hide_the_gh_failure_with_true():
    """`gh pr view ... || true` discarded the exit status that tells a failed
    fetch apart from a PR that simply has no Intent section."""
    run = _promote_body_step()
    assert "--json body -q .body 2>/dev/null || true" not in run

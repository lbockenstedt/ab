"""The fleet PR template, as something code can write and read.

.github/pull_request_template.md asks every PR for four things: intent,
solution, guardrails, verification. Humans get prompted with it. Nothing AB
files ever followed it:

  * fix_engine opened fix PRs with a single sentence -- "Automated fix for
    issue #123. Avg Confidence: 87.50%" -- while the originating ISSUE, which
    is the authoritative statement of intent, went unmentioned.
  * promote.yml opened promotion PRs with boilerplate that claimed the change
    "carries code only".

The skeptical panel then judged a large diff against one sentence and,
correctly, refused to approve: every recent promotion rejection reads some
variant of "the description doesn't match the diff" with ZERO code findings.
dns#65, le#22, opnsense#54, cppm#34 and nw#128 were all rejected on that
ground alone, and qa#17 -- whose body did describe what it carried -- was the
only one to approve.

So the panel was never the problem. It was being asked to review intent that
nobody had written down. This module is the single place that renders and
parses that intent, so every PR-opening path states it the same way and the
reviewer reads it from the same place.
"""
import re

INTENT = "Intent & Problem Statement"
SOLUTION = "Proposed Solution & Changes"
GUARDRAILS = "Guardrails & Side-Effect Assessment"
VERIFICATION = "Verification & Parity"

# Keep these in lockstep with .github/pull_request_template.md.
_EMOJI = {
    INTENT: "\U0001F3AF",        # dart
    SOLUTION: "\U0001F6E0\uFE0F",  # hammer and wrench
    GUARDRAILS: "\U0001F6E1\uFE0F",  # shield
    VERIFICATION: "\U0001F9EA",   # test tube
}

SECTIONS = (INTENT, SOLUTION, GUARDRAILS, VERIFICATION)

_PLACEHOLDER = "_Not stated._"


def heading(name):
    """The exact heading line the template uses for ``name``."""
    emoji = _EMOJI.get(name)
    return "## %s %s" % (emoji, name) if emoji else "## %s" % name


def render(intent=None, solution=None, guardrails=None, verification=None):
    """Render a PR body in template order.

    Empty sections are still emitted, with an explicit placeholder. A missing
    section is indistinguishable from a section nobody bothered to write, and
    the reviewer should be able to tell the difference between "no guardrail
    concerns" and "nobody considered guardrails".
    """
    parts = []
    for name, text in ((INTENT, intent), (SOLUTION, solution),
                       (GUARDRAILS, guardrails), (VERIFICATION, verification)):
        body = (text or "").strip() or _PLACEHOLDER
        parts.append("%s\n\n%s" % (heading(name), body))
    return "\n\n".join(parts) + "\n"


def _section_pattern(name):
    # Tolerate the emoji being absent, extra whitespace, a trailing colon, and
    # any heading level -- a human editing the template by hand should not be
    # able to break extraction.
    return re.compile(
        r"^#{1,6}[ \t]*(?:\S+[ \t]+)?%s[ \t]*:?[ \t]*$" % re.escape(name),
        re.IGNORECASE | re.MULTILINE)


def extract(body, name):
    """Return the text under section ``name``, or "" when absent/empty.

    HTML comments are stripped: the template ships each section pre-filled with
    a `<!-- guidance -->` comment, and a PR author who writes nothing leaves
    that comment behind. Treating it as content would let an unfilled template
    masquerade as a stated intent.
    """
    if not body:
        return ""
    m = _section_pattern(name).search(body)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^#{1,6}[ \t]*\S", rest, re.MULTILINE)
    text = rest[:nxt.start()] if nxt else rest
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = text.replace(_PLACEHOLDER, "")
    return text.strip()


def has_template(body):
    """True when the body carries at least the intent and solution headings."""
    return bool(body) and all(_section_pattern(n).search(body)
                              for n in (INTENT, SOLUTION))


def stated_intent(body):
    """The PR's own statement of intent.

    Falls back to the first non-heading paragraph so a PR that ignores the
    template still contributes whatever it did say, rather than presenting the
    reviewer with nothing.
    """
    intent = extract(body or "", INTENT)
    if intent:
        return intent
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.DOTALL)
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if para and not para.startswith("#"):
            return para
    return ""


def from_issue(number, title, body, url=None):
    """Build an intent section from the issue a fix PR was opened for.

    The issue IS the authoritative purpose statement -- it is where the human
    said what they wanted -- so a fix PR should quote it rather than paraphrase
    it or, as before, omit it entirely.
    """
    ref = "#%s" % number if number is not None else "the originating issue"
    if url:
        ref = "[%s](%s)" % (ref, url)
    lines = ["Fixes %s%s." % (ref, (": **%s**" % title.strip()) if title else "")]
    text = (body or "").strip()
    if text:
        if len(text) > 1500:
            text = text[:1500].rstrip() + "\n\n_(issue text truncated)_"
        lines.append("")
        lines.append("As reported:")
        lines.append("")
        lines.extend("> " + ln if ln.strip() else ">" for ln in text.splitlines())
    return "\n".join(lines)

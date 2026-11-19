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
# Regular expressions locate section headings and strip HTML comments from PR bodies.
import re

# Section titles exactly as they appear in .github/pull_request_template.md.
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

# The four sections in the order the template lists them.
SECTIONS = (INTENT, SOLUTION, GUARDRAILS, VERIFICATION)

# Text written into a section nobody filled in, so "empty" is explicit to the reviewer.
_PLACEHOLDER = "_Not stated._"

# What GitHub hides as an HTML comment, read by _visible(), in three branches.
_COMMENT = re.compile(r"""
    # 1. A fenced code block, which _visible() keeps whole: a comment marker
    #    inside one is code, and GitHub shows it as typed. As in CommonMark,
    #    it opens with 3 or more backticks or tildes, up to 3 spaces in (the
    #    rest of a backtick fence's line has no backtick), and closes at a
    #    line of the same character, at least as long and followed only by
    #    spaces or tabs, or at the end of the body.
    (?P<code>
        ^\ {0,3}(?P<ticks>`{3,})[^`\n]*$ .*? (?:^\ {0,3}(?P=ticks)`*[ \t]*$|\Z)
      | ^\ {0,3}(?P<tildes>~{3,})[^\n]*$ .*? (?:^\ {0,3}(?P=tildes)~*[ \t]*$|\Z) )
    # 2. A closed comment, hidden wherever it is.
  | <!--.*?-->
    # 3. An opener that starts a line (up to 3 spaces in) and is never
    #    closed. CommonMark makes it an HTML block that hides the rest of the
    #    body, headings included. The no-closer check is needed because
    #    re.sub tries this branch at the start of the line, before an
    #    indented opener, where branch 2 cannot match yet. An unclosed opener
    #    in mid-line, for example in backticks, is plain text and is kept.
  | ^\ {0,3}<!--(?:(?!-->).)*\Z
""", re.DOTALL | re.MULTILINE | re.VERBOSE)


def _visible(text):
    """``text`` as a reader sees it on GitHub, without its HTML comments."""
    # Keep each fenced code block as it is; drop each comment.
    return _COMMENT.sub(lambda m: m.group("code") or "", text or "")


def heading(name):
    """The exact heading line the template uses for ``name``."""
    # Look up the section's emoji; unknown section names simply get a plain heading.
    emoji = _EMOJI.get(name)
    # Build a level-2 markdown heading, with the emoji when there is one.
    return "## %s %s" % (emoji, name) if emoji else "## %s" % name


def render(intent=None, solution=None, guardrails=None, verification=None):
    """Render a PR body in template order.

    Empty sections are still emitted, with an explicit placeholder. A missing
    section is indistinguishable from a section nobody bothered to write, and
    the reviewer should be able to tell the difference between "no guardrail
    concerns" and "nobody considered guardrails".
    """
    # Collect one rendered "heading + body" block per section.
    parts = []
    # Walk the sections in template order, pairing each with the caller's text.
    for name, text in ((INTENT, intent), (SOLUTION, solution),
                       (GUARDRAILS, guardrails), (VERIFICATION, verification)):
        # Trim the text; an empty or missing section gets the explicit placeholder.
        body = (text or "").strip() or _PLACEHOLDER
        parts.append("%s\n\n%s" % (heading(name), body))
    # Separate sections with a blank line and end the body with a newline.
    return "\n\n".join(parts) + "\n"


def _section_pattern(name):
    # Tolerate the emoji being absent, extra whitespace, a trailing colon, and
    # any heading level -- a human editing the template by hand should not be
    # able to break extraction.
    """Return a compiled regex that matches the heading line for section ``name``."""
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
    # Nothing to search in an empty body.
    if not body:
        return ""
    # Read the body as GitHub shows it. Comments go first, so a heading
    # inside one cannot open or close a section.
    body = _visible(body)
    # Find the section's heading line.
    m = _section_pattern(name).search(body)
    # The section heading is not present at all.
    if not m:
        return ""
    # Take everything after the heading ...
    rest = body[m.end():]
    # ... up to the next markdown heading of any level (or the end of the body).
    nxt = re.search(r"^#{1,6}[ \t]*\S", rest, re.MULTILINE)
    text = rest[:nxt.start()] if nxt else rest
    # Drop our own placeholder so it does not count as content.
    text = text.replace(_PLACEHOLDER, "")
    # Return the cleaned section text.
    return text.strip()


def has_template(body):
    """True when the body carries at least the intent and solution headings."""
    # Both headings must be visible; one hidden in a comment does not count.
    text = _visible(body)
    return bool(text) and all(_section_pattern(n).search(text)
                              for n in (INTENT, SOLUTION))


def stated_intent(body):
    """The PR's own statement of intent.

    Falls back to the first non-heading paragraph so a PR that ignores the
    template still contributes whatever it did say, rather than presenting the
    reviewer with nothing.
    """
    # Prefer the explicit Intent section when the PR filled it in.
    intent = extract(body or "", INTENT)
    if intent:
        return intent
    # Otherwise strip HTML comments and look at the body paragraph by paragraph.
    text = _visible(body)
    # Paragraphs are separated by blank lines.
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        # The first paragraph that is not a heading is the best available intent statement.
        if para and not para.startswith("#"):
            return para
    # The body says nothing usable.
    return ""


def from_issue(number, title, body, url=None):
    """Build an intent section from the issue a fix PR was opened for.

    The issue IS the authoritative purpose statement -- it is where the human
    said what they wanted -- so a fix PR should quote it rather than paraphrase
    it or, as before, omit it entirely.
    """
    # Refer to the issue by number, or generically if no number is known.
    ref = "#%s" % number if number is not None else "the originating issue"
    # Turn the reference into a markdown link when a URL is available.
    if url:
        ref = "[%s](%s)" % (ref, url)
    # First line: "Fixes #N: **title**." (title omitted when absent).
    lines = ["Fixes %s%s." % (ref, (": **%s**" % title.strip()) if title else "")]
    # Quote the issue body itself, if it has one.
    text = (body or "").strip()
    if text:
        # Cap very long issue bodies so the PR description stays readable.
        if len(text) > 1500:
            text = text[:1500].rstrip() + "\n\n_(issue text truncated)_"
        # Add a blank line and an "As reported:" lead-in before the quote.
        lines.append("")
        lines.append("As reported:")
        lines.append("")
        # Prefix every line with "> " (bare ">" for blank lines) to form a markdown blockquote.
        lines.extend("> " + ln if ln.strip() else ">" for ln in text.splitlines())
    # Join everything into the final intent section text.
    return "\n".join(lines)

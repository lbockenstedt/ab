"""Which branches AppBuilder is allowed to force-push or delete, and the
canonical names it gives its own automation-driven branches.

AppBuilder destroyed two long-lived branches in practice, from two different
code paths that shared the same missing check:

  * ``pr_actions._delete_pr_branch`` deletes a merged PR's head branch. Its only
    guard was ``ref == repo.default_branch``, so ``main`` was safe but ``dev``
    and ``qa`` were not. A low-confidence fix opens its PR with ``dev`` as the
    HEAD branch (see ``fix_engine``), so merging that PR deleted ``dev``.
  * ``fix_engine`` force-pushes the branch it just built. When that branch is
    ``dev``, a force-push silently discards whatever else had landed on ``dev``
    in the meantime.

The intent was only ever to clean up the throwaway branches AppBuilder itself
creates (``bug/*``, ``ai-feature/*``) so they don't pile up after every merge
-- never to touch a branch a human works on.

The policy here is therefore an ALLOWLIST, not a blocklist: a branch is
deletable only if it positively looks like one AppBuilder created AND is not
protected. Anything unrecognised is kept. Getting this wrong in the permissive
direction destroys work; getting it wrong in the strict direction leaves a
stale branch someone can delete in one click.

NAMING (added 2026-08-29): bug-fix branches are named ``bug/<desc>`` or
``bug/<issue-number>-<desc>`` when a GitHub issue exists; features use
``ai-feature/`` the same way. NOT plain ``feature/`` -- that is the existing,
heavily-used HUMAN branch-naming convention in this repo (37+ branches at the
time of this change), so reusing it for AppBuilder's own branches would make
automation-driven branches indistinguishable from human ones by prefix alone,
directly defeating the point of a distinct prefix, and would make every
existing human ``feature/*`` branch match ``is_auto_created()`` below --
exactly the class of bug this module exists to prevent. AUTO_BRANCH_PREFIXES_BY_KIND
is the single source of truth for both the prefixes recognised here and the
names ``auto_branch_name()`` builds -- previously these were two hand-synced
string literals in different files, held together only by a comment saying
"keep in step".
"""

import re

# Names protected regardless of configuration. These are the long-lived
# branches of the dev -> qa -> main promotion flow, plus the conventional names
# other repos use for the same roles.
DEFAULT_PROTECTED = ("main", "master", "dev", "qa", "staging", "release", "next")

# The one place the "bug"/"feature" -> prefix mapping is defined. Both
# DEFAULT_AUTO_PREFIXES (recognition) and auto_branch_name() (construction)
# derive from this dict, so they cannot drift apart the way the old two
# hardcoded literals (in fix_engine.py and feature_build.py) eventually would.
AUTO_BRANCH_PREFIXES_BY_KIND = {
    "bug": "bug/",
    "feature": "ai-feature/",
}

# Prefixes of branches AppBuilder creates itself, and may therefore clean up.
DEFAULT_AUTO_PREFIXES = tuple(AUTO_BRANCH_PREFIXES_BY_KIND.values())


def slugify_branch_desc(text, max_len=50):
    """Lowercase, hyphen-separated, git-ref-safe rendering of free text for
    use as a branch-name description segment. Deliberately conservative:
    collapses every run of non-alphanumeric characters to a single hyphen and
    strips leading/trailing hyphens, so it can't produce a ref git rejects
    (no spaces, no ``~^:?*[\\``, no leading/trailing/double dots) regardless
    of what's in the source title. Falls back to "untitled" rather than
    returning an empty segment, which would otherwise collapse the prefix's
    trailing slash with the issue number into something like "bug/123-" or
    (with no issue number at all) an invalid "bug/" with nothing after it."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len].rstrip("-") or "untitled"


def auto_branch_name(kind, issue=None, description=None):
    """The branch name AppBuilder gives its own automated work.

    kind: "bug" or "feature" -- looked up in AUTO_BRANCH_PREFIXES_BY_KIND, so
          an unrecognised kind raises KeyError immediately rather than
          silently producing an unprefixed (and therefore unrecognisable-as-
          AppBuilder's-own) branch name.
    issue: a GitHub issue-like object (needs .number and .title) if this work
           is driven by a real issue. When given, its number is embedded in
           the name (e.g. "bug/123-null-pointer-in-parser") so the branch is
           traceable back to the issue at a glance.
    description: fallback text to slug when issue is None (or has no number)
                 -- e.g. a chat-triggered fix with no filed issue yet. Falls
                 back to issue.title if issue is given but description isn't.

    Returns e.g. "bug/123-null-pointer-in-parser" with an issue, or
    "bug/null-pointer-in-parser" without one.
    """
    prefix = AUTO_BRANCH_PREFIXES_BY_KIND[kind]
    number = getattr(issue, "number", None) if issue is not None else None
    title = description if description is not None else getattr(issue, "title", None)
    slug = slugify_branch_desc(title)
    return f"{prefix}{number}-{slug}" if number else f"{prefix}{slug}"


def parse_names(value):
    """Accept a list, or a comma/space/newline-separated string, from config."""
    if not value:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.replace(",", " ").split() if p.strip()]
    return [str(p).strip() for p in value if str(p).strip()]


def protected_branches(config=None, repo_default_branch=None):
    """Every branch name AppBuilder must never force-push or delete.

    Combines the built-in names, the repo's own default branch, the configured
    ``default_branch``/``dev_branch`` (so renaming those in config protects
    them automatically), and any extra ``protected_branches`` from config.
    """
    cfg = config or {}
    names = set(DEFAULT_PROTECTED)
    for extra in (repo_default_branch, cfg.get("default_branch"), cfg.get("dev_branch")):
        if extra:
            names.add(str(extra).strip())
    names.update(parse_names(cfg.get("protected_branches")))
    return {n.lower() for n in names if n}


def auto_branch_prefixes(config=None):
    """Prefixes that mark a branch as AppBuilder-created, hence disposable."""
    cfg = config or {}
    configured = parse_names(cfg.get("auto_branch_prefixes"))
    return tuple(configured) if configured else DEFAULT_AUTO_PREFIXES


def is_protected(ref, config=None, repo_default_branch=None):
    return str(ref or "").strip().lower() in protected_branches(config, repo_default_branch)


def is_auto_created(ref, config=None):
    name = str(ref or "").strip()
    return bool(name) and name.startswith(auto_branch_prefixes(config))


def may_delete(ref, config=None, repo_default_branch=None):
    """(ok, reason) -- may AppBuilder delete this merged head branch?

    Reason is always populated so refusals can be logged and understood; a
    branch left behind should never be a silent mystery.
    """
    cfg = config or {}
    name = str(ref or "").strip()
    if not name:
        return False, "no branch name"
    if not cfg.get("delete_merged_branches", True):
        return False, "branch cleanup is disabled (delete_merged_branches=false)"
    if is_protected(name, cfg, repo_default_branch):
        return False, f"'{name}' is a protected branch"
    if not is_auto_created(name, cfg):
        return False, (f"'{name}' was not created by AppBuilder "
                       f"(expected one of: {', '.join(auto_branch_prefixes(cfg))})")
    return True, f"'{name}' is a merged AppBuilder branch"


def may_force_push(ref, config=None, repo_default_branch=None):
    """(ok, reason) -- may AppBuilder force-push this branch?

    Force-pushing a shared branch discards anything that landed on it since the
    working copy was made, so it is confined to AppBuilder's own branches.
    """
    name = str(ref or "").strip()
    if not name:
        return False, "no branch name"
    if is_protected(name, config, repo_default_branch):
        return False, (f"'{name}' is a protected branch -- force-pushing it would "
                       f"discard commits made since this working copy was created")
    return True, f"'{name}' is not protected"


def integration_branch(config=None, repo_obj=None):
    """The branch AppBuilder integrates its work into -- always ``dev``.

    Changes reach production one way: dev -> qa -> main, one deliberate
    promotion at a time. AppBuilder previously took ``default_branch`` as its
    base, which is ``main``, so every automated fix either opened a PR straight
    into main or (on a trusted repo with an approving review) pushed to main
    directly, bypassing qa and the repo owner entirely.

    ``dev`` is therefore not merely the default here, it is the rule: this
    never returns the default/production branch while a dev branch exists.
    The only fallback is a repo that genuinely has no dev branch, where the
    alternative would be to fail outright; that case is reported so it can be
    fixed by creating the branch.
    """
    cfg = config or {}
    dev = (cfg.get("dev_branch") or "dev").strip() or "dev"

    if repo_obj is not None:
        try:
            repo_obj.get_branch(dev)
        except Exception:
            fallback = (cfg.get("default_branch")
                        or getattr(repo_obj, "default_branch", None) or "main")
            return fallback, (f"'{dev}' does not exist in "
                              f"{getattr(repo_obj, 'full_name', 'this repo')} -- falling back to "
                              f"'{fallback}'; create '{dev}' to restore the dev -> qa -> main flow")
    return dev, f"'{dev}' is the integration branch (dev -> qa -> main)"


def may_direct_push(ref, config=None, repo_default_branch=None):
    """(ok, reason) -- may AppBuilder push commits straight at this branch,
    with no pull request?

    Belt-and-braces backstop for ``fix_engine``'s trusted-repo direct-push
    path. That path is already aimed at ``integration_branch()`` (``dev``),
    but AppBuilder authenticates as the repo OWNER's PAT, which bypasses every
    GitHub ruleset -- so if the target it computes ever drifts back to the
    production branch, GitHub will NOT stop the push. Nothing else will either.
    This function is that stop, in code.

    Deliberately NOT ``is_protected`` wholesale. ``is_protected`` covers the
    whole promotion flow (main/master/dev/qa/staging/release/next plus the
    configured default/dev names) because force-pushing or DELETING any of
    those destroys work. Direct-pushing is a different question: ``dev`` is
    precisely where AppBuilder's trusted-repo flow is *supposed* to commit, so
    refusing it wholesale would break the normal path this guard is meant to
    leave alone.

    The rule is therefore "protected, EXCEPT the integration branch":

      * ``dev`` (or whatever ``dev_branch`` is configured to) -- ALLOWED. This
        is the intended destination of a trusted, approved automated fix.
      * ``main``/``master``, the repo's own default branch, and a configured
        ``default_branch`` -- REFUSED. Production is reached by promotion PR
        (dev -> qa -> main), never by a push from automation.
      * ``qa``/``staging``/``release``/``next`` -- REFUSED. These are promotion
        gates; a push straight at one strands the gate ahead of its source and
        skips the review the gate exists to provide.
      * anything else (``bug/*``, ``ai-feature/*``, a topic branch) -- ALLOWED;
        pushing to AppBuilder's own working branch is the ordinary case.

    A refusal is never fatal: callers log the reason and fall back to opening a
    pull request, which is the outcome the promotion flow wants anyway.
    """
    cfg = config or {}
    name = str(ref or "").strip()
    if not name:
        return False, "no branch name"

    # Checked BEFORE is_protected: the integration branch is itself protected
    # (against force-push/delete), but is the one branch a normal direct push
    # is allowed to target.
    dev, _dev_why = integration_branch(cfg)
    if name.lower() == str(dev or "").strip().lower():
        return True, f"'{name}' is the integration branch -- the intended target for automated commits"

    if is_protected(name, cfg, repo_default_branch):
        return False, (f"'{name}' is a protected branch -- automated commits reach it by "
                       f"promotion PR (dev -> qa -> main), never by a direct push; "
                       f"opening a pull request instead")

    return True, f"'{name}' is not a protected branch"

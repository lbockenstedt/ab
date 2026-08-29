"""Which branches AppBuilder is allowed to force-push or delete.

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
creates (``ai-fix-issue-*``, ``ai-feature-issue-*``) so they don't pile up after
every merge -- never to touch a branch a human works on.

The policy here is therefore an ALLOWLIST, not a blocklist: a branch is
deletable only if it positively looks like one AppBuilder created AND is not
protected. Anything unrecognised is kept. Getting this wrong in the permissive
direction destroys work; getting it wrong in the strict direction leaves a
stale branch someone can delete in one click.
"""

# Names protected regardless of configuration. These are the long-lived
# branches of the dev -> qa -> main promotion flow, plus the conventional names
# other repos use for the same roles.
DEFAULT_PROTECTED = ("main", "master", "dev", "qa", "staging", "release", "next")

# Prefixes of branches AppBuilder creates itself, and may therefore clean up.
# Keep in step with fix_engine (``ai-fix-issue-{n}``) and feature_build
# (``ai-feature-issue-{n}``).
DEFAULT_AUTO_PREFIXES = ("ai-fix-issue-", "ai-feature-issue-")


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

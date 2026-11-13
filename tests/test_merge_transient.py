"""merge_pr must not record a refusal for a merge that only failed "for now".

Six promotion PRs (ab#500, ab#547, netbox#272, opnsense#115, #116, #118)
showed "Merge refused" although they merged seconds later:

- ab#500/#547: ``Required status check "test" is in progress.`` -- the check
  passed under a minute later.
- opnsense#115: two AppBuilder threads merged the same PR; one got
  ``Merge already in progress`` while the other succeeded.

merge_pr is extracted by source with ast (importing pr_actions boots the live
app) and run against stubs.
"""
import ast


class GithubException(Exception):
    def __init__(self, status, data):
        super().__init__(status, data)
        self.status, self.data = status, data


class _PR:
    def __init__(self, error, merged_after=False):
        self.merged, self.state = False, "open"
        self._error, self._merged_after = error, merged_after

    def merge(self):
        raise self._error

    def update(self):
        self.merged = self._merged_after


class _Repo:
    def __init__(self, pr):
        self.pr = pr

    def get_pull(self, _number):
        return self.pr


class _GH:
    def __init__(self, pr):
        self.repo = _Repo(pr)

    def get_repo(self, _name):
        return self.repo


class _Logger:
    def info(self, *a): pass
    def warning(self, *a): pass


def _merge_pr():
    """merge_pr and its helpers, bound to stubs; returns (fn, review updates)."""
    src = open("pr_actions.py").read()
    tree = ast.parse(src)
    names = {"merge_pr", "_merge_refusal", "_is_merge_conflict", "_expects_missing_check",
             "_github_message", "_merge_is_transient"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and any(
                getattr(t, "id", "") == "_TRANSIENT_MERGE_REFUSALS" for t in n.targets))]
    updates = []
    ns = {"GithubException": GithubException, "logger": _Logger(),
          "state": {"pr_reviews": {"o/r#5": {"approved": True}}},
          "update_pr_review": lambda *a, **k: updates.append(k),
          "_release_parked_checks": lambda *a: 0, "_github_token": lambda: ""}
    exec(compile(ast.Module(body=body, type_ignores=[]), "pr_actions.py", "exec"), ns)
    return ns["merge_pr"], updates


def test_a_required_check_still_running_is_retried_not_refused():
    merge_pr, updates = _merge_pr()
    err = GithubException(405, {"message": "Repository rule violations found\n\n"
                                           "Required status check \"test\" is in progress.\n"})
    status, res = merge_pr(_GH(_PR(err)), "o/r", 5)
    assert res["retryable"] is True and res["deferred"] is True
    assert not any("merge_blocked_reason" in u and u["merge_blocked_reason"] for u in updates)


def test_a_merge_already_in_progress_is_retried_not_refused():
    merge_pr, updates = _merge_pr()
    err = GithubException(405, {"message": "Merge already in progress"})
    _status, res = merge_pr(_GH(_PR(err)), "o/r", 5)
    assert res["retryable"] is True and res["deferred"] is True


def test_a_pr_merged_by_a_concurrent_attempt_is_a_success():
    merge_pr, updates = _merge_pr()
    err = GithubException(405, {"message": "Merge already in progress"})
    status, res = merge_pr(_GH(_PR(err, merged_after=True)), "o/r", 5)
    assert status == 200 and res["status"] == "success"
    assert {"merged": True, "merge_blocked_reason": None} in updates


def test_a_real_rule_violation_is_still_a_refusal():
    merge_pr, updates = _merge_pr()
    err = GithubException(405, {"message": "Repository rule violations found\n\n"
                                           "Changes must be made through a pull request."})
    _status, res = merge_pr(_GH(_PR(err)), "o/r", 5)
    assert res["retryable"] is False
    assert updates and updates[-1]["merge_blocked_reason"]


def test_a_running_check_next_to_a_real_blocker_is_still_a_refusal():
    """GitHub lists every violation; one transient line must not hide the rest."""
    merge_pr, updates = _merge_pr()
    for other in ("Changes must be made through a pull request.",
                  "Required status check \"check-direction\" is failing."):
        err = GithubException(405, {"message": "Repository rule violations found\n\n"
                                               "Required status check \"test\" is in progress.\n"
                                               + other + "\n"})
        _status, res = merge_pr(_GH(_PR(err)), "o/r", 5)
        assert not res.get("deferred")

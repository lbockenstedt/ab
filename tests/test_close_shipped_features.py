"""Fleet PRs merge into dev, so GitHub never auto-closed a built feature's
issue once it reached main (cs#257, lm#1898). AB now closes it itself."""
import copy
from types import SimpleNamespace as N

import main  # noqa: F401 - loads workers in import order
import workers


class _Issue:
    def __init__(self):
        self.state, self.comments, self.labels = "open", [], []

    def create_comment(self, body):
        self.comments.append(body)

    def add_to_labels(self, name):
        self.labels.append(name)

    def edit(self, state):
        self.state = state


class _Repo:
    def __init__(self, compare_status):
        self.default_branch = "main"
        self.compare_status = compare_status
        self.issues = {7: _Issue()}
        self.pr = N(merged=True, merge_commit_sha="abc", base=N(ref="dev"),
                    html_url="https://github.com/o/cs/pull/9")

    def get_pull(self, number):
        return self.pr

    def get_issue(self, number):
        return self.issues[number]

    def compare(self, base, head):
        assert (base, head) == ("main", "abc")
        return N(status=self.compare_status)


def _run(monkeypatch, compare_status):
    store = {"data": {"o/cs:7": {"status": "feature_built", "pr_url": "https://github.com/o/cs/pull/9"}}}
    monkeypatch.setattr(workers, "load_processed", lambda: copy.deepcopy(store["data"]))
    monkeypatch.setattr(workers, "save_processed", lambda p: store.update(data=copy.deepcopy(p)))
    monkeypatch.setattr(workers, "recompute_issue_counters", lambda p=None: None)
    monkeypatch.setattr(main, "_apply_closed_label", lambda r, i, k: i.add_to_labels("ab-closed"))
    repo = _Repo(compare_status)
    gh = N(get_repo=lambda name: repo)
    return workers.close_shipped_features(gh, {}), store["data"]["o/cs:7"], repo.issues[7]


def test_feature_on_main_is_closed_with_a_pr_link(monkeypatch):
    closed, rec, issue = _run(monkeypatch, "behind")
    assert closed == ["o/cs:7"]
    assert issue.state == "closed" and "ab-closed" in issue.labels
    assert "https://github.com/o/cs/pull/9" in issue.comments[0]
    assert rec["status"] == "resolved" and rec["resolution"] == "feature_shipped"


def test_feature_only_on_dev_stays_open(monkeypatch):
    closed, rec, issue = _run(monkeypatch, "ahead")
    assert closed == [] and issue.state == "open" and not issue.comments
    assert rec["status"] == "feature_built" and rec["shipped_checked_at"]

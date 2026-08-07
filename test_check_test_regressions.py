#!/usr/bin/env python3
"""Self-test for bugfixer/check_test_regressions.py — the pure-logic pieces
only (failure-line parsing, test-command detection, default-off gating).
Does NOT exercise the actual clone/Docker-sandbox path — that needs a real
git remote + Docker, which is exactly the kind of thing this file avoids per
the other test_*.py self-tests in this repo (stdlib-only, no network).

Run:  python3 bugfixer/test_check_test_regressions.py
"""
import sys
import tempfile
import os

from check_test_regressions import (
    _FAILED_LINE_RE, _detect_test_cmd, check_test_regressions,
)


def _check(label, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    return bool(condition)


_PYTEST_Q_OUTPUT = """............F.....F.........                                             [100%]
=================================== FAILURES ===================================
_________________ test_engine_poll_logs_summary_and_sub_errors _________________
    assert res["status"] == "SUCCESS"
E   AssertionError: assert 'PARTIAL' == 'SUCCESS'
=========================== short test summary info ============================
FAILED tests/test_nw_logging.py::test_engine_poll_logs_summary_and_sub_errors - AssertionError
FAILED tests/test_nw_poll.py::test_poll_partial_failure_tolerated - AssertionError: assert 'PARTIAL' == 'SUCCESS'
2 failed, 27 passed in 0.06s
"""


class _FakeConfig(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


def main():
    print("Running bugfixer check_test_regressions self-test...")
    ok = True

    # (1) FAILED-line extraction matches pytest's standard -q short summary.
    names = set(_FAILED_LINE_RE.findall(_PYTEST_Q_OUTPUT))
    ok &= _check(
        "extracts both FAILED test ids from pytest -q output",
        names == {"tests/test_nw_logging.py::test_engine_poll_logs_summary_and_sub_errors",
                 "tests/test_nw_poll.py::test_poll_partial_failure_tolerated"},
    )

    # (2) test-command detection by repo marker file.
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "pyproject.toml"), "w").close()
        ok &= _check("detects pytest for a pyproject.toml repo",
                    _detect_test_cmd(d) == "python3 -m pytest -q")
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "package.json"), "w").close()
        ok &= _check("detects npm test for a package.json repo",
                    _detect_test_cmd(d) == "npm test")
    with tempfile.TemporaryDirectory() as d:
        ok &= _check("no marker file -> None (skip, not a guess)",
                    _detect_test_cmd(d) is None)

    # (3) default-OFF gating: check_test_regressions must return [] immediately
    # (no clone attempt, no exception) when the opt-in flag is unset — this is
    # the actual safety property ("doesn't run unless explicitly enabled"),
    # not just a docstring claim.
    class _Pr:
        number = 1
        head = type("H", (), {"sha": "deadbeef"})()
        base = type("B", (), {"sha": "cafef00d"})()
    class _Repo:
        full_name = "example/repo"
        clone_url = "https://example.invalid/repo.git"
    ok &= _check(
        "returns [] with no side effects when pr_test_regression_enabled is unset",
        check_test_regressions(_Repo(), _Pr(), _FakeConfig(), "tok") == [],
    )
    ok &= _check(
        "returns [] when qa_enabled is explicitly off, even if the opt-in flag is on",
        check_test_regressions(_Repo(), _Pr(),
                               _FakeConfig(qa_enabled=False, pr_test_regression_enabled=True),
                               "tok") == [],
    )

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())

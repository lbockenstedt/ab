"""Exercise repository opt-in without importing AppBuilder workers."""

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from jinja2 import Environment
from test_feature_settings_roundtrip import _base_pairs, _FakeRequest, _load_ns, _run


def test_save_settings_keeps_selected_monitored_repositories():
    """Save active selections using monitored spelling, ignoring other repos."""
    ns = _load_ns()
    ns["_config_holder"]["config"] = {"pr_review_lm_summary_repos": ["OWNER/REPO"]}
    pairs = _base_pairs(monitored_repos="Owner/Repo") + [
        ("pr_review_lm_summary_repos", "OWNER/REPO"),
        ("pr_review_lm_summary_repos", "owner/unmonitored"),
    ]
    _run(ns["save_settings"](_FakeRequest(pairs)))
    assert ns["_config_holder"]["config"]["pr_review_lm_summary_repos"] == ["Owner/Repo"]


def test_settings_picker_retains_opt_in_with_different_repo_case():
    """An existing opt-in must appear checked so saving Settings preserves it."""
    template = Path(__file__).resolve().parents[1] / "templates" / "index.html"
    text = template.read_text(encoding="utf-8")
    marker = text.index('name="pr_review_lm_summary_repos"')
    fieldset = text[text.rindex("<fieldset", 0, marker):text.index("</fieldset>", marker)]
    html = Environment(autoescape=True).from_string(fieldset).render(
        monitored_set={"OWNER/Repo", "owner/other"}, lm_summary_repos_set={"owner/repo"},
    )
    assert 'value="OWNER/Repo" checked' in html
    assert 'value="owner/other" checked' not in html


@pytest.mark.parametrize(("opt_in", "reply", "expected"), [
    (True, {"status": "SUCCESS", "text": " local summary "}, "local summary"),
    (True, {"status": "ERROR", "code": "no_capacity"}, ""),
    (False, None, "existing summary"),
])
def test_repository_opt_in_does_not_fall_through(monkeypatch, opt_in, reply, expected):
    """A selected repo delegates placement; a failed request cannot use AB fallback."""
    file = Path(__file__).resolve().parents[1] / "pr_review.py"
    node = next(item for item in ast.parse(file.read_text()).body
                if isinstance(item, ast.FunctionDef) and item.name == "_summarize_changes")
    client = SimpleNamespace(request_sync=Mock(return_value=reply))
    legacy = Mock(return_value="existing summary")
    monkeypatch.setitem(sys.modules, "llm_client", SimpleNamespace(call_llm=legacy))
    monkeypatch.setitem(sys.modules, "model_selection",
                        SimpleNamespace(LlmRequirements=lambda **kwargs: kwargs))
    monkeypatch.setitem(sys.modules, "workers",
                        SimpleNamespace(_get_hub_agent_client=lambda: client))
    scope = {"_SUMMARY_MAX_FILES": 10, "_SUMMARY_PATCH_CHARS": 100,
             "_SUMMARY_PROMPT_CHARS": 1000, "logger": Mock()}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(file), "exec"), scope)
    repo = SimpleNamespace(full_name="owner/repo", visibility="private", private=True)
    pr = SimpleNamespace(title="Synthetic change", number=1, head=SimpleNamespace(sha="head"))
    files = [SimpleNamespace(filename="example.py", patch="+ value = 1")]
    config = {"pr_review_lm_summary_repos": [repo.full_name.upper()]} if opt_in else {}
    assert scope["_summarize_changes"](pr, files, config, repo=repo) == expected
    if opt_in:
        legacy.assert_not_called()
        assert client.request_sync.call_args.args[1]["public"] is False
    else:
        client.request_sync.assert_not_called()

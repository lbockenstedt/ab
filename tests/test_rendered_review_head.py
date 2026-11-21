"""The actual dashboard actions carry the exact review head shown at render."""
import ast
import asyncio
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest
from markupsafe import escape

import mission_control


def test_all_approval_buttons_render_the_review_record_head(monkeypatch):
    """Render Mission Control and the actual history table with dashboard data."""
    records = {
        "o/r#1": {"head": "normal-head", "reviewed_at": "2026-01-01 00:00:00"},
        "o/r#2": {"head": "held-head", "reviewed_at": "2026-01-01 00:00:00",
                  "auto_merge_blocked_reason": "CI failure"},
        "o/r#3": {"head": "split-head", "reviewed_at": "2026-01-01 00:00:00",
                  "panel_verdict": "Approve", "panel2_verdict": "Reject"},
        "o/r#4": {"reviewed_at": "2026-01-01 00:00:00"},
        "o/r#5": {"head": 'exhausted-"<&', "reviewed_at": "2026-01-01 00:00:00",
                  "auto_remediate_status": "exhausted_human_review"},
        "o/r#6": {"head": "merged-head", "merged": True},
        "o/r#7": {"head": "denied-head", "denied": True},
        "o/r#8": {"head": "approved-head", "approved": True},
        "o/r#9": {"head": "closed-head", "closed": True},
        "o/r#10": {"head": "auto-merged-head", "auto_merged": True},
    }
    for key, record in records.items():
        record.update(repo="o/r", number=int(key.split("#")[1]), items=[])
    mc = mission_control.build({}, records, now=datetime(2026, 1, 2))
    # Execute the real dashboard projection without importing/starting the app.
    tree = ast.parse(Path("routes.py").read_text(encoding="utf-8"))
    dashboard = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                     and n.name == "dashboard")
    dashboard.decorator_list = []
    ns = {"Request": object, "state": {"processed": {}, "pr_reviews": records},
          "activity_feed_items": lambda items: items, "load_config": lambda: {},
          "_mission": lambda config: mc,
          "templates": SimpleNamespace(TemplateResponse=lambda **kwargs: kwargs["context"])}
    monkeypatch.setitem(sys.modules, "llm_client", SimpleNamespace(_find_claude_cli_slot=lambda config: None))
    exec(compile(ast.Module(body=[dashboard], type_ignores=[]), "routes.py", "exec"), ns)
    context = asyncio.run(ns["dashboard"](SimpleNamespace(url=SimpleNamespace(path="/history"))))
    assert dict(context["sorted_pr_reviews"])["o/r#5"]["head"] == 'exhausted-"<&'

    html = Path("templates/index.html").read_text(encoding="utf-8")
    start = html.index("{% for it in mc.attention %}")
    end = html.index("</section>", start)
    env = jinja2.Environment(autoescape=True)
    attention = env.from_string(html[start:end]).render(mc=mc)
    # Include the production macro definitions AND the whole real PR table loop.
    macro_start = html.index("{% macro act_button(")
    macro_end = html.index("{% macro filter_chip(", macro_start)
    table_start = html.index("{% for _k, pr in sorted_pr_reviews %}")
    table_end = html.index("{% if not state.processed", table_start)
    table = env.from_string(html[macro_start:macro_end] + html[table_start:table_end]).render(
        **context, pr_chip=mission_control.pr_chip, panel_chips=mission_control.panel_chips,
        age_of=mission_control.age_of)
    for surface, rendered in [("attention", attention), ("table", table)]:
        buttons = re.findall(r'<button[^>]*data-mc="(?:approve|approve-merge)"[^>]*>', rendered)
        assert len(buttons) == 5, surface
        for number in range(1, 6):
            button = next(b for b in buttons if f'data-num="{number}"' in b)
            action = "approve-merge" if surface == "attention" and number == 3 else "approve"
            head = records[f"o/r#{number}"].get("head", "")
            assert f'data-mc="{action}"' in button, surface
            assert f'data-head="{escape(head)}"' in button, surface
        # Merged/denied/closed PRs expose no approval; approved rows only offer Merge.
        assert not any(f'data-num="{n}"' in b for n in range(6, 11) for b in buttons)


@pytest.mark.parametrize("action", ["approve", "approve-merge", "merge", "deny"])
@pytest.mark.parametrize("head", ['rendered-"<&', ""])
def test_action_macro_scopes_and_escapes_review_head(action, head):
    """Both approval actions carry even a missing head; unrelated actions do not."""
    html = Path("templates/index.html").read_text(encoding="utf-8")
    start = html.index("{% macro act_button(")
    end = html.index("{% macro act_menu(", start)
    env = jinja2.Environment(autoescape=True)
    macro = env.from_string(html[start:end]).module.act_button
    rendered = macro(action, "Action", "Title", repo="o/r", num=1, head=head)
    if action in ("approve", "approve-merge"):
        assert f'data-head="{escape(head)}"' in rendered
        assert 'data-head=""' in macro(action, "Action", "Title", repo="o/r", num=1)
    else:
        assert "data-head=" not in rendered


def test_every_macro_approval_call_supplies_rendered_review_head():
    """Prevent another approval macro call from silently defaulting to no SHA."""
    html = Path("templates/index.html").read_text(encoding="utf-8")
    tree = jinja2.Environment().parse(html)
    calls = [node for node in tree.find_all(jinja2.nodes.Call)
             if isinstance(node.node, jinja2.nodes.Name) and node.node.name == "act_button"
             and node.args and isinstance(node.args[0], jinja2.nodes.Const)
             and node.args[0].value in ("approve", "approve-merge")]
    assert calls
    for call in calls:
        head = next((kw.value for kw in call.kwargs if kw.key == "head"), None)
        assert isinstance(head, jinja2.nodes.Getattr) and head.attr == "head", call.lineno


@pytest.mark.skipif(not shutil.which("node"), reason="Node is needed to execute the dashboard handlers")
@pytest.mark.parametrize("name,end", [("approvePr", "async function reprocessPr"),
                                      ("approveAndMergePr", "function filterActivity")])
def test_both_real_approval_handlers_send_button_head(name, end):
    """Execute the actual JavaScript against a non-network fetch fake."""
    html = Path("templates/index.html").read_text(encoding="utf-8")
    start = html.index("async function " + name + "(")
    source = html[start:html.index(end, start)]
    script = """
        const calls = [];
        async function fetch(url, opts) {
            calls.push([url, JSON.parse(opts.body)]);
            return {ok: false, status: 409, json: async () => ({status: 'error'})};
        }
        function showToast() {}
        function mergePr() { throw new Error('must not merge after refusal'); }
    """ + source + """
        (async () => {
            const button = {dataset: {head: 'displayed-X'}, innerHTML: '', textContent: ''};
            await HANDLER('o/r', 1, button);
            console.log(JSON.stringify(calls));
        })().catch(e => { console.error(e); process.exit(1); });
    """.replace("HANDLER", name)
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == [["/api/pr-review/approve",
                                          {"repo": "o/r", "number": 1, "head": "displayed-X"}]]

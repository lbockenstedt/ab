"""The actual dashboard actions carry the exact review head shown at render."""
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import jinja2
import pytest

import mission_control


def test_all_approval_buttons_render_the_review_record_head():
    """Render all three approval variants from real Mission Control data."""
    records = {
        "o/r#1": {"head": "normal-head", "reviewed_at": "2026-01-01 00:00:00"},
        "o/r#2": {"head": "held-head", "reviewed_at": "2026-01-01 00:00:00",
                  "auto_merge_blocked_reason": "CI failure"},
        "o/r#3": {"head": "split-head", "reviewed_at": "2026-01-01 00:00:00",
                  "panel_verdict": "Approve", "panel2_verdict": "Reject"},
        "o/r#4": {"reviewed_at": "2026-01-01 00:00:00"},
    }
    mc = mission_control.build({}, records, now=datetime(2026, 1, 2))
    html = Path("templates/index.html").read_text(encoding="utf-8")
    start = html.index("{% for it in mc.attention %}")
    end = html.index("</section>", start)
    rendered = jinja2.Environment(autoescape=True).from_string(html[start:end]).render(mc=mc)
    buttons = re.findall(r'<button[^>]*data-mc="(?:approve|approve-merge)"[^>]*>', rendered)
    assert len(buttons) == 4
    for number, action, head in [(1, "approve", "normal-head"), (2, "approve", "held-head"),
                                 (3, "approve-merge", "split-head"), (4, "approve", "")]:
        button = next(b for b in buttons if f'data-num="{number}"' in b)
        assert f'data-mc="{action}"' in button and f'data-head="{head}"' in button


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

#!/usr/bin/env python3
"""Tests for sim_session (interactive Simulation Build Assistant sessions).

Standalone: the manager takes injected config/skills/GitHub/processed loaders,
and a fake `copilot` script stands in for the CLI, so nothing here imports
main or touches the network.

Run:  python3 -m pytest -q test_sim_session.py
"""
import json
import os
import stat
import sys
import tempfile
import time

import pytest

import sim_session as ss


SPEC = {"intent": "create", "target": "", "name": "dns_fail_lrb", "title": "DNS fail for LRB", "summary": "Variant of dns_fail.",
        "behavior": "Query nonexistent names every 30s.", "platforms": ["Linux", "windows"],
        "alert": "DNS failure", "config_knobs": [{"key": "rate", "default": "5", "description": "qps"}],
        "touchpoints": [{"repo": "cs", "path": "clients/linux/dns_fail_lrb.sh", "change": "new"},
                        {"repo": "lm", "path": "WebUI/sim-views.js", "change": "add to list"}],
        "open_questions": [], "ready": True, "choices": ["Yes", "No"]}


def _reply_with_spec(text, spec):
    return f"{text}\n\n```sim-spec\n{json.dumps(spec)}\n```\n"


# ── pure helpers ────────────────────────────────────────────────────────────

def test_extract_spec_strips_block_and_parses_last():
    old = dict(SPEC, ready=False, name="old")
    text = _reply_with_spec("first", old) + _reply_with_spec("Which rate?", SPEC)
    display, spec, choices = ss.extract_spec(text)
    assert "sim-spec" not in display and "Which rate?" in display
    assert spec["name"] == "dns_fail_lrb"
    assert spec["platforms"] == ["linux", "windows"]
    assert choices == ["Yes", "No"]
    assert "choices" not in spec


def test_extract_spec_bad_json_keeps_none():
    display, spec, choices = ss.extract_spec("hi\n```sim-spec\n{not json\n```")
    assert display == "hi" and spec is None and choices == []


def test_extract_spec_no_block():
    assert ss.extract_spec("plain") == ("plain", None, [])


def test_spec_is_ready_requires_completeness():
    assert ss.spec_is_ready(ss.normalize_spec(SPEC))
    assert not ss.spec_is_ready(ss.normalize_spec(dict(SPEC, ready=False)))
    assert not ss.spec_is_ready(ss.normalize_spec(dict(SPEC, open_questions=["rate?"])))
    assert not ss.spec_is_ready(ss.normalize_spec(dict(SPEC, touchpoints=[])))
    assert not ss.spec_is_ready(None)
    assert not ss.spec_is_ready(ss.normalize_spec(dict(SPEC, intent="")))
    assert not ss.spec_is_ready(ss.normalize_spec(dict(SPEC, intent="explain")))
    assert not ss.spec_is_ready(ss.normalize_spec(SPEC), ["out of scope"])


# ── guardrails: create / modify / explain simulations only ─────────────────

def test_normalize_spec_intent_and_repo_prefixed_paths():
    spec = ss.normalize_spec(dict(SPEC, intent="Delete-Everything", touchpoints=[
        {"repo": "", "path": "cs/clients/linux/x.sh"},
        {"repo": "LM", "path": "./lm/WebUI/sim-views.js"}]))
    assert spec["intent"] == ""
    assert spec["touchpoints"][0]["repo"] == "cs"
    assert spec["touchpoints"][0]["path"] == "clients/linux/x.sh"
    assert spec["touchpoints"][1] == {"repo": "lm", "path": "WebUI/sim-views.js", "change": ""}


@pytest.mark.parametrize("repo,path,ok", [
    ("cs", "clients/linux/dns_fail_lrb.sh", True),
    ("cs", "clients/windows/common.ps1", True),
    ("cs", "configs/simulation.conf", True),
    ("cs", "lm-spoke/static/sim-views.js", True),
    ("cs", "lm-spoke/src/sim_quota.py", True),
    ("lm", "WebUI/sim-views.js", True),
    ("lm", "core/src/simulations/sim_quota.py", True),
    ("lm", "docs/alert-generation.md", True),
    ("lm", "core/src/security/mtls.py", False),
    ("lm", "core/src/main.py", False),
    ("lm", "WebUI/main.js", False),
    ("lm", ".github/workflows/promote.yml", False),
    ("cs", "lm-spoke/src/cs_spoke.py", False),
    ("cs", "install_cs.sh", False),
    ("cs", "clients/../install_cs.sh", False),
    ("cs", "/etc/passwd", False),
    ("ab", "main.py", False),
])
def test_path_allowlist(repo, path, ok):
    assert ss.path_allowed(repo, path, ss.DEFAULT_ALLOWED_PATHS) is ok


def test_spec_problems_scope():
    known = {"dns_fail", "iperf"}
    good = ss.normalize_spec(SPEC)
    assert ss.spec_problems(good, {"cs", "lm"}, None, known) == []
    # explain never builds
    assert ss.spec_problems(dict(good, intent="explain", target="dns_fail"), {"cs", "lm"}, None, known)
    # missing intent
    assert ss.spec_problems(dict(good, intent=""), {"cs", "lm"}, None, known)
    # modify must target an existing sim
    assert ss.spec_problems(dict(good, intent="modify", target=""), {"cs", "lm"}, None, known)
    assert any("not an existing" in p for p in ss.spec_problems(
        dict(good, intent="modify", target="nope"), {"cs", "lm"}, None, known))
    ok_mod = dict(good, intent="modify", target="dns_fail", modify_confirmed=True,
                  diagnosis="dns_fail.sh ignores dns_fail_rate on Windows", verified_broken=True)
    assert ss.spec_problems(ok_mod, {"cs", "lm"}, None, known) == []
    # create must not collide with an existing sim
    assert ss.spec_problems(dict(good, name="iperf"), {"cs", "lm"}, None, known)
    # non-simulation file / repo
    bad = dict(good, touchpoints=good["touchpoints"] + [
        {"repo": "lm", "path": "core/src/security/mtls.py", "change": "x"}])
    assert any("outside the simulation build surface" in p for p in ss.spec_problems(
        bad, {"cs", "lm"}, None, known))
    other = dict(good, touchpoints=[{"repo": "ab", "path": "main.py", "change": "x"}])
    assert any("not in a simulation repo" in p for p in ss.spec_problems(
        other, {"cs", "lm"}, None, known))


def test_build_command_is_read_only_and_resumes():
    first = ss.build_copilot_command("copilot", "hi", "sid", False, "/ws", model="m")
    i = first.index("--available-tools")
    assert first[i + 1:i + 4] == ["view", "grep", "glob"]
    for banned in ("bash", "edit", "create", "--allow-all", "--yolo"):
        assert banned not in first
    assert first[first.index("--session-id") + 1] == "sid"
    assert first[first.index("--model") + 1] == "m"
    later = ss.build_copilot_command("copilot", "hi", "sid", True, "/ws")
    assert "--resume" in later and "--session-id" not in later and "--model" not in later


def test_event_from_cli_mapping():
    ev = ss.event_from_cli({"type": "tool.execution_start", "data": {
        "toolName": "view", "toolTitle": "Viewing file",
        "arguments": {"path": "/base/workspace/cs/clients/linux/dns_fail.sh"}}}, "/base/workspace")
    assert ev == {"kind": "tool", "text": "Viewing file: cs/clients/linux/dns_fail.sh"}
    real = tempfile.mkdtemp()
    link = real + "-link"
    os.symlink(real, link)
    ev = ss.event_from_cli({"type": "tool.execution_start", "data": {
        "toolTitle": "Viewing file", "arguments": {"path": os.path.realpath(real) + "/lm/AGENTS.md"}}}, link)
    assert ev["text"] == "Viewing file: lm/AGENTS.md"
    ev = ss.event_from_cli({"type": "tool.execution_start", "data": {
        "toolTitle": "Searching", "arguments": {"pattern": "x", "path": link + "/cs"}}}, link)
    assert ev["text"] == "Searching: x in cs"
    assert ss._relativize("/elsewhere/ws/a", "/ws") == "/elsewhere/ws/a"
    assert ss.event_from_cli({"type": "assistant.message_delta", "ephemeral": True, "data": {}}) is None
    assert ss.event_from_cli({"type": "assistant.message", "data": {"content": ""}}) is None
    assert ss.event_from_cli({"type": "assistant.message", "data": {"content": "x"}})["kind"] == "assistant_raw"
    r = ss.event_from_cli({"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}})
    assert r["kind"] == "result" and r["usage"]["premiumRequests"] == 1
    assert ss.event_from_cli({"type": "session.error", "data": {"message": "boom"}})["text"] == "boom"


def test_resolve_repos_prefers_explicit_and_filters_defaults():
    cfg = {"feature_drive_repos": ["o/cs", "o/lm", "o/netbox"], "bug_report_repo": "o/lm"}
    assert ss.SimSessionManager.resolve_repos(cfg) == {"cs": "o/cs", "lm": "o/lm"}
    assert ss.SimSessionManager.resolve_repos({"sim_assistant_repos": ["o/x"]}) == {"x": "o/x"}


def test_copilot_token_prefers_override_then_copilot_cred():
    cfg = {"llm_credentials": {"anthropic": {"api_key": "a"}, "copilot-2": {"api_key": "c2"},
                               "copilot": {"api_key": "c1"}}}
    assert ss.SimSessionManager.copilot_token(cfg) == "c1"
    assert ss.SimSessionManager.copilot_token(dict(cfg, sim_assistant_copilot_token="o")) == "o"
    assert ss.SimSessionManager.copilot_token({}) == ""


def test_issue_body_has_feature_marker_and_scopes_touchpoints():
    sess = {"id": "abc", "owner": {"user": "u1", "tenant": "t1"}, "spec": ss.normalize_spec(SPEC),
            "events": [{"kind": "user", "text": "make dns fail"},
                       {"kind": "tool", "text": "Viewing"},
                       {"kind": "assistant", "text": "ok"}]}
    body = ss.render_issue_body(sess, "cs", "o/cs", ["o/lm#2 (url)"])
    assert body.startswith("<!-- report-type: feature -->")
    assert "<!-- ab-sim-session: abc -->" in body
    mine = body.split("## Touch-points in this repo (cs)")[1].split("##")[0]
    assert "dns_fail_lrb.sh" in mine and "sim-views.js" not in mine
    assert "NOT built here" in body and "o/lm#2" in body
    assert "**User:** make dns fail" in body and "Viewing" not in body.split("transcript")[-1]
    assert len(ss.render_issue_body(dict(sess, events=[{"kind": "user", "text": "x" * 200000}]),
                                    "cs", "o/cs")) < ss.ISSUE_BODY_MAX + 1000


# ── manager end-to-end with a fake copilot CLI ──────────────────────────────

FAKE_CLI = r'''#!/usr/bin/env python3
import json, sys, os
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
log = os.path.join(os.environ["COPILOT_HOME"], "calls.jsonl")
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "token": os.environ.get("COPILOT_GITHUB_TOKEN"),
                        "gh": os.environ.get("GITHUB_TOKEN")}) + "\n")
def out(o): print(json.dumps(o), flush=True)
if "FAIL" in prompt:
    print("Error: auth failed", flush=True); sys.exit(1)
ws = args[args.index("-C") + 1]
out({"type": "tool.execution_start", "data": {"toolName": "view", "toolTitle": "Viewing file",
     "arguments": {"path": ws + "/cs/clients/linux/dns_fail.sh"}}})
out({"type": "assistant.message_delta", "ephemeral": True, "data": {"deltaContent": "x"}})
ready = "READY" in prompt
spec = %SPEC%
spec["ready"] = ready
spec["open_questions"] = [] if ready else ["What rate?"]
out({"type": "assistant.message", "data": {"content": "What rate?\n\n```sim-spec\n" + json.dumps(spec) + "\n```"}})
out({"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}})
'''


class _Issue:
    def __init__(self, n, repo):
        self.number = n
        self.html_url = f"https://github.com/{repo}/issues/{n}"
        self.body = None

    def edit(self, body=None):
        self.body = body


class _Repo:
    def __init__(self, name, sink):
        self.name, self.sink = name, sink

    def create_issue(self, title, body, labels):
        iss = _Issue(len(self.sink) + 1, self.name)
        iss.body = body
        self.sink.append((self.name, title, labels, iss))
        return iss


class _GH:
    def __init__(self):
        self.created = []

    def get_repo(self, name):
        return _Repo(name, self.created)


class _Skills:
    @staticmethod
    def get_loaded():
        return {"add-simulation": {}}

    @staticmethod
    def skill_files(name):
        return {"SKILL.md": "# recipe", "reference.md": "# ref"}


@pytest.fixture
def mgr(tmp_path):
    cli = tmp_path / "copilot"
    cli.write_text(FAKE_CLI.replace("%SPEC%", repr(SPEC)))
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    cfg = {"copilot_binary": str(cli), "llm_credentials": {"copilot": {"api_key": "ghu_x"}},
           "GITHUB_TOKEN": "ghp_repo", "sim_assistant_repos": ["o/cs", "o/lm"]}
    gh = _GH()
    processed = {}
    m = ss.SimSessionManager(str(tmp_path / "base"), lambda: cfg, gh_factory=lambda c: gh,
                             processed_loader=lambda: processed, skills_provider=_Skills,
                             boundaries_renderer=lambda b: "- no auth changes")
    m.refresh_workspace = lambda force=False: m._materialize_skills()
    m._gh, m._processed, m._cfgd = gh, processed, cfg
    return m


def _wait_idle(m, sid, owner, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = m.poll(owner, sid)
        if st["state"] == "awaiting_user":
            return st
        time.sleep(0.05)
    raise AssertionError("turn did not finish")


OWNER = {"user": "alice", "tenant": "t1"}


def test_session_flow_start_reply_submit(mgr):
    st = mgr.start(OWNER, "I want dns_fail for LRB")
    sid = st["session_id"]
    st = _wait_idle(mgr, sid, OWNER)
    kinds = [e["kind"] for e in st["events"]]
    assert kinds[0] == "user" and "tool" in kinds and "assistant" in kinds and "spec" in kinds
    tool = next(e for e in st["events"] if e["kind"] == "tool")
    assert tool["text"] == "Viewing file: cs/clients/linux/dns_fail.sh"
    assert st["ready"] is False and st["choices"] == ["Yes", "No"]
    assert st["premium_requests"] == 1
    assert os.path.isfile(os.path.join(mgr.workspace, "skills", "add-simulation", "SKILL.md"))

    calls = [json.loads(l) for l in open(os.path.join(mgr.copilot_home, "calls.jsonl"))]
    a0 = calls[0]["args"]
    assert "--session-id" in a0 and a0[a0.index("--session-id") + 1] == sid
    first_prompt = a0[a0.index("-p") + 1]
    assert "sim-spec" in first_prompt and "AGENTS.md" in first_prompt and "no auth changes" in first_prompt
    assert calls[0]["token"] == "ghu_x" and calls[0]["gh"] is None

    with pytest.raises(RuntimeError):
        mgr.submit(OWNER, sid)

    seq = st["seq"]
    mgr.reply(OWNER, sid, "rate 5, READY")
    st = _wait_idle(mgr, sid, OWNER)
    assert all(e["seq"] > seq for e in mgr.poll(OWNER, sid, since=seq)["events"])
    assert st["ready"] is True
    calls = [json.loads(l) for l in open(os.path.join(mgr.copilot_home, "calls.jsonl"))]
    assert "--resume" in calls[1]["args"]

    out = mgr.submit(OWNER, sid)
    assert out["state"] == "submitted"
    repos = [c[0] for c in mgr._gh.created]
    assert repos == ["o/cs", "o/lm"]
    assert all(c[2] == ["enhancement"] for c in mgr._gh.created)
    assert "o/lm#2" in mgr._gh.created[0][3].body
    with pytest.raises(RuntimeError):
        mgr.reply(OWNER, sid, "more")

    mgr._processed["o/cs:1"] = {"status": "feature_built", "pr_url": "https://pr/1"}
    builds = mgr.poll(OWNER, sid)["builds"]
    assert builds[0]["status"] == "feature_built" and builds[0]["done"] and builds[0]["pr_url"]
    assert builds[1]["status"] == "queued" and not builds[1]["done"]

    again = mgr.submit(OWNER, sid)
    assert len(mgr._gh.created) == 2 and again["submitted"]


def test_ownership_is_enforced(mgr):
    sid = mgr.start(OWNER, "hi")["session_id"]
    _wait_idle(mgr, sid, OWNER)
    with pytest.raises(LookupError):
        mgr.poll({"user": "bob", "tenant": "t1"}, sid)
    with pytest.raises(LookupError):
        mgr.poll({"user": "alice", "tenant": "t2"}, sid)
    assert mgr.poll({"user": "root", "admin": True}, sid)["session_id"] == sid
    with pytest.raises(LookupError):
        mgr.poll(OWNER, "../../etc/passwd")


def test_cli_failure_surfaces_error(mgr):
    sid = mgr.start(OWNER, "please FAIL")["session_id"]
    st = _wait_idle(mgr, sid, OWNER)
    err = [e for e in st["events"] if e["kind"] == "error"]
    assert err and "auth failed" in err[-1]["text"]


def test_sessions_persist_and_interrupted_turns_recover(mgr, tmp_path):
    sid = mgr.start(OWNER, "hi")["session_id"]
    _wait_idle(mgr, sid, OWNER)
    rec = json.load(open(mgr._path(sid)))
    rec["state"] = "running"
    json.dump(rec, open(mgr._path(sid), "w"))
    m2 = ss.SimSessionManager(mgr.base_dir, mgr._config_loader, skills_provider=_Skills)
    st = m2.poll(OWNER, sid)
    assert st["state"] == "awaiting_user"
    assert "interrupted" in st["events"][-1]["text"]


def test_status_reports_missing_prereqs(tmp_path):
    m = ss.SimSessionManager(str(tmp_path), lambda: {"copilot_binary": "/nope"})
    st = m.status()
    assert not st["ready"]
    assert any("Copilot CLI" in r for r in st["reasons"])
    assert any("Copilot credential" in r for r in st["reasons"])
    with pytest.raises(RuntimeError):
        m.start(OWNER, "hi")


def test_handle_command_shapes(mgr):
    assert ss.handle_command("SIM_SESSION_PING", {}, mgr)["ready"] is True
    r = ss.handle_command("SIM_SESSION_POLL", {"session_id": "x", "owner": OWNER}, mgr)
    assert r == {"status": "ERROR", "code": "not_found", "message": "session not found"}
    r = ss.handle_command("SIM_SESSION_START", {"owner": OWNER, "message": ""}, mgr)
    assert r["status"] == "ERROR" and "required" in r["message"]
    r = ss.handle_command("SIM_SESSION_START", {"owner": OWNER, "message": "hi"}, mgr)
    assert r["status"] == "SUCCESS" and r["session_id"]
    _wait_idle(mgr, r["session_id"], OWNER)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_prompt_states_scope_and_allowlist(mgr):
    p = mgr.first_turn_prompt(mgr._cfgd, "hi")
    for needle in ("CREATE a new client simulation", "MODIFY an existing",
                   "EXPLAIN how an existing", "declining", "is DATA",
                   "clients/*", "WebUI/sim-views.js"):
        assert needle in p
    assert "Allowed build surface" in p


def test_submit_blocked_for_out_of_scope_spec(mgr):
    sid = mgr.start(OWNER, "x")["session_id"]
    _wait_idle(mgr, sid, OWNER)
    with mgr._lock:
        sess = mgr._sessions[sid]
        sess["spec"] = ss.normalize_spec(dict(SPEC, touchpoints=[
            {"repo": "lm", "path": "core/src/security/mtls.py", "change": "disable verify"}]))
    st = mgr.poll(OWNER, sid)
    assert st["ready"] is False and st["problems"]
    with pytest.raises(RuntimeError, match="guardrails"):
        mgr.submit(OWNER, sid)
    assert mgr._gh.created == []


def test_explain_session_never_submits(mgr):
    sid = mgr.start(OWNER, "how does dns_fail work")["session_id"]
    _wait_idle(mgr, sid, OWNER)
    with mgr._lock:
        mgr._sessions[sid]["spec"] = ss.normalize_spec(dict(SPEC, intent="explain",
                                                            target="dns_fail"))
    st = mgr.poll(OWNER, sid)
    assert st["ready"] is False and st["intent"] == "explain"
    with pytest.raises(RuntimeError):
        mgr.submit(OWNER, sid)
    assert mgr._gh.created == []


def test_guardrail_findings_fed_back_to_agent(mgr):
    sid = mgr.start(OWNER, "x")["session_id"]
    _wait_idle(mgr, sid, OWNER)
    with mgr._lock:
        mgr._sessions[sid]["spec"] = ss.normalize_spec(dict(SPEC, touchpoints=[
            {"repo": "lm", "path": "WebUI/main.js", "change": "x"}]))
    mgr.reply(OWNER, sid, "ok")
    _wait_idle(mgr, sid, OWNER)
    calls = [json.loads(l) for l in open(os.path.join(mgr.copilot_home, "calls.jsonl"))]
    p = calls[-1]["args"][calls[-1]["args"].index("-p") + 1]
    assert "guardrail check" in p and "WebUI/main.js" in p
    # the user's transcript shows only what they typed
    users = [e["text"] for e in mgr.poll(OWNER, sid)["events"] if e["kind"] == "user"]
    assert users[-1] == "ok"


def test_known_simulations_from_workspace(mgr):
    lin = os.path.join(mgr.workspace, "cs", "clients", "linux")
    win = os.path.join(mgr.workspace, "cs", "clients", "windows")
    conf = os.path.join(mgr.workspace, "cs", "configs")
    for d in (lin, win, conf):
        os.makedirs(d, exist_ok=True)
    for f in ("dns_fail.sh", "common.sh", "connect_wifi.sh", "simulation.sh"):
        open(os.path.join(lin, f), "w").close()
    open(os.path.join(win, "collab.ps1"), "w").close()
    with open(os.path.join(conf, "simulation.conf"), "w") as fh:
        fh.write("[simulation]\nauth_fail=off\niperf_bw=1k\nkill_switch=off\n")
    names = mgr.known_simulations()
    assert {"dns_fail", "collab", "auth_fail"} <= names
    assert not names & {"common", "connect_wifi", "simulation", "iperf_bw"}


# ── changing an existing simulation: warn → confirm → verify → steer → override ──

MOD = dict(SPEC, intent="modify", target="dns_fail", name="dns_fail",
           touchpoints=[{"repo": "cs", "path": "configs/simulation.conf", "change": "rate 500"}])


def _probs(**kw):
    return ss.spec_problems(ss.normalize_spec(dict(MOD, **kw)), {"cs", "lm"}, None, {"dns_fail"})


def test_modify_requires_confirmation():
    assert any("explicit confirmation" in p for p in _probs())
    assert any("explicit confirmation" in p for p in _probs(modify_confirmed="yes"))  # must be true


def test_modify_requires_diagnosis():
    assert any("verified against the code" in p for p in _probs(modify_confirmed=True))


def test_modify_working_sim_needs_override_reason():
    probs = _probs(modify_confirmed=True, diagnosis="Works as built: rate is 750 by design.")
    assert any("works as built" in p for p in probs)
    assert _probs(modify_confirmed=True, diagnosis="Works as built.",
                  override_reason="Our lab's DNS servers drop above 500 qps.",
                  modify_reason="behavior_change") == []


def test_modify_verified_broken_is_submittable():
    assert _probs(modify_confirmed=True, diagnosis="Windows ignores the rate knob.",
                  verified_broken=True, modify_reason="broken") == []


def test_prompt_has_modify_protocol(mgr):
    p = mgr.first_turn_prompt(mgr._cfgd, "hi")
    for needle in ("WARN first", "duplicate_of", "modify_confirmed", "VERIFY against the real code",
                   "verified_broken", "override_reason", "dedicated PR", "steer"):
        assert needle in p


def _ready_session(mgr, spec):
    sid = mgr.start(OWNER, "x")["session_id"]
    _wait_idle(mgr, sid, OWNER)
    with mgr._lock:
        mgr._sessions[sid]["spec"] = ss.normalize_spec(spec)
    return sid


def test_modify_submit_blocked_until_protocol_done(mgr):
    os.makedirs(os.path.join(mgr.workspace, "cs", "clients", "linux"), exist_ok=True)
    open(os.path.join(mgr.workspace, "cs", "clients", "linux", "dns_fail.sh"), "w").close()
    sid = _ready_session(mgr, dict(MOD, modify_confirmed=True))
    with pytest.raises(RuntimeError, match="guardrails"):
        mgr.submit(OWNER, sid)
    assert mgr._gh.created == []


def test_modify_submit_dedicated_labelled_issue_with_audit(mgr):
    os.makedirs(os.path.join(mgr.workspace, "cs", "clients", "linux"), exist_ok=True)
    open(os.path.join(mgr.workspace, "cs", "clients", "linux", "dns_fail.sh"), "w").close()
    sid = _ready_session(mgr, dict(MOD, modify_confirmed=True, diagnosis="Works as built; 750 qps default.",
                                   override_reason="Lab DNS drops above 500.", modify_reason="behavior_change"))
    submitter = {"user": "admin1", "tenant": "", "admin": True, "auth_type": "sso", "ip": "10.0.0.5"}
    st = mgr.submit(submitter, sid)
    assert len(st["submitted"]) == 1                       # one repo -> one dedicated issue/PR
    repo, title, labels, issue = mgr._gh.created[0]
    assert repo == "o/cs" and ss.MODIFY_LABEL in labels and "enhancement" in labels
    assert "existing `dns_fail`" in title
    head = issue.body[:1500]                               # what pr_template quotes into the PR
    for needle in ("<!-- report-type: feature -->", "<!-- ab-sim-modify: dns_fail -->",
                   "dedicated PR", "user override", "Works as built", "Lab DNS drops above 500",
                   "Requested by (LM user):** `alice`", "Submitted by (LM user):** `admin1`",
                   "sso, 10.0.0.5", sid):
        assert needle in head, needle
    recs = [json.loads(l) for l in open(os.path.join(mgr.base_dir, "audit.jsonl"))]
    r = recs[-1]
    assert r["session"] == sid and r["intent"] == "modify" and r["target"] == "dns_fail"
    assert r["requested_by"]["user"] == "alice" and r["submitted_by"]["user"] == "admin1"
    assert r["override_reason"] == "Lab DNS drops above 500." and r["issues"] == [issue.html_url]


def test_create_submit_records_user_and_duplicate_of(mgr):
    sid = _ready_session(mgr, dict(SPEC, duplicate_of="dns_fail"))
    mgr.submit(dict(OWNER, auth_type="local"), sid)
    for _repo, title, labels, issue in mgr._gh.created:
        assert ss.MODIFY_LABEL not in labels and title.startswith("Simulation: ")
        assert "Requested by (LM user):** `alice`" in issue.body[:1500]
        assert "Duplicated from:** `dns_fail`" in issue.body
    assert os.path.isfile(os.path.join(mgr.base_dir, "audit.jsonl"))

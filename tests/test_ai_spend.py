"""AppBuilder's daily AI spend gate (fleet rule 1.10, ai_spend.py): the cost
estimate, the gate closing once, a person's Continue, the day rollover and
restarts, failing open, and the call path refusing a paid call."""
import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient

import main  # noqa: F401 - loads workers in import order
import ai_spend
import auth
import config_store
import llm_client
import routes

PRICES = {"claude-sonnet-5": {"input": 3.0, "cache_read": 0.3, "output": 15.0},
          "claude-opus-5.5": {"input": 5.0, "cache_read": 0.5, "output": 25.0}}
PAID = ("copilot", "claude-sonnet-5")
FREE = ("openrouter", "qwen/qwen3-coder:free")
ON, OFF = {"AI_SPEND_GATE": True}, {"AI_SPEND_GATE": False}
SIX_USD = {"input_tokens": 1, "output_tokens": 400_000}  # about $6 at the claude-sonnet-5 price


class _Log:
    """Collects ai_spend's log lines by level."""

    def __init__(self):
        """No lines yet."""
        self.lines = []

    def __getattr__(self, level):
        """logger.<level>(msg, *args) appends (level, formatted msg)."""
        return lambda msg, *args, **_kw: self.lines.append((level, msg % args if args else msg))


@pytest.fixture(autouse=True)
def log(tmp_path, monkeypatch):
    """A $10 gate with a $5 step, its policy and ledger in tmp_path, on a fixed day."""
    policy = tmp_path / "ai-budget.json"
    policy.write_text(json.dumps({"gate_usd": 10, "step_usd": 5, "prices_usd_per_mtok": PRICES}))
    monkeypatch.setattr(ai_spend, "POLICY_FILE", str(policy))
    monkeypatch.setattr(config_store, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(ai_spend, "_ledger", None)
    monkeypatch.setattr(ai_spend, "_today", lambda: "2026-11-21")
    rec = _Log()
    monkeypatch.setattr(ai_spend, "logger", rec)
    return rec


def test_cost_prices_cached_input_at_the_cache_read_rate():
    """Cached input costs the cache-read price; the rest the input and output prices."""
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 100_000}
    assert ai_spend.record(*PAID, usage, ON) == pytest.approx(0.6 * 3 + 0.4 * 0.3 + 0.1 * 15)
    assert ai_spend.status(ON)["spent_usd"] == pytest.approx(3.42)


def test_unlisted_models_and_missing_token_counts_err_high():
    """An unlisted model costs the dearest listed row; a reply with no counts is sized from its text."""
    assert ai_spend.price_of("Claude-Sonnet-5", PRICES) == PRICES["claude-sonnet-5"]
    assert ai_spend.price_of("github/claude-sonnet-5:latest", PRICES) == PRICES["claude-sonnet-5"]
    assert ai_spend.price_of("gpt-9", PRICES) == PRICES["claude-opus-5.5"]
    assert ai_spend.price_of("gpt-9", {}) == ai_spend.FALLBACK_PRICE
    # About 4 characters a token: 4017 characters of prompt JSON in, 402 out.
    usd = ai_spend.record(*PAID, {}, ON, messages=[{"content": "x" * 4000}], result="y" * 400)
    assert usd == pytest.approx((1004 * 3 + 100 * 15) / 1e6)


def test_free_and_self_hosted_models_cost_nothing():
    """Free-tier and self-hosted calls add nothing, so they never close the gate."""
    big = {"input_tokens": 10**7, "output_tokens": 10**7}
    assert ai_spend.record(*FREE, big, ON) == 0
    assert ai_spend.record("ollama", "qwen2.5-coder:14b", big, ON) == 0
    assert ai_spend.status(ON)["calls"] == 0


def test_gate_closes_once_and_a_person_reopens_it(log):
    """Paid calls stop at the gate with one WARNING (not ERROR, so no issue is
    filed); free calls go on; Continue adds the step to what is already spent."""
    ai_spend.record(*PAID, SIX_USD, ON)
    assert not ai_spend.blocks(*PAID, ON)
    ai_spend.record(*PAID, SIX_USD, ON)
    ai_spend.record(*PAID, SIX_USD, ON)
    assert ai_spend.blocks(*PAID, ON) and not ai_spend.blocks(*FREE, ON)
    assert [lvl for lvl, msg in log.lines if "gate closed" in msg] == ["warning"]
    gate, spent = ai_spend.reopen()
    assert (round(gate), round(spent)) == (23, 18)
    st = ai_spend.status(ON)
    assert st["open"] and st["gate_usd"] == 23 and st["left_usd"] == pytest.approx(5)


def test_gate_turned_off_in_settings_never_closes(log):
    """AI_SPEND_GATE off: spend is still counted, but nothing waits and nothing is logged."""
    for _ in range(3):
        ai_spend.record(*PAID, SIX_USD, OFF)
    st = ai_spend.status(OFF)
    assert st["open"] and not st["enabled"] and round(st["spent_usd"]) == 18
    assert not ai_spend.blocks(*PAID, OFF)
    assert not [msg for _, msg in log.lines if "gate closed" in msg]


def test_spend_survives_a_restart_and_resets_the_next_day(monkeypatch):
    """Today's spend and a reopened gate are on disk; a new local day starts from nothing."""
    ai_spend.record(*PAID, SIX_USD, ON)
    ai_spend.reopen()
    monkeypatch.setattr(ai_spend, "_ledger", None)
    st = ai_spend.status(ON)
    assert (round(st["spent_usd"]), st["gate_usd"], st["calls"]) == (6, 15, 1)
    monkeypatch.setattr(ai_spend, "_today", lambda: "2026-11-22")
    st = ai_spend.status(ON)
    assert (st["spent_usd"], st["gate_usd"], st["calls"]) == (0, 10, 0)


def test_a_broken_gate_fails_open(log, monkeypatch, tmp_path):
    """An unreadable policy uses the defaults; a gate check that raises lets calls run, and is logged."""
    (tmp_path / "ai-budget.json").write_text("{not json")
    assert ai_spend.policy() == dict(ai_spend.DEFAULTS, prices={})

    def boom(_config):
        """A gate check that fails."""
        raise RuntimeError("disk gone")
    monkeypatch.setattr(ai_spend, "status", boom)
    assert not ai_spend.closed(ON) and not ai_spend.blocks(*PAID, ON)
    assert [lvl for lvl, _ in log.lines] == ["exception", "exception"]


def _fake_provider(sent):
    """A _call_provider stand-in that reports $6 of usage and records each call."""
    def call(*args, usage_out=None, **_kw):
        """One successful call."""
        sent.append(args[:2])
        usage_out.update(SIX_USD)
        return "ok"
    return call


def test_call_path_counts_paid_calls_then_refuses_them_before_sending(monkeypatch):
    """_call_provider_timed adds each paid call's cost; once the gate is closed it
    raises GateClosed before sending, without marking the entry failing."""
    sent, failed = [], []
    monkeypatch.setattr(llm_client, "_call_provider", _fake_provider(sent))
    monkeypatch.setattr(llm_client, "_record_llm_failure", lambda *a: failed.append(a))
    monkeypatch.setattr(llm_client, "_get_llm_perf_store", lambda: {})
    monkeypatch.setattr(llm_client.llm_perf, "save", lambda *a: None)
    msgs = [{"role": "user", "content": "hi"}]
    for _ in range(2):
        assert llm_client._call_provider_timed(*PAID, "k", "", msgs, None, False, None, ON) == "ok"
    assert round(ai_spend.status(ON)["spent_usd"]) == 12
    with pytest.raises(ai_spend.GateClosed, match="spend_gate"):
        llm_client._call_provider_timed(*PAID, "k", "", msgs, None, False, None, ON)
    assert llm_client._call_provider_timed(*FREE, "", "", msgs, None, False, None, ON) == "ok"
    assert sent == [PAID, PAID, FREE] and not failed


def test_continue_route_reopens_the_gate(monkeypatch):
    """POST /ai_spend/continue opens the gate for one more step, and GET /ai_spend shows it."""
    monkeypatch.setattr(ai_spend, "load_config", lambda: ON)
    for _ in range(2):
        ai_spend.record(*PAID, SIX_USD, ON)
    assert not asyncio.run(routes.get_ai_spend())["open"]
    reply = asyncio.run(routes.continue_ai_spend())
    assert reply == {"status": "ok", "gate_usd": 17.0, "spent_usd": 12.0,
                     "message": "AI spend gate open until $17 today ($12 used)."}
    assert asyncio.run(routes.get_ai_spend())["open"]


@pytest.fixture
def topbar(monkeypatch):
    """Render the dashboard's topbar, as a logged-in user sees it, for a given config."""
    monkeypatch.setattr(auth, "any_users", lambda: True)
    monkeypatch.setattr(auth, "verify_session", lambda token: "admin")
    monkeypatch.setattr(routes, "load_config", lambda: {"dashboard_mission_control": False})
    client = TestClient(main.app)

    def render(config):
        """The topbar markup with the gate reading ``config``."""
        monkeypatch.setattr(ai_spend, "load_config", lambda: config)
        return client.get("/").text.split('<header class="hpe-topbar', 1)[1].split("</header>", 1)[0]
    return render


def test_topbar_shows_spend_and_offers_continue_only_once_the_gate_closes(topbar):
    """The pill shows spend against the gate with Continue hidden; once the gate
    closes it turns amber and Continue shows; with the gate off there is neither."""
    ai_spend.record(*PAID, SIX_USD, ON)
    bar = topbar(ON)
    assert "AI $6/$10" in bar and 'id="hdr-ai-continue"' in bar
    assert re.search(r'id="hdr-ai-continue"[^>]*class="hdr-btn hidden"', bar)
    ai_spend.record(*PAID, SIX_USD, ON)
    bar = topbar(ON)
    assert re.search(r'id="hdr-ai-spend" class="hdr-pill hdr-pill-warn[^"]*"[^>]*>AI \$12/\$10<', bar)
    assert re.search(r'onclick="continueAiSpend\(\)" id="hdr-ai-continue" title="Reopen the AI spend gate '
                     r'for \$5 more today" class="hdr-btn">Continue<', bar)
    bar = topbar(OFF)
    assert "hdr-ai-spend" not in bar and "hdr-ai-continue" not in bar

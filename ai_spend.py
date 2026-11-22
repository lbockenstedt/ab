"""Daily gate on AppBuilder's cloud AI spend (fleet rule 1.10).

The gate stops a runaway process; it is not a budget. Once today's estimated
spend on paid models reaches the gate, llm_client stops offering them
(unavailable_reason "spend_gate", a timed cooldown like a credit one), so work
that needs them waits while free models keep working. A person reopens it for
step_usd more with the topbar's Continue button (POST /ai_spend/continue).
The AI_SPEND_GATE setting turns it off.

The numbers and per-model prices come from .github/ai-budget.json, which lm's
fleet-file sync keeps in step with lm; the Copilot CLI hook reads the same
file. AB's API replies carry token counts but no cost, so spend is estimated:
(input - cached) x input price + cached x cache_read price + output x output
price, per 1M tokens. A model the table does not list is charged at the
dearest listed price, and a reply with no token counts (a streamed one) at
about 4 characters a token, so the estimate errs high and the gate closes
early rather than late. Free models (cost tier "free" in model_registry) and
self-hosted providers cost nothing.

The day is the local calendar day. Today's spend and any reopened gate are kept
in CONFIG_DIR/ai_spend.json, so a restart does not reset them.
"""
import datetime
import json
import os
import threading

import config_store
import model_registry
from main import logger, load_config

POLICY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github", "ai-budget.json")
DEFAULTS = {"gate_usd": 100.0, "step_usd": 100.0, "reserve_usd": 25.0}
# lm's dearest price when this was written, used until the sync brings the
# table: an over-estimate closes the gate early, never late.
FALLBACK_PRICE = {"input": 7.0, "cache_read": 0.7, "output": 35.0}
_LOCK = threading.Lock()
# Per-thread running cost of paid calls, for fix_outcomes' per-attempt cost.
_tally = threading.local()
# Today's {"day", "spent_usd", "calls", and "gate_usd" once reopened}; read from disk once.
_ledger = None


class GateClosed(Exception):
    """A paid model call refused because today's AI spend gate is closed."""


def _ledger_file():
    """Where today's spend is kept (CONFIG_DIR is read at call time so tests can move it)."""
    return os.path.join(config_store.CONFIG_DIR, "ai_spend.json")


def policy():
    """The fleet's gate numbers (USD) and prices from .github/ai-budget.json; defaults if unreadable."""
    try:
        with open(POLICY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        pol = {k: float(data.get(k, v)) for k, v in DEFAULTS.items()}
        prices = data.get("prices_usd_per_mtok")
        pol["prices"] = prices if isinstance(prices, dict) else {}
        return pol
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(DEFAULTS, prices={})


def price_of(model, prices):
    """USD per 1M tokens for ``model``: its row (ignoring any vendor/ prefix or
    :suffix), else the dearest listed row, else FALLBACK_PRICE."""
    name = str(model or "").lower().rsplit("/", 1)[-1].split(":", 1)[0]
    rows = {k.lower(): v for k, v in prices.items() if isinstance(v, dict)}
    if name in rows:
        return rows[name]
    return max(rows.values(), key=lambda r: float(r.get("output") or 0), default=FALLBACK_PRICE)


def metered(provider, model, config):
    """True when a call to this model is paid: not self-hosted and not cost tier "free"."""
    if model_registry.is_local_provider(provider):
        return False
    return model_registry.resolve(provider, model, config).get("cost_tier") != "free"


def _tokens(value):
    """About how many tokens ``value`` holds (4 characters a token), for replies that report none."""
    try:
        return len(json.dumps(value, default=str)) // 4
    except (TypeError, ValueError):
        return 0


def _today():
    """Today's local date: the gate's day."""
    return datetime.date.today().isoformat()


def _load(day):
    """Today's ledger: in memory, else from disk, else a new one. The caller holds _LOCK."""
    global _ledger
    if _ledger is None:
        try:
            with open(_ledger_file(), encoding="utf-8") as fh:
                _ledger = json.load(fh)
        except (OSError, ValueError):
            _ledger = {}
    # A new day starts from nothing, with the fleet's gate.
    if not isinstance(_ledger, dict) or _ledger.get("day") != day:
        _ledger = {"day": day, "spent_usd": 0.0, "calls": 0}
    return _ledger


def _save():
    """Write the ledger to disk. The caller holds _LOCK; a failure keeps the count in memory."""
    try:
        config_store._atomic_write_json(_ledger_file(), _ledger)
    except Exception as e:  # noqa: BLE001 -- the gate must never fail an LLM call
        logger.warning("AI spend gate: could not save %s: %s", _ledger_file(), e)


def _gate(pol, led):
    """Today's gate: the fleet's, or higher when a person reopened it today."""
    return max(pol["gate_usd"], float(led.get("gate_usd") or 0))


def status(config=None):
    """Today's spend and gate, for the topbar, the API and the call path."""
    config = load_config() if config is None else config
    pol = policy()
    with _LOCK:
        led = _load(_today())
        day, spent, gate = led["day"], float(led.get("spent_usd") or 0), _gate(pol, led)
        calls = int(led.get("calls") or 0)
    enabled = bool(config.get("AI_SPEND_GATE", True))
    return {"enabled": enabled, "day": day, "spent_usd": round(spent, 2), "gate_usd": round(gate, 2),
            "left_usd": round(max(gate - spent, 0), 2), "open": not enabled or spent < gate,
            "calls": calls, "step_usd": pol["step_usd"]}


def closed(config):
    """True when the gate is on and today's spend has reached it.

    Fails open: a broken gate is logged (and so filed) but never stops AB."""
    try:
        return not status(config)["open"]
    except Exception:  # noqa: BLE001
        logger.exception("AI spend gate check failed; letting calls run")
        return False


def blocks(provider, model, config):
    """True when the gate is closed and this call would be paid."""
    return closed(config) and metered(provider, model, config)


def seconds_left_today():
    """Seconds until local midnight, when a closed gate opens by itself."""
    now = datetime.datetime.now()
    midnight = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time())
    return (midnight - now).total_seconds()


def start_tally():
    """Start counting the cost of this thread's paid calls (fix_outcomes uses it per attempt)."""
    _tally.usd = 0.0


def tally():
    """USD this thread has spent on paid calls since start_tally(); None when not started."""
    return getattr(_tally, "usd", None)


def record(provider, model, usage, config, messages=None, result=None):
    """Add one successful call's estimated cost (USD) to today's spend and return it.

    Free calls add nothing. Token counts come from the reply's usage, else are
    estimated from the prompt and answer text."""
    # Free models and self-hosted providers cost nothing.
    if not metered(provider, model, config):
        return 0.0
    # Token counts, with cached input priced at the cache-read rate.
    usage = usage or {}
    tok_in = int(usage.get("input_tokens") or _tokens(messages))
    tok_out = int(usage.get("output_tokens") or _tokens(result))
    cached = min(int(usage.get("cached_input_tokens") or 0), tok_in)
    pol = policy()
    price = price_of(model, pol["prices"])
    usd = ((tok_in - cached) * float(price.get("input") or 0) + cached * float(price.get("cache_read") or 0)
           + tok_out * float(price.get("output") or 0)) / 1e6
    # Add it to today's ledger.
    with _LOCK:
        led = _load(_today())
        before = float(led.get("spent_usd") or 0)
        led["spent_usd"] = round(before + usd, 6)
        led["calls"] = int(led.get("calls") or 0) + 1
        gate = _gate(pol, led)
        _save()
    if getattr(_tally, "usd", None) is not None:
        _tally.usd += usd
    # Say so once, on the call that closes the gate. WARNING, not ERROR: a
    # closed gate is the gate working, not a fault for the self-log scan to file.
    if before < gate <= before + usd and config.get("AI_SPEND_GATE", True):
        logger.warning("AI spend gate closed: about $%.2f of paid AI today, gate $%.2f (fleet rule 1.10). "
                       "Paid models wait until midnight or until a person clicks Continue.", before + usd, gate)
    return usd


def reopen(usd=None):
    """A person's override: open the gate for ``usd`` (default step_usd) more, today only.

    Returns (new gate, spent so far)."""
    pol = policy()
    usd = pol["step_usd"] if usd is None else float(usd)
    with _LOCK:
        led = _load(_today())
        spent = float(led.get("spent_usd") or 0)
        gate = max(_gate(pol, led), spent) + usd
        led["gate_usd"] = round(gate, 2)
        _save()
    logger.info("AI spend gate reopened by a person: open until $%.2f today ($%.2f used)", gate, spent)
    return gate, spent

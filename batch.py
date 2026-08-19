"""
batch.py — async BATCH processing for the CLOUD tier (Anthropic Message Batches +
Google Gemini Batch). ~50% off vs synchronous, for latency-tolerant work.

Model: AppBuilder runs local-first; when it would escalate a *latency-tolerant*
task (triage / log_review / pr_summary) to the cloud, it can instead ENQUEUE the
request here, and a poller submits batches + dispatches results whenever they come
back (minutes to hours). The tight fix→review loop stays synchronous.

Everything is GATED behind config['batch_enabled'] (default OFF) — inert until
turned on. State (pending queue + submitted batches) persists to a JSON file so it
survives restarts. Import + worker registration are wrapped defensively by callers
so a problem here can never crash AppBuilder.

Wiring a task:
  1. register_handler(kind, fn)  — fn(context, text) does whatever the result means
     (e.g. post a PR comment, file/skip an issue).
  2. enqueue(kind, context, provider, model, system, prompt)  — parks the request.
The worker submits + polls + calls your handler with the returned text.

Provider support:
  - Anthropic Message Batches: fully implemented (POST /v1/messages/batches, poll,
    fetch JSONL results).
  - Google Gemini Batch: structured best-effort (inline batchGenerateContent) —
    VERIFY against your API version before relying on it.
"""
import json
import os
import threading
import time
import uuid

import requests

try:
    from main import logger, load_config, CONFIG_DIR
except Exception:  # pragma: no cover - fallback so import never hard-fails
    import logging
    logger = logging.getLogger("batch")
    def load_config():  # type: ignore
        return {}
    CONFIG_DIR = os.getenv("AB_CONFIG_DIR", "/etc/ab")

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
GOOGLE_BASE = "https://generativelanguage.googleapis.com"

_STATE_FILE = os.path.join(CONFIG_DIR or "/etc/ab", "batch_state.json")
_LOCK = threading.Lock()
_HANDLERS = {}   # kind -> callable(context: dict, text: str)


# ── result handlers ──────────────────────────────────────────────────────────
def register_handler(kind, fn):
    """Register a result handler for a task kind. fn(context, text)."""
    _HANDLERS[kind] = fn


# ── persistent state ─────────────────────────────────────────────────────────
def _load_state():
    try:
        with open(_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"pending": [], "submitted": {}}


def _save_state(st):
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:  # noqa: BLE001
        logger.error(f"batch: could not persist state: {e}")


# ── enqueue ──────────────────────────────────────────────────────────────────
def enqueue(kind, context, provider, model, system, prompt):
    """Park a cloud LLM request for batch processing. Returns the custom_id.

    context is an arbitrary JSON-serializable dict handed back to the handler.
    """
    cid = "req_" + uuid.uuid4().hex[:16]
    item = {
        "custom_id": cid, "kind": kind, "context": context,
        "provider": (provider or "").lower().strip(), "model": model,
        "system": system or "", "prompt": prompt or "", "queued_at": int(time.time()),
    }
    with _LOCK:
        st = _load_state()
        st["pending"].append(item)
        _save_state(st)
    logger.info(f"batch: queued {kind} req {cid} for {provider}/{model}")
    return cid


# ── Anthropic Message Batches ────────────────────────────────────────────────
def _submit_anthropic(items, key):
    reqs = [{
        "custom_id": it["custom_id"],
        "params": {
            "model": it["model"],
            "max_tokens": 4096,
            "system": it["system"],
            "messages": [{"role": "user", "content": it["prompt"]}],
        },
    } for it in items]
    r = requests.post(
        f"{ANTHROPIC_BASE}/messages/batches",
        headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"},
        json={"requests": reqs}, timeout=60)
    r.raise_for_status()
    return r.json().get("id")


def _poll_anthropic(batch_id, key):
    """Return {custom_id: text} if the batch has ended, else None."""
    hdr = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    r = requests.get(f"{ANTHROPIC_BASE}/messages/batches/{batch_id}", headers=hdr, timeout=30)
    r.raise_for_status()
    meta = r.json()
    if meta.get("processing_status") != "ended":
        return None
    results_url = meta.get("results_url")
    if not results_url:
        return {}
    rr = requests.get(results_url, headers=hdr, timeout=120)
    rr.raise_for_status()
    out = {}
    for line in rr.text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            cid = obj.get("custom_id")
            res = obj.get("result") or {}
            if res.get("type") == "succeeded":
                blocks = (res.get("message") or {}).get("content") or []
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                out[cid] = text
            else:
                out[cid] = ""  # errored/expired/canceled → empty (handler decides)
        except Exception:
            continue
    return out


# ── Google Gemini Batch (structured best-effort — VERIFY) ────────────────────
def _submit_gemini(items, key, model):
    # Inline batch: POST {model}:batchGenerateContent with a list of requests.
    reqs = [{
        "request": {
            "contents": [{"role": "user", "parts": [{"text": it["prompt"]}]}],
            "systemInstruction": {"parts": [{"text": it["system"]}]} if it["system"] else None,
        },
        "metadata": {"custom_id": it["custom_id"]},
    } for it in items]
    r = requests.post(
        f"{GOOGLE_BASE}/v1beta/models/{model}:batchGenerateContent?key={key}",
        json={"batch": {"inlinedRequests": reqs}}, timeout=60)
    r.raise_for_status()
    return r.json().get("name")  # operation/batch name


def _poll_gemini(op_name, key):
    r = requests.get(f"{GOOGLE_BASE}/v1beta/{op_name}?key={key}", timeout=30)
    r.raise_for_status()
    meta = r.json()
    if not meta.get("done"):
        return None
    out = {}
    for resp in ((meta.get("response") or {}).get("inlinedResponses") or []):
        cid = (resp.get("metadata") or {}).get("custom_id")
        cands = (resp.get("response") or {}).get("candidates") or []
        text = ""
        if cands:
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
        out[cid] = text
    return out


# ── flush (form + submit batches) ────────────────────────────────────────────
def _provider_key(provider, config):
    for n in (1, 2, 3, 4):
        p = (config.get(f"LLM_PROVIDER_{n}") or "").lower().strip()
        if p == provider:
            return config.get(f"LLM_API_KEY_{n}") or ""
    return ""


def flush(config):
    """Form + submit a batch per provider when the queue is big enough or old enough."""
    bmax = int(config.get("batch_max", 20) or 20)
    bwait = int(config.get("batch_max_wait_s", 300) or 300)
    with _LOCK:
        st = _load_state()
        pending = st.get("pending", [])
        if not pending:
            return
        oldest = min(p.get("queued_at", 0) for p in pending)
        if len(pending) < bmax and (time.time() - oldest) < bwait:
            return  # not full and not old enough
        # group by provider
        by_prov = {}
        for it in pending:
            by_prov.setdefault(it["provider"], []).append(it)
        remaining = []
        for prov, items in by_prov.items():
            key = _provider_key(prov, config)
            if not key:
                logger.warning(f"batch: no API key for provider {prov}; leaving {len(items)} queued")
                remaining.extend(items)
                continue
            try:
                if prov == "anthropic":
                    bid = _submit_anthropic(items, key)
                elif prov in ("google", "gemini"):
                    bid = _submit_gemini(items, key, items[0]["model"])
                else:
                    logger.warning(f"batch: provider {prov} not batch-capable; dropping to remaining")
                    remaining.extend(items)
                    continue
                st["submitted"][bid] = {
                    "provider": prov, "model": items[0]["model"],
                    "items": {it["custom_id"]: {"kind": it["kind"], "context": it["context"]} for it in items},
                    "submitted_at": int(time.time()),
                }
                logger.info(f"batch: submitted {len(items)} {prov} req(s) as batch {bid}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"batch: submit failed for {prov}: {e}")
                remaining.extend(items)
        st["pending"] = remaining
        _save_state(st)


# ── poll submitted batches + dispatch ────────────────────────────────────────
def poll_and_dispatch(config):
    with _LOCK:
        st = _load_state()
        submitted = dict(st.get("submitted", {}))
    done_ids = []
    for bid, info in submitted.items():
        try:
            prov = info["provider"]
            key = _provider_key(prov, config)
            if not key:
                continue
            results = (_poll_anthropic(bid, key) if prov == "anthropic"
                       else _poll_gemini(bid, key) if prov in ("google", "gemini") else {})
            if results is None:
                continue  # still processing
            for cid, item in (info.get("items") or {}).items():
                text = results.get(cid, "")
                fn = _HANDLERS.get(item.get("kind"))
                if fn:
                    try:
                        fn(item.get("context") or {}, text)
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"batch: handler {item.get('kind')} failed for {cid}: {e}")
            done_ids.append(bid)
            logger.info(f"batch: dispatched results for batch {bid} ({len(info.get('items') or {})} req)")
        except Exception as e:  # noqa: BLE001
            logger.error(f"batch: poll failed for {bid}: {e}")
    if done_ids:
        with _LOCK:
            st = _load_state()
            for bid in done_ids:
                st["submitted"].pop(bid, None)
            _save_state(st)


# ── synchronous batch (block-and-wait) ───────────────────────────────────────
def run_batched(provider, model, system, prompt, config, max_wait=None):
    """Submit a single-request batch and BLOCK-poll until it returns (or times out).

    Returns the text, or None on failure/timeout so the caller can fall back to a
    normal synchronous call. ~50% cheaper than sync — and slow (that's the trade).
    Used by call_llm when batch_enabled for cloud, non-streaming, non-tool calls.
    """
    provider = (provider or "").lower().strip()
    key = _provider_key(provider, config)
    if not key or not model:
        return None
    cid = "sync_" + uuid.uuid4().hex[:12]
    item = {"custom_id": cid, "model": model, "system": system or "", "prompt": prompt or ""}
    try:
        if provider == "anthropic":
            bid = _submit_anthropic([item], key)
        elif provider in ("google", "gemini"):
            bid = _submit_gemini([item], key, model)
        else:
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"batch run_batched submit failed ({provider}): {e}")
        return None
    if not bid:
        return None
    deadline = time.time() + int(max_wait or config.get("batch_sync_max_wait_s", 3600) or 3600)
    poll = max(15, int(config.get("batch_poll_s", 60) or 60))
    logger.info(f"batch: block-waiting on {provider} batch {bid} (up to {int(deadline-time.time())}s)")
    while time.time() < deadline:
        time.sleep(poll)
        try:
            res = _poll_anthropic(bid, key) if provider == "anthropic" else _poll_gemini(bid, key)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"batch run_batched poll failed: {e}")
            continue
        if res is not None:
            return res.get(cid, "") or ""
    logger.warning(f"batch run_batched timed out for {provider} batch {bid}")
    return None


# ── worker ───────────────────────────────────────────────────────────────────
def batch_worker():
    """Periodic flush + poll. Off unless batch_enabled. Best-effort; never dies."""
    while True:
        try:
            cfg = load_config()
            if cfg.get("batch_enabled", False):
                flush(cfg)
                poll_and_dispatch(cfg)
            interval = int(cfg.get("batch_poll_s", 60) or 60)
        except Exception as e:  # noqa: BLE001
            logger.error(f"batch_worker error: {e}")
            interval = 60
        time.sleep(max(15, interval))

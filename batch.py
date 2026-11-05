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
# json stores queue state and parses results; os builds paths and swaps files atomically;
# threading guards the state file; time stamps and paces polling; uuid makes request ids.
import json
import os
import threading
import time
import uuid

# HTTP client used to call the Anthropic and Google batch APIs.
import requests

# Use AppBuilder's logger/config when running inside the service; fall back to standalone
# stand-ins so this module can still be imported on its own (e.g. in tests).
try:
    from main import logger, load_config, CONFIG_DIR
except Exception:  # pragma: no cover - fallback so import never hard-fails
    import logging
    logger = logging.getLogger("batch")
    def load_config():  # type: ignore
        """Stand-in config loader used when main cannot be imported; returns no settings."""
        return {}
    CONFIG_DIR = os.getenv("AB_CONFIG_DIR", "/etc/ab")

# Anthropic API base URL and the API version header value it requires.
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
# Google Generative Language API base URL (Gemini).
GOOGLE_BASE = "https://generativelanguage.googleapis.com"

# File holding the pending queue and submitted batches, so they survive restarts.
_STATE_FILE = os.path.join(CONFIG_DIR or "/etc/ab", "batch_state.json")
# Serialises reads/writes of the state file across threads.
_LOCK = threading.Lock()
_HANDLERS = {}   # kind -> callable(context: dict, text: str)


# ── result handlers ──────────────────────────────────────────────────────────
def register_handler(kind, fn):
    """Register a result handler for a task kind. fn(context, text)."""
    # Remember which function handles results for this kind of task.
    _HANDLERS[kind] = fn


# ── persistent state ─────────────────────────────────────────────────────────
def _load_state():
    """Load the persisted queue/batch state, or an empty state if the file is missing/bad."""
    # Read the JSON state file.
    try:
        with open(_STATE_FILE, "r") as f:
            return json.load(f)
    # No file yet or unreadable: start with an empty queue and no submitted batches.
    except Exception:
        return {"pending": [], "submitted": {}}


def _save_state(st):
    """Save the queue/batch state atomically; failures are logged, not raised."""
    # Write to a temp file then rename, so a crash never leaves a half-written file.
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, _STATE_FILE)
    # Persisting is best-effort; log the problem.
    except Exception as e:  # noqa: BLE001
        logger.error(f"batch: could not persist state: {e}")


# ── enqueue ──────────────────────────────────────────────────────────────────
def enqueue(kind, context, provider, model, system, prompt):
    """Park a cloud LLM request for batch processing. Returns the custom_id.

    context is an arbitrary JSON-serializable dict handed back to the handler.
    """
    # Unique id that lets us match the batch result back to this request.
    cid = "req_" + uuid.uuid4().hex[:16]
    # Everything needed to submit the request later and to dispatch its result.
    item = {
        "custom_id": cid, "kind": kind, "context": context,
        "provider": (provider or "").lower().strip(), "model": model,
        "system": system or "", "prompt": prompt or "", "queued_at": int(time.time()),
    }
    # Append to the persisted queue under the lock.
    with _LOCK:
        st = _load_state()
        st["pending"].append(item)
        _save_state(st)
    logger.info(f"batch: queued {kind} req {cid} for {provider}/{model}")
    # Return the id so callers can correlate the result.
    return cid


# ── Anthropic Message Batches ────────────────────────────────────────────────
def _submit_anthropic(items, key):
    """Submit items as one Anthropic Message Batch; returns the batch id."""
    # One batch request per item, carrying its custom_id and normal Messages API params.
    reqs = [{
        "custom_id": it["custom_id"],
        "params": {
            "model": it["model"],
            "max_tokens": 4096,
            "system": it["system"],
            "messages": [{"role": "user", "content": it["prompt"]}],
        },
    } for it in items]
    # POST the batch with the API key and version headers.
    r = requests.post(
        f"{ANTHROPIC_BASE}/messages/batches",
        headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION,
                 "content-type": "application/json"},
        json={"requests": reqs}, timeout=60)
    # Raise on HTTP errors, then return the new batch's id.
    r.raise_for_status()
    return r.json().get("id")


def _poll_anthropic(batch_id, key):
    """Return {custom_id: text} if the batch has ended, else None."""
    # Auth headers reused for both the status and results requests.
    hdr = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    # Fetch the batch status.
    r = requests.get(f"{ANTHROPIC_BASE}/messages/batches/{batch_id}", headers=hdr, timeout=30)
    r.raise_for_status()
    meta = r.json()
    # Not finished yet: tell the caller to keep waiting.
    if meta.get("processing_status") != "ended":
        return None
    # A finished batch without a results URL has nothing to report.
    results_url = meta.get("results_url")
    if not results_url:
        return {}
    # Download the results file (JSON Lines: one result per line).
    rr = requests.get(results_url, headers=hdr, timeout=120)
    rr.raise_for_status()
    # Collect custom_id -> text for every result line.
    out = {}
    for line in rr.text.splitlines():
        # Skip blank lines.
        if not line.strip():
            continue
        # Parse a line; malformed lines are skipped.
        try:
            obj = json.loads(line)
            cid = obj.get("custom_id")
            res = obj.get("result") or {}
            # Successful result: join all text blocks of the reply.
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
    """Submit items as one inline Gemini batch for model; returns the operation name."""
    # Inline batch: POST {model}:batchGenerateContent with a list of requests.
    reqs = [{
        "request": {
            "contents": [{"role": "user", "parts": [{"text": it["prompt"]}]}],
            "systemInstruction": {"parts": [{"text": it["system"]}]} if it["system"] else None,
        },
        "metadata": {"custom_id": it["custom_id"]},
    } for it in items]
    # POST the batch; the API key goes in the query string as Google expects.
    r = requests.post(
        f"{GOOGLE_BASE}/v1beta/models/{model}:batchGenerateContent?key={key}",
        json={"batch": {"inlinedRequests": reqs}}, timeout=60)
    # Raise on HTTP errors.
    r.raise_for_status()
    return r.json().get("name")  # operation/batch name


def _poll_gemini(op_name, key):
    """Return {custom_id: text} once the Gemini batch operation is done, else None."""
    # Fetch the operation status.
    r = requests.get(f"{GOOGLE_BASE}/v1beta/{op_name}?key={key}", timeout=30)
    r.raise_for_status()
    meta = r.json()
    # Not finished yet: tell the caller to keep waiting.
    if not meta.get("done"):
        return None
    # Collect custom_id -> text from each inline response.
    out = {}
    for resp in ((meta.get("response") or {}).get("inlinedResponses") or []):
        # Find the request id and the candidate answers.
        cid = (resp.get("metadata") or {}).get("custom_id")
        cands = (resp.get("response") or {}).get("candidates") or []
        # Take the text of the first candidate, if there is one.
        text = ""
        if cands:
            parts = (cands[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts)
        # Record the text (empty when the model returned no candidate).
        out[cid] = text
    return out


# ── flush (form + submit batches) ────────────────────────────────────────────
def _provider_key(provider, config):
    """Return the API key configured for provider in any of the four provider slots, or ""."""
    # Check each provider slot for a matching provider name.
    for n in (1, 2, 3, 4):
        p = (config.get(f"LLM_PROVIDER_{n}") or "").lower().strip()
        if p == provider:
            return config.get(f"LLM_API_KEY_{n}") or ""
    # Provider is not configured.
    return ""


def flush(config):
    """Form + submit a batch per provider when the queue is big enough or old enough."""
    # Flush thresholds: queue size, and the longest a request may wait before being sent.
    bmax = int(config.get("batch_max", 20) or 20)
    bwait = int(config.get("batch_max_wait_s", 300) or 300)
    # Work on the state under the lock so enqueue/poll cannot interleave.
    with _LOCK:
        st = _load_state()
        pending = st.get("pending", [])
        # Nothing queued.
        if not pending:
            return
        # Age of the oldest queued request.
        oldest = min(p.get("queued_at", 0) for p in pending)
        if len(pending) < bmax and (time.time() - oldest) < bwait:
            return  # not full and not old enough
        # group by provider
        by_prov = {}
        for it in pending:
            by_prov.setdefault(it["provider"], []).append(it)
        # Requests that could not be submitted stay in the queue.
        remaining = []
        # Submit one batch per provider.
        for prov, items in by_prov.items():
            # Without an API key the provider cannot be called; keep its requests queued.
            key = _provider_key(prov, config)
            if not key:
                logger.warning(f"batch: no API key for provider {prov}; leaving {len(items)} queued")
                remaining.extend(items)
                continue
            # Submit; any failure keeps the items queued for a later attempt.
            try:
                if prov == "anthropic":
                    bid = _submit_anthropic(items, key)
                elif prov in ("google", "gemini"):
                    bid = _submit_gemini(items, key, items[0]["model"])
                else:
                    logger.warning(f"batch: provider {prov} not batch-capable; dropping to remaining")
                    remaining.extend(items)
                    continue
                # Remember the batch so the poller can match results to handlers.
                st["submitted"][bid] = {
                    "provider": prov, "model": items[0]["model"],
                    "items": {it["custom_id"]: {"kind": it["kind"], "context": it["context"]} for it in items},
                    "submitted_at": int(time.time()),
                }
                logger.info(f"batch: submitted {len(items)} {prov} req(s) as batch {bid}")
            # Submission failed: keep the requests queued.
            except Exception as e:  # noqa: BLE001
                logger.error(f"batch: submit failed for {prov}: {e}")
                remaining.extend(items)
        # Save what is still pending.
        st["pending"] = remaining
        _save_state(st)


# ── poll submitted batches + dispatch ────────────────────────────────────────
def poll_and_dispatch(config):
    """Poll every submitted batch and hand finished results to their registered handlers."""
    # Take a snapshot of submitted batches; polling happens outside the lock (slow HTTP).
    with _LOCK:
        st = _load_state()
        submitted = dict(st.get("submitted", {}))
    # Batches whose results have been dispatched.
    done_ids = []
    # Check each submitted batch; errors on one batch do not stop the others.
    for bid, info in submitted.items():
        try:
            # Need the provider's API key to poll.
            prov = info["provider"]
            key = _provider_key(prov, config)
            if not key:
                continue
            # Poll the right provider API.
            results = (_poll_anthropic(bid, key) if prov == "anthropic"
                       else _poll_gemini(bid, key) if prov in ("google", "gemini") else {})
            if results is None:
                continue  # still processing
            # Hand each request's text to the handler registered for its kind.
            for cid, item in (info.get("items") or {}).items():
                text = results.get(cid, "")
                fn = _HANDLERS.get(item.get("kind"))
                if fn:
                    # A failing handler is logged; other results are still delivered.
                    try:
                        fn(item.get("context") or {}, text)
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"batch: handler {item.get('kind')} failed for {cid}: {e}")
            # Mark this batch as done.
            done_ids.append(bid)
            logger.info(f"batch: dispatched results for batch {bid} ({len(info.get('items') or {})} req)")
        # Polling errors are logged; the batch is retried on the next cycle.
        except Exception as e:  # noqa: BLE001
            logger.error(f"batch: poll failed for {bid}: {e}")
    # Remove finished batches from the persisted state.
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
    # Normalise the provider name and find its key; no key or model means fall back.
    provider = (provider or "").lower().strip()
    key = _provider_key(provider, config)
    if not key or not model:
        return None
    # A single-request batch with a unique id.
    cid = "sync_" + uuid.uuid4().hex[:12]
    item = {"custom_id": cid, "model": model, "system": system or "", "prompt": prompt or ""}
    # Submit to the right provider; unsupported providers or errors mean fall back.
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
    # No batch id came back.
    if not bid:
        return None
    # Give up after the configured maximum wait (default one hour).
    deadline = time.time() + int(max_wait or config.get("batch_sync_max_wait_s", 3600) or 3600)
    # Poll interval in seconds, never faster than every 15s.
    poll = max(15, int(config.get("batch_poll_s", 60) or 60))
    logger.info(f"batch: block-waiting on {provider} batch {bid} (up to {int(deadline-time.time())}s)")
    # Sleep and poll until the batch finishes or the deadline passes.
    while time.time() < deadline:
        time.sleep(poll)
        # Poll errors are logged and retried on the next loop.
        try:
            res = _poll_anthropic(bid, key) if provider == "anthropic" else _poll_gemini(bid, key)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"batch run_batched poll failed: {e}")
            continue
        # Finished: return this request's text.
        if res is not None:
            return res.get(cid, "") or ""
    # Deadline passed without a result.
    logger.warning(f"batch run_batched timed out for {provider} batch {bid}")
    return None


# ── worker ───────────────────────────────────────────────────────────────────
def batch_worker():
    """Periodic flush + poll. Off unless batch_enabled. Best-effort; never dies."""
    # Run forever in a background thread.
    while True:
        # Reload config each cycle so toggles apply without a restart.
        try:
            cfg = load_config()
            # Only do work when batching is switched on.
            if cfg.get("batch_enabled", False):
                flush(cfg)
                poll_and_dispatch(cfg)
            # Poll interval from config.
            interval = int(cfg.get("batch_poll_s", 60) or 60)
        # Never let an error kill the worker; log it and use the default interval.
        except Exception as e:  # noqa: BLE001
            logger.error(f"batch_worker error: {e}")
            interval = 60
        # Sleep between cycles, at least 15 seconds.
        time.sleep(max(15, interval))

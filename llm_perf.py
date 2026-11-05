"""
llm_perf.py — per-model performance sample store for AppBuilder's LLM picker.

No main import: computation runs over a plain in-memory dict (ModelKey ->
sample history), with a thin JSON load/save boundary at the edges. Feeds
model_selection.select_model()'s `perf` snapshot and backs the Settings
"Model Performance" panel. The relative-exhaustion DECISION lives in
model_selection.py (it needs tier peers to compare against, which this module
has no notion of) — this module's job stops at storing samples and producing
a per-model summary (n, median tok/s, median wire latency).
"""
import json
import os
import time

# How many samples to keep, how long they count, and the file format version.
PERF_WINDOW = 50            # samples kept per model, newest last (deque-like trim)
PERF_MAX_AGE_S = 7 * 86400  # age-out horizon: a model slow 6 months ago isn't condemned forever
STORE_VERSION = 2


def _key_str(key):
    """ModelKey (provider, base_url, model) 3-tuple -> a JSON-object-safe string."""
    # Join the three parts with "|".
    provider, base_url, model = key
    return "|".join((provider or "", base_url or "", model or ""))


def _key_from_str(s):
    """Inverse of _key_str: parse "provider|base_url|model" back to a tuple, or None if malformed."""
    # Split into at most three parts; anything else is invalid.
    parts = (s or "").split("|", 2)
    if len(parts) != 3:
        return None
    return (parts[0], parts[1], parts[2])


def new_store():
    """Return a new, empty performance store."""
    return {}


def record(store, key, latency_ms_wire, tps=None, source="api", error=False, now=None):
    """Appends one observation for `key`. `latency_ms_wire` is the successful-
    attempt-only wire time (excludes 429-backoff sleeps) that model_selection
    ranks and exhausts on — required on a success. `tps` may be None (a
    provider whose usage payload didn't parse this call is still a valid
    latency sample). On error, only the error counter/last_seen advance —
    a failed call has no latency signal worth ranking on."""
    # Get or create this model's entry and update when it was last seen.
    now = time.time() if now is None else now
    entry = store.setdefault(key, {"samples": [], "last_seen": now, "errors": 0})
    entry["last_seen"] = now
    # Errors only bump the counter.
    if error:
        entry["errors"] += 1
        return
    # Add the sample and keep only the newest PERF_WINDOW.
    entry["samples"].append((now, latency_ms_wire, tps, source))
    if len(entry["samples"]) > PERF_WINDOW:
        del entry["samples"][:-PERF_WINDOW]


def _median(values):
    """Middle value of a list (upper middle for even lengths), or None if empty."""
    # No values: no median.
    if not values:
        return None
    # Sort and take the middle.
    s = sorted(values)
    return s[len(s) // 2]


def snapshot(store, now=None, max_age_s=PERF_MAX_AGE_S):
    """{ModelKey: {"n": int, "tps": float|None, "latency_ms": float|None}} —
    exactly the shape model_selection.select_model()'s `perf` param expects.
    Samples older than max_age_s are excluded at read time (no store mutation
    needed for an in-memory store to age out live between save/load cycles)."""
    # Summarise each model using only recent samples.
    now = time.time() if now is None else now
    out = {}
    for key, entry in (store or {}).items():
        fresh = [smp for smp in entry.get("samples", []) if now - smp[0] <= max_age_s]
        lat_vals = [smp[1] for smp in fresh if smp[1] is not None]
        tps_vals = [smp[2] for smp in fresh if smp[2] is not None]
        out[key] = {"n": len(fresh), "tps": _median(tps_vals), "latency_ms": _median(lat_vals)}
    return out


def load(path):
    """Reads the v2 JSON store. Any other shape (missing/mismatched version,
    the discarded v1 llm_tps.json flat format, corrupt JSON, a missing file)
    returns an empty store rather than raising — llm_tps.json's bare
    model-name keys can't be safely mapped to a ModelKey (see the migration
    plan), so silent discard-and-start-fresh is the intended behavior here,
    not a bug swallowed by accident."""
    # Read the file; a missing or corrupt file gives an empty store.
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return new_store()

    # Wrong format or version: start fresh.
    if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
        return new_store()

    # Rebuild each model's entry, skipping malformed keys and samples.
    store = {}
    for key_str, entry in (raw.get("samples") or {}).items():
        key = _key_from_str(key_str)
        if key is None or not isinstance(entry, dict):
            continue
        samples = []
        for smp in entry.get("samples", []):
            if not isinstance(smp, list) or len(smp) != 4:
                continue
            ts, lat, tps, source = smp
            samples.append((ts, lat, tps, source))
        # Keep the newest samples and the counters.
        store[key] = {
            "samples": samples[-PERF_WINDOW:],
            "last_seen": entry.get("last_seen", 0),
            "errors": entry.get("errors", 0),
        }
    return store


def save(path, store):
    """Atomic write (tmp + os.replace) so a crash mid-write never corrupts
    the store a running process is about to load() on next start."""
    # Build the JSON payload with string keys.
    payload = {
        "version": STORE_VERSION,
        "samples": {
            _key_str(key): {
                "samples": [list(smp) for smp in entry.get("samples", [])],
                "last_seen": entry.get("last_seen", 0),
                "errors": entry.get("errors", 0),
            }
            for key, entry in (store or {}).items()
        },
    }
    # Write to a temp file, then replace the real one.
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, path)

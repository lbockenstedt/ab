#!/usr/bin/env python3
"""Self-test for llm_perf.py — the per-model performance sample store.

Run:  python3 bugfixer/test_llm_perf_store.py

Standalone: imports only llm_perf (stdlib json/os/time only). No app/main init.
"""
import os
import sys
import tempfile

import llm_perf as perf


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


KEY = ("ollama", "http://localhost:11434", "qwen2.5-coder:14b")
KEY2 = ("anthropic", "https://api.anthropic.com", "claude-sonnet-5")


def main():
    print("Running bugfixer llm_perf self-test...")
    ok = True

    # --- record + snapshot basics --------------------------------------------

    store = perf.new_store()
    perf.record(store, KEY, latency_ms_wire=100, tps=50, now=1000.0)
    snap = perf.snapshot(store, now=1000.0)
    ok &= _check("one recorded sample -> n=1 with that latency/tps",
                snap[KEY]["n"] == 1 and snap[KEY]["latency_ms"] == 100 and snap[KEY]["tps"] == 50)

    ok &= _check("a key with no samples at all is simply absent from the snapshot",
                KEY2 not in snap)

    # --- median summarization ---------------------------------------------

    store2 = perf.new_store()
    for lat, tps in [(100, 50), (200, 40), (300, 10)]:
        perf.record(store2, KEY, latency_ms_wire=lat, tps=tps, now=1000.0)
    snap2 = perf.snapshot(store2, now=1000.0)
    ok &= _check("latency_ms/tps are the MEDIAN of recorded samples, not mean/last",
                snap2[KEY]["latency_ms"] == 200 and snap2[KEY]["tps"] == 40)
    ok &= _check("n reflects the sample count", snap2[KEY]["n"] == 3)

    # --- tps=None is a valid latency-only sample -----------------------------

    store3 = perf.new_store()
    perf.record(store3, KEY, latency_ms_wire=150, tps=None, now=1000.0)
    snap3 = perf.snapshot(store3, now=1000.0)
    ok &= _check("a sample with tps=None still counts toward n and contributes latency",
                snap3[KEY]["n"] == 1 and snap3[KEY]["latency_ms"] == 150 and snap3[KEY]["tps"] is None)

    # --- error samples don't pollute the perf signal -------------------------

    store4 = perf.new_store()
    perf.record(store4, KEY, latency_ms_wire=100, tps=50, now=1000.0)
    perf.record(store4, KEY, latency_ms_wire=None, error=True, now=1001.0)
    snap4 = perf.snapshot(store4, now=1001.0)
    ok &= _check("an error record does not add a sample (n stays at the successful-call count)",
                snap4[KEY]["n"] == 1)
    ok &= _check("errors are counted on the raw store entry", store4[KEY]["errors"] == 1)

    # --- window trimming (PERF_WINDOW) ---------------------------------------

    store5 = perf.new_store()
    for i in range(perf.PERF_WINDOW + 10):
        perf.record(store5, KEY, latency_ms_wire=float(i), tps=1.0, now=1000.0 + i)
    ok &= _check(f"the sample list is trimmed to PERF_WINDOW ({perf.PERF_WINDOW})",
                len(store5[KEY]["samples"]) == perf.PERF_WINDOW)
    ok &= _check("trimming drops the OLDEST samples, keeping the newest",
                store5[KEY]["samples"][0][1] == 10.0 and store5[KEY]["samples"][-1][1] == float(perf.PERF_WINDOW + 9))

    # --- age-out at snapshot time ---------------------------------------------

    store6 = perf.new_store()
    perf.record(store6, KEY, latency_ms_wire=999, tps=1, now=0.0)  # ancient
    perf.record(store6, KEY, latency_ms_wire=100, tps=50, now=1_000_000.0)  # recent
    snap6 = perf.snapshot(store6, now=1_000_000.0, max_age_s=perf.PERF_MAX_AGE_S)
    ok &= _check("a sample older than max_age_s is excluded from the snapshot",
                snap6[KEY]["n"] == 1 and snap6[KEY]["latency_ms"] == 100)

    store7 = perf.new_store()
    perf.record(store7, KEY, latency_ms_wire=100, tps=50, now=0.0)
    snap7 = perf.snapshot(store7, now=perf.PERF_MAX_AGE_S + 1, max_age_s=perf.PERF_MAX_AGE_S)
    ok &= _check("age-out with everything stale -> n=0, tps/latency None (not a crash, not a stale value)",
                snap7[KEY]["n"] == 0 and snap7[KEY]["tps"] is None and snap7[KEY]["latency_ms"] is None)

    # --- multiple distinct ModelKeys stay isolated -----------------------------

    store8 = perf.new_store()
    perf.record(store8, KEY, latency_ms_wire=100, tps=50, now=1000.0)
    perf.record(store8, KEY2, latency_ms_wire=9000, tps=5, now=1000.0)
    snap8 = perf.snapshot(store8, now=1000.0)
    ok &= _check("two different ModelKeys never mix samples",
                snap8[KEY]["latency_ms"] == 100 and snap8[KEY2]["latency_ms"] == 9000)

    # --- save/load round-trip (v2, tuple-keyed <-> JSON) -----------------------

    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "llm_perf.json")

        store9 = perf.new_store()
        perf.record(store9, KEY, latency_ms_wire=100, tps=50, source="server", now=1000.0)
        perf.record(store9, KEY2, latency_ms_wire=9000, tps=5, source="api", now=1000.0)
        perf.save(path, store9)
        loaded = perf.load(path)

        ok &= _check("round-trip preserves both ModelKeys as proper 3-tuples",
                    KEY in loaded and KEY2 in loaded)
        loaded_snap = perf.snapshot(loaded, now=1000.0)
        ok &= _check("round-trip preserves sample values exactly",
                    loaded_snap[KEY]["latency_ms"] == 100 and loaded_snap[KEY]["tps"] == 50
                    and loaded_snap[KEY2]["latency_ms"] == 9000)
        ok &= _check("round-trip preserves the source tag",
                    loaded[KEY]["samples"][0][3] == "server")
        ok &= _check("save() writes atomically (no leftover .tmp file after a successful save)",
                    not os.path.exists(path + ".tmp"))

        # loading twice is idempotent (a second load doesn't double up samples)
        loaded_again = perf.load(path)
        ok &= _check("load() is idempotent -- reading the same file twice yields the same sample count",
                    len(loaded_again[KEY]["samples"]) == len(loaded[KEY]["samples"]))

    # --- malformed / legacy input is discarded, never crashes -----------------

    with tempfile.TemporaryDirectory() as tmpdir:
        missing_path = os.path.join(tmpdir, "does-not-exist.json")
        ok &= _check("load() on a missing file returns an empty store, no exception",
                    perf.load(missing_path) == {})

        garbage_path = os.path.join(tmpdir, "garbage.json")
        with open(garbage_path, "w") as f:
            f.write("{not valid json")
        ok &= _check("load() on corrupt JSON returns an empty store, no exception",
                    perf.load(garbage_path) == {})

        v1_path = os.path.join(tmpdir, "v1.json")
        with open(v1_path, "w") as f:
            f.write('{"qwen2.5-coder:14b": [12.4, 11.9, 13.0]}')  # old flat llm_tps.json shape
        ok &= _check("load() discards the old v1 flat llm_tps.json shape rather than misinterpreting it",
                    perf.load(v1_path) == {})

        wrong_version_path = os.path.join(tmpdir, "wrongver.json")
        with open(wrong_version_path, "w") as f:
            f.write('{"version": 1, "samples": {}}')
        ok &= _check("load() rejects a version mismatch even with an otherwise-valid v2 shape",
                    perf.load(wrong_version_path) == {})

        malformed_samples_path = os.path.join(tmpdir, "malformed.json")
        with open(malformed_samples_path, "w") as f:
            f.write('{"version": 2, "samples": {"bad-key-no-pipes": {"samples": [[1,2,3,"x"]]}, '
                    '"ollama|http://x|m": {"samples": ["not-a-list"], "last_seen": 1, "errors": 0}}}')
        loaded_malformed = perf.load(malformed_samples_path)
        ok &= _check("a key with no '|' separators is skipped rather than crashing",
                    len(loaded_malformed) == 1)
        ok &= _check("a malformed individual sample entry is skipped, the key still loads with 0 samples",
                    loaded_malformed[("ollama", "http://x", "m")]["samples"] == [])

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())

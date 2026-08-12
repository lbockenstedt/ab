#!/usr/bin/env python3
"""Self-test for call_llm's per-category / per-slot concurrency model.

Run:  python3 bugfixer/test_llm_concurrency.py

llm_client.py cannot be imported directly (it imports main, which circularly
imports app_state, which imports main again before it finishes initializing —
plus a real FastAPI app spin-up), so this extracts the SOURCE of the pure
routing functions via ast and execs them with stubbed leaf dependencies
(_get_provider_config, _call_provider, etc). This exercises the real
control-flow text of call_llm, not a reimplementation of it.

Regression guard for the switch from ONE global LLM_MAX_CONCURRENT semaphore
to a per-CATEGORY (CODE/LOG/REVIEW) one plus per-SLOT exclusivity: a slot can
run only one job at a time; a busy slot is skipped in favor of the next one
in its pool; if every slot in the pool is busy, the call waits and re-scans
instead of failing. Caught a real bug during development: a pool with one
real (busy) slot and several UNCONFIGURED ones was treated as "exhausted"
instead of "the one real slot is just busy" — not_configured must not count
as a genuine attempt for purposes of the busy-wait decision.
"""
import ast
import json
import random
import threading
import time


def _load_ns(config, get_provider_config, call_provider):
    path = "llm_client.py"
    src = open(path).read()
    tree = ast.parse(src)
    want_funcs = {"call_llm", "_slots_for_task", "_pool_category_name",
                  "_get_category_semaphore", "_reset_llm_semaphore",
                  "_record_provider_result"}
    want_assign = {"_CODE_SLOTS", "_LOG_SLOTS", "_REVIEW_SLOTS", "_ALL_SLOTS",
                   "_LOG_TASK_KINDS", "_REVIEW_TASK_KINDS",
                   "_SLOT_LOCKS", "_CATEGORY_SEMAPHORES", "_CATEGORY_SEM_LOCK",
                   "_PROVIDER_CREDIT_CB", "_PROVIDER_CREDIT_CB_LOCK"}
    segs = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_funcs:
            segs.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") in want_assign:
                    segs.append(ast.get_source_segment(src, node))

    class _NoLog:
        def __getattr__(self, _):
            return lambda *a, **k: None

    class LLMCreditExhausted(Exception):
        pass

    ns = {
        "threading": threading, "time": time, "random": random, "json": json,
        "logger": _NoLog(), "load_config": lambda: config,
        "_get_provider_config": get_provider_config,
        "_provider_configured": lambda provider, key, model: bool(model),
        "_provider_is_nokey": lambda provider: True,
        "_provider_credit_cb_remaining": lambda n, provider=None: 0,
        "_provider_rate_limit_wait": lambda n, rpm, provider_name: None,
        "_get_provider_rpm": lambda n, config: 0,
        "_call_provider": call_provider,
        "LLMCreditExhausted": LLMCreditExhausted,
        "main": type("M", (), {"state": {"provider_last_result": {}}})(),
    }
    exec("\n\n".join(segs), ns)
    return ns


def _check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return cond


def _all_configured(n, config):
    return ("ollama", "key", f"model-{n}", f"http://host{n}")


def _slow_provider(delay, log, log_lock):
    def cp(provider, model, key, url, messages, tools, effective_stream, task_id, config, **kw):
        with log_lock:
            log.append(("start", model, time.time()))
        time.sleep(delay)
        with log_lock:
            log.append(("end", model, time.time()))
        return f"OK from {model}"
    return cp


def main():
    ok = True

    # 1. Two configured slots, two concurrent calls -> parallel, distinct slots.
    log1, lock1 = [], threading.Lock()
    ns1 = _load_ns({"LLM_MAX_CONCURRENT": "2", "LLM_TIMEOUT": "900"},
                    _all_configured, _slow_provider(0.3, log1, lock1))
    call_llm1 = ns1["call_llm"]
    results = [None, None]
    def w1(i):
        results[i] = call_llm1("hi", task_kind=None, force_provider=None)
    t0 = time.time()
    threads = [threading.Thread(target=w1, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    ok &= _check("two free slots run in parallel on DIFFERENT slots",
                 results[0] != results[1] and elapsed < 0.5)

    # 2. One real (busy) slot + unconfigured others -> second call queues and
    #    retries instead of failing (the bug this test suite guards against).
    def gpc2(n, config):
        if n == 1:
            return ("ollama", "key", "model-1", "http://host1")
        return ("ollama", None, "", "")
    log2, lock2 = [], threading.Lock()
    ns2 = _load_ns({"LLM_MAX_CONCURRENT": "3", "LLM_TIMEOUT": "900"},
                    gpc2, _slow_provider(0.4, log2, lock2))
    call_llm2 = ns2["call_llm"]
    results2, errors2 = [None, None], [None, None]
    def w2(i):
        try:
            results2[i] = call_llm2("hi", task_kind=None, force_provider=None)
        except Exception as e:
            errors2[i] = str(e)
    t0 = time.time()
    threads = [threading.Thread(target=w2, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    starts = sorted(t for k, m, t in log2 if k == "start")
    ends = sorted(t for k, m, t in log2 if k == "end")
    no_overlap = len(starts) == 2 and starts[1] >= ends[0] - 0.05
    ok &= _check("1 real slot + 3 unconfigured: second call QUEUES on the busy "
                 "slot instead of failing",
                 errors2 == [None, None] and results2 == ["OK from model-1"] * 2
                 and elapsed >= 0.7 and no_overlap)

    # 3. Nothing configured at all -> fails immediately, no hang.
    def gpc3(n, config):
        return ("ollama", None, "", "")
    def cp3(*a, **kw):
        raise AssertionError("should never be called — nothing is configured")
    ns3 = _load_ns({"LLM_MAX_CONCURRENT": "2", "LLM_TIMEOUT": "900"}, gpc3, cp3)
    call_llm3 = ns3["call_llm"]
    t0 = time.time()
    raised = None
    try:
        call_llm3("hi", task_kind=None, force_provider=None)
    except Exception as e:
        raised = str(e)
    elapsed = time.time() - t0
    ok &= _check("entirely unconfigured pool fails immediately (no wait-loop hang)",
                 raised is not None and "All configured LLM providers failed" in raised
                 and elapsed < 1.0)

    # 4. Category isolation: a busy CODE slot must not block a REVIEW call.
    log4, lock4 = [], threading.Lock()
    ns4 = _load_ns({"LLM_MAX_CONCURRENT": "1", "LLM_TIMEOUT": "900"},
                    _all_configured, _slow_provider(0.3, log4, lock4))
    call_llm4 = ns4["call_llm"]
    results4 = {}
    def w4(key, task_kind):
        results4[key] = call_llm4("hi", task_kind=task_kind, force_provider=None)
    t0 = time.time()
    tA = threading.Thread(target=w4, args=("code", "build"))
    tB = threading.Thread(target=w4, args=("review", "review"))
    tA.start(); tB.start()
    tA.join(); tB.join()
    elapsed = time.time() - t0
    ok &= _check("CODE and REVIEW pools run independently (not serialized "
                 "against each other)",
                 elapsed < 0.5 and bool(results4.get("code")) and bool(results4.get("review")))

    # 5. The category CAP itself is enforced even with more free slots than the
    #    cap — LLM_MAX_CONCURRENT=1 with 4 configured slots still serializes.
    log5, lock5 = [], threading.Lock()
    ns5 = _load_ns({"LLM_MAX_CONCURRENT": "1", "LLM_TIMEOUT": "900"},
                    _all_configured, _slow_provider(0.3, log5, lock5))
    call_llm5 = ns5["call_llm"]
    results5 = [None, None, None]
    def w5(i):
        results5[i] = call_llm5("hi", task_kind=None, force_provider=None)
    t0 = time.time()
    threads = [threading.Thread(target=w5, args=(i,)) for i in range(3)]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    starts = sorted(t for k, m, t in log5 if k == "start")
    ends = sorted(t for k, m, t in log5 if k == "end")
    no_overlap5 = all(starts[i] >= ends[i - 1] - 0.05 for i in range(1, len(starts)))
    ok &= _check("LLM_MAX_CONCURRENT=1 serializes 3 jobs despite 4 free slots",
                 all(results5) and elapsed >= 0.8 and no_overlap5)

    # 6. force_provider (pinned, e.g. a cross-check reviewer) queues on its OWN
    #    slot when busy — it has no failover partner to try instead.
    log6, lock6 = [], threading.Lock()
    ns6 = _load_ns({"LLM_MAX_CONCURRENT": "4", "LLM_TIMEOUT": "900"},
                    _all_configured, _slow_provider(0.3, log6, lock6))
    call_llm6 = ns6["call_llm"]
    results6 = [None, None]
    def w6(i):
        results6[i] = call_llm6("hi", task_kind=None, force_provider=1)
    t0 = time.time()
    threads = [threading.Thread(target=w6, args=(i,)) for i in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0
    starts = sorted(t for k, m, t in log6 if k == "start")
    ends = sorted(t for k, m, t in log6 if k == "end")
    no_overlap6 = len(starts) == 2 and starts[1] >= ends[0] - 0.05
    ok &= _check("force_provider queues on its pinned slot instead of failing over",
                 results6 == ["OK from model-1", "OK from model-1"]
                 and elapsed >= 0.5 and no_overlap6)

    print()
    if ok:
        print("ALL CASES PASSED")
        return 0
    print("ONE OR MORE CASES FAILED")
    return 1


if __name__ == "__main__":
    print("Running call_llm concurrency self-test...")
    import sys
    sys.exit(main())

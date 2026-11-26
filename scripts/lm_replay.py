#!/usr/bin/env python3
"""Replay old AB prompts through the LM-hosted local models and score them.

Run it ON the hub host (for example through ``az vm run-command``): it calls the
loopback ``/admin/ops/llm-complete`` lever, so no cloud model is used.

Input: a JSONL file, one case per line:
  {"id": "pr-123", "task": "review", "prompt": "...", "expected_verdict": "Reject"}
``expected_verdict`` (optional) is the verdict the cloud reviewers gave; the
report counts how often the local model agrees. Output: one JSON line per case
plus a summary line.
"""
import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def ask(args, case):
    """POST one case to the hub lever and return the reply dict."""
    body = {"tenant": args.tenant, "task": case["task"], "prompt": case["prompt"],
            "format": case.get("format", "text")}
    req = urllib.request.Request(
        args.url.rstrip("/") + "/admin/ops/llm-complete", json.dumps(body).encode(),
        {"x-lm-admin-token": args.token, "content-type": "application/json"})
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # loopback to the hub's own cert
    with urllib.request.urlopen(req, timeout=args.timeout, context=ctx) as resp:
        return json.load(resp)


def verdict_of(text):
    """Pull the reviewer verdict out of a reply, or None."""
    m = re.search(r'"verdict"\s*:\s*"(Approve|Reject)"', text or "", re.I)
    return m.group(1).capitalize() if m else None


def main():
    """Replay every case and print per-case results and an agreement summary."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cases")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--url", default="https://127.0.0.1")
    ap.add_argument("--timeout", type=float, default=960)
    ap.add_argument("--concurrency", type=int, default=1, help="cases in flight at once")
    args = ap.parse_args()
    cases = [json.loads(line) for line in open(args.cases) if line.strip()]
    total, agree, scored = len(cases), 0, 0

    def run(case):
        """Replay one case; return its result row."""
        try:
            out = ask(args, case)
        except Exception as exc:  # noqa: BLE001 - report and keep replaying
            return {"id": case.get("id"), "error": str(exc)}
        return {"id": case.get("id"), "status": out.get("status"), "verdict": verdict_of(out.get("text")),
                "expected": case.get("expected_verdict"), "model": out.get("model"),
                "elapsed_s": out.get("elapsed_s")}

    # Several cases in flight at once spread across the pool's workers.
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for row in pool.map(run, cases):
            if row.get("expected") and row.get("verdict"):
                scored += 1
                agree += row["verdict"].lower() == row["expected"].lower()
            print(json.dumps(row), flush=True)
    print(json.dumps({"summary": {"cases": total, "scored": scored, "agreed": agree}}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

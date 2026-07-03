"""Parallel-tabs operational canary (run against live Chrome + ChatGPT).

Thin validation harness for the `parallel_tabs=true` mode merged in PR #33.
Boots NO processes by default — it expects two REST workers already running on
distinct local ports against ONE shared Chrome/CDP port, each with
`parallel_tabs=true` (and `tab_mode=owned`). It fires concurrent requests and
reports whether different-tab sends proceed concurrently and same-tab sends
serialize, then emits a JSON summary.

This is an operational acceptance tool, NOT a re-proof of the invariants — the
unit/integration suites (test_lock_resolver, test_parallel_tabs_pr4,
test_chrome_lifecycle) are authoritative for the locking/serialization logic.
Live timing against real ChatGPT is noisy (backend latency, streaming,
rate limits, DOM readiness all confound it), so timing here is advisory; the
CDP `/json/list` snapshots + log excerpts are the primary evidence.

Usage (default — against already-started endpoints)::

    # Terminal 1
    W2A_PARALLEL_TABS=1 chatgpt-web2api --port 8081 --cdp-port 9222
    # Terminal 2
    W2A_PARALLEL_TABS=1 chatgpt-web2api --port 8082 --cdp-port 9222
    # Terminal 3 (this script)
    python scripts/parallel_canary.py --ports 8081,8082 --cdp-port 9222

Args:
  --ports      Comma-separated REST ports of the two workers (required).
  --cdp-port   Shared Chrome CDP port (default 9222).
  --model      Model slug to send (default auto).
  --prompt     Prompt text (default a timestamped marker).
  --timeout    Per-request timeout seconds (default 120).
  --json-out   Path to write the JSON summary (default ./parallel-canary.json).

Exit code: 0 if both workers returned OpenAI-compatible 200s and the CDP
snapshot shows >=2 distinct chatgpt.com page targets; 1 otherwise. Timing/
serialization observations are reported but do not fail the exit code (live
timing is noisy — see module docstring).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def _post_chat(port: int, model: str, prompt: str, timeout: float) -> dict:
    """POST /v1/chat/completions (non-streaming) to one worker. Returns a
    result dict with port, status, elapsed, and either content or error."""
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        return {
            "port": port,
            "status": resp.status,
            "elapsed_s": round(time.monotonic() - started, 2),
            "content_preview": (payload.get("choices", [{}])[0]
                                 .get("message", {})
                                 .get("content", ""))[:120],
            "ok": resp.status == 200,
        }
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except Exception:
            pass
        return {
            "port": port,
            "status": e.code,
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": err_body,
            "ok": False,
        }
    except Exception as e:
        return {
            "port": port,
            "status": None,
            "elapsed_s": round(time.monotonic() - started, 2),
            "error": f"{type(e).__name__}: {e}",
            "ok": False,
        }


def _cdp_targets(cdp_port: int) -> list[dict]:
    """Snapshot /json/list page targets (best-effort; non-fatal)."""
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json/list"),
            timeout=5,
        ) as resp:
            targets = json.loads(resp.read())
        return [
            {
                "type": t.get("type"),
                "url": (t.get("url") or "")[:100],
                "title": (t.get("title") or "")[:60],
            }
            for t in targets
            if t.get("type") == "page"
        ]
    except Exception as e:
        return [{"error": f"{type(e).__name__}: {e}"}]


def _health(port: int) -> dict:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/health"), timeout=5
        ) as resp:
            return {"port": port, "status": resp.status, "body": json.loads(resp.read())}
    except Exception as e:
        return {"port": port, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ports", required=True, help="Comma-separated REST ports (2+)")
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--model", default="auto")
    ap.add_argument(
        "--prompt", default=f"parallel-canary-{int(time.time())}: reply with one word."
    )
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--json-out", default="parallel-canary.json")
    args = ap.parse_args()

    ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
    if len(ports) < 2:
        print("ERROR: --ports needs at least 2 workers", file=sys.stderr)
        return 2

    summary: dict = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cdp_port": args.cdp_port,
        "ports": ports,
        "prompt": args.prompt,
    }

    # 1. Preflight health on both workers (advisory — does not gate exit code).
    summary["health"] = [_health(p) for p in ports]

    # 2. CDP snapshot before (count of chatgpt.com page targets).
    targets_before = _cdp_targets(args.cdp_port)
    chatgpt_tabs_before = sum(
        1 for t in targets_before if "chatgpt.com" in t.get("url", "")
    )

    # 3. Fire concurrent requests, one per worker (different-tab parallel path).
    concurrent_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        futs = {
            pool.submit(_post_chat, p, args.model, args.prompt, args.timeout): p
            for p in ports
        }
        concurrent_results = [f.result() for f in as_completed(futs)]
    concurrent_results.sort(key=lambda r: r["port"])
    concurrent_window_s = round(time.monotonic() - concurrent_started, 2)

    # 4. Same-tab serialization sanity: two requests to the SAME worker.
    # Timing is advisory (live latency is noisy); logged for human inspection.
    same_port = ports[0]
    serial_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        sfuts = {
            pool.submit(_post_chat, same_port, args.model, args.prompt, args.timeout): i
            for i in range(2)
        }
        serial_results = [f.result() for f in as_completed(sfuts)]
    serial_window_s = round(time.monotonic() - serial_started, 2)

    # 5. CDP snapshot after.
    targets_after = _cdp_targets(args.cdp_port)
    chatgpt_tabs_after = sum(
        1 for t in targets_after if "chatgpt.com" in t.get("url", "")
    )

    summary["concurrent"] = {
        "results": concurrent_results,
        "total_window_s": concurrent_window_s,
        "note": (
            "different-tab sends should overlap; if total_window is close to "
            "max(individual), they serialized (unexpected for parallel mode)."
        ),
    }
    summary["same_worker_serialization"] = {
        "port": same_port,
        "results": serial_results,
        "total_window_s": serial_window_s,
        "note": (
            "same-tab sends SHOULD serialize; total_window should be roughly "
            "sum of individuals. Timing is advisory — see module docstring."
        ),
    }
    summary["cdp_targets"] = {
        "chatgpt_tabs_before": chatgpt_tabs_before,
        "chatgpt_tabs_after": chatgpt_tabs_after,
        "sample_after": targets_after[:6],
    }

    # 6. Exit decision: both concurrent 200s AND >=2 chatgpt tabs after.
    all_ok = all(r["ok"] for r in concurrent_results)
    distinct_tabs_ok = chatgpt_tabs_after >= 2
    summary["passed"] = bool(all_ok and distinct_tabs_ok)

    with open(args.json_out, "w") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\nJSON summary written to {args.json_out}", file=sys.stderr)
    print(
        f"PASS={summary['passed']} (concurrent_all_200={all_ok}, "
        f"distinct_tabs_after={chatgpt_tabs_after}>=2={distinct_tabs_ok})",
        file=sys.stderr,
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

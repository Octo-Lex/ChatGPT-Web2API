"""Stress test 2 v2: postData truncation — direct fetch injection.

The bridge can't type prompts >~2KB without timing out. To test whether CDP
truncates the *captured* postData for large request bodies, inject a large
fetch directly via Runtime.evaluate (bypassing the bridge). This lets us
control the exact body size and observe what Network.requestWillBeSent reports.

We don't actually want to send a real message — we want to observe the
reporting behavior. So we send to a benign endpoint that accepts large POSTs
and observe the CDP-reported postData length vs the actual length we sent.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request

import websockets

CDP = 9222


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


def find_target():
    targets = list_targets()
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com" in t.get("url", ""):
            return t
    raise RuntimeError("no target")


async def main():
    target = find_target()
    print(f"target: {target['url'][:80]}")
    ws_url = target["webSocketDebuggerUrl"]

    # Body sizes to test (bytes)
    sizes = [1024, 10_000, 100_000, 500_000, 2_000_000]

    async with websockets.connect(ws_url, max_size=None) as ws:
        nxt = [0]
        def nid():
            nxt[0] += 1; return nxt[0]

        # Enable Network with maxPostDataSize=4MB (what A2 would use)
        await ws.send(json.dumps({"id": nid(), "method": "Network.enable",
                                  "params": {"maxPostDataSize": 4 * 1024 * 1024}}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if r.get("id"):
                break

        results = []
        for size in sizes:
            # Build a body with a known structure: UUID at the start, filler in the middle,
            # marker at the end. Send to /backend-api/conversation/init (a benign endpoint
            # that ChatGPT's page already calls; it will 4xx but CDP still captures the request).
            test_uuid = f"a2b3c4d5-{size:08x}-4eb2-bed0-a1eebe4720f1"
            filler = "X" * size
            body_obj = {
                "messages": [{"id": test_uuid, "content": {"parts": [filler]}}],
                "end_marker": "END_OF_BODY_" + str(size),
            }
            body_str = json.dumps(body_obj)
            actual_len = len(body_str)
            print(f"\n=== testing body size {size} bytes (actual JSON: {actual_len}) ===")

            # Drain
            while True:
                try:
                    _ = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    break

            # Inject fetch via the page — fire and forget; we capture it via Network events
            # regardless of whether the request succeeds.
            fetch_js = (
                "(async()=>{try{"
                "await fetch('/backend-api/conversation/init',{"
                "method:'POST',headers:{'Content-Type':'application/json'},"
                "body:" + json.dumps(body_str) + ""
                "});}catch(e){return 'err:'+e}return 'sent'})()"
            )
            eval_id = nid()
            await ws.send(json.dumps({"id": eval_id, "method": "Runtime.evaluate",
                                      "params": {"expression": fetch_js, "awaitPromise": True,
                                                 "returnByValue": True, "timeout": 15000}}))

            captured = None
            deadline = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                r = json.loads(raw)
                if r.get("method") == "Network.requestWillBeSent":
                    req = (r.get("params") or {}).get("request", {}) or {}
                    url = req.get("url", "")
                    if "conversation/init" in url and req.get("method") == "POST":
                        pd = req.get("postData")
                        captured = {
                            "reported_post_data_len": len(pd) if pd else 0,
                            "has_post_data": req.get("hasPostData"),
                            "post_data": pd,
                        }
                        break
                if r.get("id") == eval_id:
                    pass  # eval done, keep waiting for network event

            # Analyze
            analysis = {
                "intended_body_size": actual_len,
                "filler_size": size,
            }
            if captured:
                pd = captured["post_data"]
                analysis["reported_post_data_len"] = captured["reported_post_data_len"]
                analysis["truncated"] = captured["reported_post_data_len"] < actual_len
                analysis["complete_capture"] = captured["reported_post_data_len"] >= actual_len
                # Is the UUID at the start extractable?
                import re
                m = re.search(r'"id"\s*:\s*"([0-9a-f-]{36})"', pd[:500])
                analysis["uuid_extractable_from_start"] = bool(m)
                # Is the end marker present? (proves no truncation)
                analysis["end_marker_present"] = ("END_OF_BODY_" + str(size)) in pd
            else:
                analysis["reported_post_data_len"] = 0
                analysis["note"] = "no POST captured"

            results.append(analysis)
            print(f"  intended body: {actual_len} bytes")
            print(f"  reported postData: {analysis.get('reported_post_data_len', 0)} bytes")
            print(f"  truncated: {analysis.get('truncated', '?')}")
            print(f"  UUID extractable from start: {analysis.get('uuid_extractable_from_start', '?')}")
            print(f"  end marker present (no trunc): {analysis.get('end_marker_present', '?')}")

            await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print("SUMMARY — postData truncation behavior")
    print("=" * 70)
    print(f"{'intended':>10} {'reported':>10} {'truncated':>10} {'uuid@start':>12} {'end_marker':>12}")
    for r in results:
        print(f"{r['intended_body_size']:>10} {r.get('reported_post_data_len',0):>10} "
              f"{str(r.get('truncated','?')):>10} {str(r.get('uuid_extractable_from_start','?')):>12} "
              f"{str(r.get('end_marker_present','?')):>12}")

    import os
    out_path = os.path.join(os.environ.get("TEMP", "/tmp"), "a2_stress2_v2_truncation.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

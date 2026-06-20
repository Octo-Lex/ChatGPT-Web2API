"""Capture v3 — catch EVERYTHING. No URL filter.

Goal: see the actual send path, wherever it lives. Logs every request
during the window with method + url + (for POSTs) postData. Also flags
any request whose URL contains likely-send keywords so they're easy to spot.

Run it, send ONE message in the UI, then read the output.
"""

import asyncio
import json
import sys
import time
import urllib.request

import websockets

# Keywords to highlight (probable send endpoints)
HINTS = ("conversation", "message", "send", "stream", "response", "complete", "chat", "talk")


async def main(listen_seconds: int) -> None:
    targets = json.loads(
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5
        ).read()
    )
    page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
    if not page:
        print("ERROR: no chatgpt page", file=sys.stderr); return
    ws_url = page["webSocketDebuggerUrl"]
    print(f"[v3] page ws: {ws_url}", flush=True)

    all_reqs = []
    mid = 0

    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024) as ws:
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Network.enable",
                                  "params": {"maxPostDataSize": 64 * 1024 * 1024}}))
        # Fetch on EVERYTHING (empty pattern list = match all) — but we must auto-resume
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Fetch.enable", "params": {
            "patterns": [{"requestStage": "Request"}],
        }}))
        print("[v3] Network + Fetch (catch-all) enabled. Send ONE message now.", flush=True)

        deadline = time.monotonic() + listen_seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                evt = json.loads(raw)
            except asyncio.TimeoutError:
                continue

            m = evt.get("method")
            req = None
            src = None
            if m == "Network.requestWillBeSent":
                req = evt["params"].get("request", {}); src = "N"
            elif m == "Fetch.requestPaused":
                req = evt["params"].get("request", {}); src = "F"
                # Always resume
                rid = evt["params"].get("requestId")
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueRequest",
                                          "params": {"requestId": rid}}))

            if req is None:
                continue

            url = req.get("url", "")
            method = req.get("method", "")
            # Skip noisy irrelevance
            if any(url.startswith(p) for p in (
                "data:", "blob:", "chrome:", "chrome-extension:",
            )):
                continue
            if ".js" in url or ".css" in url or ".woff" in url or ".png" in url or ".svg" in url or ".ico" in url:
                continue
            if "sentry" in url or "datapoint" in url or "metrics" in url or "/log" in url:
                continue

            entry = {"src": src, "method": method, "url": url,
                     "postData": req.get("postData"), "t": time.strftime("%H:%M:%S")}
            all_reqs.append(entry)
            flagged = any(h in url.lower() for h in HINTS)
            marker = " <<<" if flagged else ""
            pd = req.get("postData")
            pd_summary = f" body={pd[:300]}" if pd else ""
            ts = time.strftime("%H:%M:%S")
            print(f"[v3 {ts}] [{src}] {method} {url[:130]}{pd_summary}{marker}", flush=True)

    print(f"\n[v3] done. {len(all_reqs)} non-asset requests total.", flush=True)
    flagged = [r for r in all_reqs if any(h in r["url"].lower() for h in HINTS)]
    print(f"[v3] {len(flagged)} flagged (contain a hint keyword):", flush=True)
    for r in flagged:
        print(f"  [{r['src']}] {r['method']} {r['url']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 180))

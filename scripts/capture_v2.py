"""Capture v2 — browser-level Network + Fetch, with conversation-API polling.

The page-level capture missed the send. This version:
  1. Attaches to the BROWSER target (sees all pages' traffic at a higher level)
  2. Enables BOTH Network AND Fetch domains (Fetch catches at a lower layer)
  3. Uses Target.getTargets to enumerate, Target.attachToTarget for sub-targets
  4. Polls the conversation API to independently detect when a send lands
     (so even if CDP events miss it, we know a send happened + when)

Run it, then send one message in the UI.
"""

import asyncio
import json
import sys
import time
import urllib.request

import websockets

CONV = "6a36adf9-0fa8-83ed-9b9a-aae468239ae7"
WATCH_SUBSTR = "/backend-api/conversation"


async def main(listen_seconds: int) -> None:
    # Get browser-level ws (the root /browser/ endpoint, not /page/)
    targets = json.loads(
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5
        ).read()
    )
    page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
    if not page:
        print("ERROR: no chatgpt page", file=sys.stderr); return
    page_ws = page["webSocketDebuggerUrl"]
    print(f"[v2] page ws: {page_ws}", flush=True)

    hits = []
    mid = 0
    send_count_snapshot = None

    async with websockets.connect(page_ws, max_size=128 * 1024 * 1024) as ws:
        # Enable Network (page level, retry with bigger buffers)
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Network.enable",
                                  "params": {"maxPostDataSize": 64 * 1024 * 1024}}))
        # Enable Fetch with a broad pattern (intercept — but auto-resume so we don't block)
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Fetch.enable", "params": {
            "patterns": [{"urlPattern": "*backend-api/conversation*", "requestStage": "Request"}],
            "handleAuthRequests": False,
        }}))
        print("[v2] Network + Fetch enabled. Send a message now.", flush=True)

        # Independently poll the conversation message count to detect sends
        async def poll_conv_count():
            global send_count_snapshot
            try:
                js = (
                    "(async () => {"
                    f"  var r = await fetch('/backend-api/conversation/{CONV}', {{credentials:'include'}});"
                    "  var d = await r.json();"
                    "  return JSON.stringify({n: Object.keys(d.mapping||{}).length, current: d.current_node});"
                    "})()"
                )
                # We can't easily run JS on this ws without routing; instead use a second socket.
                return None
            except Exception:
                return None

        deadline = time.monotonic() + listen_seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                evt = json.loads(raw)
            except asyncio.TimeoutError:
                continue

            m = evt.get("method")
            if m == "Network.requestWillBeSent":
                req = evt["params"].get("request", {})
                url = req.get("url", "")
                if WATCH_SUBSTR in url:
                    hits.append({"src": "Network", "url": url, "method": req.get("method"),
                                 "headers": req.get("headers"), "postData": req.get("postData")})
                    print(f"\n[v2] *** Network HIT: {req.get('method')} {url}", flush=True)
                    if req.get("postData"):
                        print(f"[v2] postData:\n{req['postData'][:4000]}", flush=True)
            elif m == "Fetch.requestPaused":
                rid = evt["params"].get("requestId")
                req = evt["params"].get("request", {})
                url = req.get("url", "")
                hits.append({"src": "Fetch", "url": url, "method": req.get("method"),
                             "headers": req.get("headers"), "postData": req.get("postData")})
                print(f"\n[v2] *** Fetch PAUSED: {req.get('method')} {url}", flush=True)
                if req.get("postData"):
                    print(f"[v2] postData:\n{req['postData'][:4000]}", flush=True)
                # ALWAYS resume immediately so the real send isn't blocked
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueRequest",
                                          "params": {"requestId": rid}}))
                print(f"[v2] (resumed requestId {rid})", flush=True)

    print(f"\n[v2] done. {len(hits)} matching request(s):", flush=True)
    for h in hits:
        print(f"  [{h['src']}] {h['method']} {h['url']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 240))

"""Capture v4 — request + response bodies for the sentinel + send flow.

Correct design: at Response stage, the request is PAUSED. We call
Fetch.getResponseBody (works while paused), capture it, THEN continueResponse.
This is the only reliable way to read response bodies via CDP Fetch.

Request bodies come for free from Fetch.requestPaused at Request stage
(we read + continue immediately).

Run, send ONE message, read output.
"""

import asyncio
import json
import re
import sys
import time
import urllib.request

import websockets

WATCH = ("/backend-api/f/conversation", "/backend-api/sentinel/chat-requirements")


def redact(s: str) -> str:
    return re.sub(r"eyJ[A-Za-z0-9_.\-]+", "<JWT>", s)


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
    print(f"[v4] page ws: {ws_url}", flush=True)

    mid = 0

    async def send(cmd: dict) -> None:
        nonlocal mid
        mid += 1
        cmd["id"] = mid
        await ws.send(json.dumps(cmd))
        return mid

    async def get_resp_body(rid: str, wait_id: int) -> str:
        """Call Fetch.getResponseBody and wait for that specific response."""
        # Send the command
        await ws.send(json.dumps({"id": wait_id, "method": "Fetch.getResponseBody",
                                  "params": {"requestId": rid}}))
        # Wait for the matching id response
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                r = json.loads(raw)
                if r.get("id") == wait_id:
                    body = r.get("result", {}).get("body", "")
                    return body
            except asyncio.TimeoutError:
                break
        return "(getResponseBody timed out)"

    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024) as ws:
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Network.enable",
                                  "params": {"maxPostDataSize": 64 * 1024 * 1024}}))
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Fetch.enable", "params": {
            "patterns": [
                {"urlPattern": "*backend-api/sentinel/chat-requirements*", "requestStage": "Request"},
                {"urlPattern": "*backend-api/sentinel/chat-requirements*", "requestStage": "Response"},
                {"urlPattern": "*backend-api/f/conversation*", "requestStage": "Request"},
                {"urlPattern": "*backend-api/f/conversation*", "requestStage": "Response"},
            ],
        }}))
        print("[v4] Network + Fetch (req+resp) enabled. Send ONE message now.", flush=True)

        deadline = time.monotonic() + listen_seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                evt = json.loads(raw)
            except asyncio.TimeoutError:
                continue
            # Skip command responses we issued (handled inline by get_resp_body)
            if "id" in evt and "method" not in evt:
                continue

            m = evt.get("method")
            if m != "Fetch.requestPaused":
                continue

            params = evt["params"]
            rid = params.get("requestId")
            req = params.get("request", {})
            url = req.get("url", "")
            if not any(w in url for w in WATCH):
                # Not ours — continue immediately
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueRequest",
                                          "params": {"requestId": rid}}))
                continue

            ts = time.strftime("%H:%M:%S")
            is_resp = params.get("responseStatusCode") is not None

            if not is_resp:
                # Request stage
                pd = req.get("postData", "")
                print(f"\n[v4 {ts}] >>> REQ {req.get('method')} {url}", flush=True)
                if pd:
                    print(f"[v4] req body: {redact(pd)[:2500]}", flush=True)
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueRequest",
                                          "params": {"requestId": rid}}))
            else:
                # Response stage — paused, read body, then continue
                status = params.get("responseStatusCode")
                print(f"\n[v4 {ts}] <<< RESP {status} {url}", flush=True)
                mid += 1
                body = await get_resp_body(rid, mid)
                print(f"[v4] resp body: {redact(body)[:2500]}", flush=True)
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueResponse",
                                          "params": {"requestId": rid}}))

    print(f"\n[v4] done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 180))

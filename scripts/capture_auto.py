"""Capture v5 — self-terminating.

Runs until IT detects a send happened (conversation message count rises),
drains a few seconds for response bodies, then stops. Eliminates the
timing problem: take your time sending. Hard ceiling 10 min as a safety net.

Captures request + response bodies for the sentinel + send flow.
"""

import asyncio
import json
import re
import sys
import time
import urllib.request

import websockets

CONV_ID = "6a36adf9-0fa8-83ed-9b9a-aae468239ae7"
WATCH = ("/backend-api/f/conversation", "/backend-api/sentinel/chat-requirements")


def redact(s: str) -> str:
    return re.sub(r"eyJ[A-Za-z0-9_.\-]+", "<JWT>", s)


async def main(max_seconds: int = 600) -> None:
    targets = json.loads(
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5
        ).read()
    )
    page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
    if not page:
        print("ERROR: no chatgpt page", file=sys.stderr); return
    ws_url = page["webSocketDebuggerUrl"]
    print(f"[v5] page ws: {ws_url}", flush=True)

    # POLL socket: separate websocket for conv-count polling, so it never
    # consumes Fetch events from the capture socket. CDP allows multiple
    # clients on the same page target.
    poll_ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)

    async def conv_count() -> int:
        # NOTE: cookie-only auth is NOT enough — must send Bearer token.
        js = (
            "(async () => {"
            "  var tok = (await (await fetch('/api/auth/session',{credentials:'include'})).json()).accessToken;"
            "  var r = await fetch('/backend-api/conversation/" + CONV_ID + "', {"
            "    headers: {'Authorization': 'Bearer ' + tok}, credentials: 'include'"
            "  });"
            "  var d = await r.json();"
            "  return String(Object.keys(d.mapping||{}).length);"
            "})()"
        )
        pid = 1
        await poll_ws.send(json.dumps({"id": pid, "method": "Runtime.evaluate",
                                       "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(poll_ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                r = json.loads(raw)
                if r.get("id") == pid:
                    try:
                        return int(r.get("result", {}).get("result", {}).get("value", "0"))
                    except Exception:
                        return 0
            except asyncio.TimeoutError:
                return 0
        return 0

    baseline = await conv_count()
    print(f"[v5] baseline msg count: {baseline}", flush=True)

    # CAPTURE socket: the ONLY thing this socket does is Network + Fetch.
    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024) as ws:
        mid = 0
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Network.enable",
                                  "params": {"maxPostDataSize": 64 * 1024 * 1024}}))
        mid += 1
        await ws.send(json.dumps({"id": mid, "method": "Fetch.enable", "params": {"patterns": [
            {"urlPattern": "*backend-api/sentinel/chat-requirements*", "requestStage": "Request"},
            {"urlPattern": "*backend-api/sentinel/chat-requirements*", "requestStage": "Response"},
            {"urlPattern": "*backend-api/f/conversation*", "requestStage": "Request"},
            {"urlPattern": "*backend-api/f/conversation*", "requestStage": "Response"},
        ]}}))
        print("[v5] listening. Send a message WHENEVER ready — I auto-stop on detect.", flush=True)

        async def get_resp_body(rid: str, this_id: int) -> str:
            await ws.send(json.dumps({"id": this_id, "method": "Fetch.getResponseBody",
                                      "params": {"requestId": rid}}))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                    r = json.loads(raw)
                    if r.get("id") == this_id:
                        return r.get("result", {}).get("body", "")
                except asyncio.TimeoutError:
                    return "(timeout)"
            return "(timeout)"

        deadline = time.monotonic() + max_seconds
        last_check = time.monotonic()
        detected = False
        drain_until = None

        while time.monotonic() < deadline:
            timeout = 1.0 if not detected else max(0.1, drain_until - time.monotonic())
            if detected and time.monotonic() >= drain_until:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                evt = json.loads(raw)
            except asyncio.TimeoutError:
                # Poll on the SEPARATE socket — never touches the capture socket
                if not detected and time.monotonic() - last_check > 4:
                    last_check = time.monotonic()
                    cur = await conv_count()
                    if cur > baseline:
                        ts = time.strftime("%H:%M:%S")
                        print(f"\n[v5 {ts}] *** SEND DETECTED (count {baseline} -> {cur}). Draining 8s for response bodies.", flush=True)
                        detected = True
                        drain_until = time.monotonic() + 8
                continue

            if "id" in evt and "method" not in evt:
                continue

            if evt.get("method") != "Fetch.requestPaused":
                continue

            params = evt["params"]
            rid = params.get("requestId")
            req = params.get("request", {})
            url = req.get("url", "")
            if not any(w in url for w in WATCH):
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueRequest",
                                          "params": {"requestId": rid}}))
                continue

            ts = time.strftime("%H:%M:%S")
            is_resp = params.get("responseStatusCode") is not None
            if not is_resp:
                pd = req.get("postData", "")
                print(f"\n[v5 {ts}] >>> REQ {req.get('method')} {url}", flush=True)
                if pd:
                    print(f"[v5] req body: {redact(pd)[:2500]}", flush=True)
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueRequest",
                                          "params": {"requestId": rid}}))
            else:
                status = params.get("responseStatusCode")
                print(f"\n[v5 {ts}] <<< RESP {status} {url}", flush=True)
                mid += 1
                body = await get_resp_body(rid, mid)
                print(f"[v5] resp body: {redact(body)[:2500]}", flush=True)
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": "Fetch.continueResponse",
                                          "params": {"requestId": rid}}))

        await poll_ws.close()
        print(f"\n[v5] done. detected={detected}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 600))

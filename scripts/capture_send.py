"""Passive CDP network capture of a ChatGPT send request.

Attaches a SECOND websocket to the chatgpt.com tab (does not touch the
existing driver's socket), enables the Network domain, and records any
request to /backend-api/conversation (the send endpoint) or
/backend-api/sentinel/chat-requirements (the anti-bot gate). Captures
headers + POST body verbatim.

Pure observation: sends no messages itself. Run it, then send one message
through the normal ChatGPT UI. The script prints what it sees and exits.

Usage:
    python capture_send.py [seconds_to_listen=120]
"""

import asyncio
import json
import sys
import urllib.request

import websockets

# Endpoints we care about
WATCH = ("/backend-api/conversation", "/backend-api/sentinel/chat-requirements")


async def main(listen_seconds: int) -> None:
    # Find the chatgpt tab
    targets = json.loads(
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:9222/json"), timeout=5
        ).read()
    )
    chatgpt = [t for t in targets if "chatgpt.com" in t.get("url", "")]
    if not chatgpt:
        print("ERROR: no chatgpt.com tab found", file=sys.stderr)
        return
    ws_url = chatgpt[0]["webSocketDebuggerUrl"]
    print(f"[capture] attaching to {ws_url}", flush=True)

    captured: list[dict] = []
    msg_id = 0

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        # Enable Network domain WITH post-data capture
        msg_id += 1
        await ws.send(json.dumps({
            "id": msg_id,
            "method": "Network.enable",
            "params": {"maxPostDataSize": 64 * 1024 * 1024, "maxResourceBufferSize": 32 * 1024 * 1024},
        }))
        print("[capture] Network.enable sent — listening (send a message in the ChatGPT UI now)", flush=True)

        async def drain(timeout: float):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                return json.loads(raw)
            except asyncio.TimeoutError:
                return None

        deadline = asyncio.get_event_loop().time() + listen_seconds
        while asyncio.get_event_loop().time() < deadline:
            evt = await drain(1.0)
            if evt is None:
                continue
            if evt.get("method") != "Network.requestWillBeSent":
                continue
            req = evt["params"].get("request", {})
            url = req.get("url", "")
            if not any(w in url for w in WATCH):
                continue
            entry = {
                "url": url,
                "method": req.get("method"),
                "headers": req.get("headers"),
                "postData": req.get("postData"),
                "requestId": evt["params"].get("requestId"),
            }
            captured.append(entry)
            print(f"\n[capture] *** HIT: {req.get('method')} {url}", flush=True)
            print(f"[capture] headers: {json.dumps(req.get('headers'), indent=2)}", flush=True)
            pd = req.get("postData")
            if pd:
                print(f"[capture] postData:\n{pd}", flush=True)
            else:
                print("[capture] (no postData on this request)", flush=True)

    print(f"\n[capture] done. {len(captured)} matching request(s) recorded.", flush=True)


if __name__ == "__main__":
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    asyncio.run(main(secs))

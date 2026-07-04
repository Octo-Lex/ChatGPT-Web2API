"""Phase 1 follow-up v2: try XHR interception, AND test whether the send uses
a fetch reference captured at module load (which our window.fetch override
would miss).

Approach: install BOTH an XHR send interceptor AND re-test by checking if the
captured fetch wrapper even sees OTHER fetches (analytics calls) — if it sees
those but not the send, the send uses a captured-reference fetch; if it sees
nothing, our override technique is broken.

The most reliable technique regardless of transport: CDP Network domain
capture (Fetch.enable + Fetch.requestPaused). That lets us pause the actual
network request and read its body before letting it proceed. This script
tests the CDP Fetch API approach.
"""
import asyncio, json, urllib.request, time, websockets

CDP = 9222
BRIDGE = "http://127.0.0.1:8080"
CONV = "6a48625b-34a4-83ed-93ba-a7153c2e6295"


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


async def main():
    targets = list_targets()
    # Try multiple targets — the bridge may drive a different one than the visible conv URL.
    candidate_targets = [t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")]
    print(f"will try CDP Fetch.pause on {len(candidate_targets)} targets (one at a time)")

    marker = f"FETCHCAP-{int(time.time())}"

    for tgt in candidate_targets:
        url_short = tgt.get("url", "")[:70]
        print(f"\n=== trying target: {url_short} ===")
        captured = await try_fetch_pause_on_target(tgt["webSocketDebuggerUrl"], marker)
        if captured:
            print(f"\nSUCCESS on target {url_short}")
            print(f"captured POST body ({len(captured)} chars):")
            try:
                parsed = json.loads(captured)
                msgs = parsed.get("messages") or []
                if msgs:
                    print(f"  messages[0].id = {msgs[0].get('id')}")
                    print(f"  conversation_id = {parsed.get('conversation_id')}")
                    parts = (msgs[0].get("content") or {}).get("parts") or []
                    print(f"  parts[0][:80] = {str(parts[0])[:80]!r}")
                else:
                    print(f"  (no messages; keys: {list(parsed.keys())[:8]})")
            except Exception as e:
                print(f"  parse error: {e}")
                print(f"  raw[:600]: {captured[:600]}")
            return
        else:
            print(f"  no /f/conversation POST paused on this target")

    print("\nVERDICT: CDP Fetch.pause did not intercept the send on any target.")
    print("The send may go through a service worker, or the bridge's target was not in the list.")


async def try_fetch_pause_on_target(ws_url: str, marker: str) -> str | None:
    """Enable Fetch domain with requestPaused for /backend-api/f/conversation.
    Fire one bridge send; capture and release the paused request body."""
    async with websockets.connect(ws_url, max_size=None, close_timeout=5) as ws:
        nxt = [0]
        def nid():
            nxt[0] += 1; return nxt[0]

        # Enable Fetch with a URL pattern filter for the send endpoint.
        # stages: Request — pause at request stage so we can read postData.
        await ws.send(json.dumps({
            "id": nid(), "method": "Fetch.enable",
            "params": {
                "patterns": [{"urlPattern": "*/backend-api/f/conversation*", "requestStage": "Request"}],
            }
        }))
        # Wait for enable ACK
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if r.get("id"):
                break
        enabled = "result" in r
        if not enabled:
            print(f"  Fetch.enable failed: {r}")
            return None

        # Fire the bridge send in a background task.
        async def fire_send():
            body = json.dumps({"model": "auto", "conversation_id": CONV,
                               "messages": [{"role": "user", "content": f"Reply with exactly: {marker}"}],
                               "stream": False}).encode()
            req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    p = json.loads(resp.read())
                    return p.get("choices", [{}])[0].get("message", {}).get("content", "")
            except Exception as e:
                return f"<err: {e}>"

        send_task = asyncio.create_task(fire_send())

        # Listen for Fetch.requestPaused events.
        captured_body = None
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                if send_task.done():
                    break
                continue
            r = json.loads(raw)
            if r.get("method") == "Fetch.requestPaused":
                params = r.get("params", {})
                rid = params.get("requestId")
                req = params.get("request", {})
                url = req.get("url", "")
                print(f"  requestPaused: {req.get('method')} {url[:80]}")
                if req.get("method") == "POST" and "/f/conversation" in url:
                    # Get the post data via Fetch.getResponseBody for requests? No —
                    # for request-stage, postData is in the request object OR we call
                    # Fetch.takeResponseBodyAsStream. Actually for requestStage=Request,
                    # the postData is in params.request.postData if hasPostData.
                    pd = req.get("postData")
                    if not pd and req.get("hasPostData"):
                        # Need Fetch.getRequestPostData
                        await ws.send(json.dumps({"id": nid(), "method": "Fetch.getRequestPostData",
                                                  "params": {"requestId": rid}}))
                        # Continue listening; the response will come as an id-matched reply.
                        # For now, just allow the request to proceed.
                    if pd:
                        captured_body = pd
                    # Allow the request to proceed regardless (so the send completes).
                    await ws.send(json.dumps({"id": nid(), "method": "Fetch.continueRequest",
                                              "params": {"requestId": rid}}))
                    if captured_body:
                        break
                else:
                    # Not our target; allow to proceed.
                    await ws.send(json.dumps({"id": nid(), "method": "Fetch.continueRequest",
                                              "params": {"requestId": rid}}))
            elif r.get("id") and r.get("result", {}).get("postData") and not captured_body:
                # Response to getRequestPostData
                captured_body = r["result"]["postData"]

        # Disable Fetch domain to stop pausing requests.
        try:
            await ws.send(json.dumps({"id": nid(), "method": "Fetch.disable"}))
        except Exception:
            pass

        # Wait for the send to complete (it may have been blocked while paused).
        try:
            content = await asyncio.wait_for(send_task, timeout=60)
            print(f"  bridge returned: {content}")
        except asyncio.TimeoutError:
            print(f"  bridge send timed out after Fetch pause")

        return captured_body


asyncio.run(main())

"""Debug: why does the conv-count poll return 0? Test the exact JS."""

import asyncio
import json
import urllib.request

import websockets


async def main():
    targets = json.loads(
        urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5
        ).read()
    )
    page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
    ws_url = page["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        # First get the token
        mid = 1
        await ws.send(json.dumps({
            "id": mid, "method": "Runtime.evaluate",
            "params": {"expression": "(async()=>{var r=await fetch('/api/auth/session',{credentials:'include'});var d=await r.json();return d.accessToken||'';})()",
                       "awaitPromise": True, "returnByValue": True}
        }))
        # Read until we get id=1
        token = ""
        for _ in range(50):
            raw = await asyncio.wait_for(ws.recv(), timeout=3)
            r = json.loads(raw)
            if r.get("id") == 1:
                token = r.get("result", {}).get("result", {}).get("value", "")
                break
        print(f"token (first 30): {token[:30]}...")

        # Now fetch conv WITH auth header
        mid = 2
        js = (
            "(async () => {"
            "  var r = await fetch('/backend-api/conversation/6a36adf9-0fa8-83ed-9b9a-aae468239ae7', {"
            f"    headers: {{'Authorization': 'Bearer ' + '{token}'}},"  # NO — cleaner below
            "    credentials: 'include'"
            "  });"
            "  var txt = await r.text();"
            "  return txt.slice(0, 200);"
            "})()"
        )
        # Actually use the token via window var to avoid f-string nesting
        js = (
            "(async () => {"
            "  var tok = (await (await fetch('/api/auth/session',{credentials:'include'})).json()).accessToken;"
            "  var r = await fetch('/backend-api/conversation/6a36adf9-0fa8-83ed-9b9a-aae468239ae7', {"
            "    headers: {'Authorization': 'Bearer ' + tok}, credentials: 'include'"
            "  });"
            "  var d = await r.json();"
            "  return JSON.stringify({status: r.status, keys: Object.keys(d).slice(0,10), mapSize: Object.keys(d.mapping||{}).length});"
            "})()"
        )
        await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        for _ in range(50):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            r = json.loads(raw)
            if r.get("id") == 2:
                print("result:", r.get("result", {}).get("result", {}).get("value", "(none)"))
                if r.get("result", {}).get("exceptionDetails"):
                    print("EXCEPTION:", r["result"]["exceptionDetails"])
                break


asyncio.run(main())

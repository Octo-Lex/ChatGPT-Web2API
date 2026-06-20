"""curl_cffi TLS-impersonation send test.

The pure-Python (urllib) send 403'd with 'Unusual activity'. This test
isolates whether TLS fingerprinting is the cause by replaying the SAME
send request through curl_cffi with Chrome impersonation. If it goes
200, TLS was the gate. If still 403, something else.

prepare/finalize still use urllib (they already worked) — only the SEND
swaps to Chrome-impersonated TLS.
"""

import asyncio
import base64
import json
import time
import urllib.request

from curl_cffi import requests as cffi_requests
import websockets

CONV = "6a36adf9-0fa8-83ed-9b9a-aae468239ae7"

STATIC_HEADERS = {
    "OAI-Language": "en-US",
    "Content-Type": "application/json",
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "OAI-Client-Build-Number": "7646290",
    "OAI-Client-Version": "prod-497f333866796e100096ad083b51ca949d22e751",
    "OAI-Device-Id": "a2791825-a74f-4557-84cb-b611834e7f6c",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": f"https://chatgpt.com/c/{CONV}",
    "Origin": "https://chatgpt.com",
    "Accept": "*/*",
}


def http_post_urllib(url, headers, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"EXC: {e}"


async def get_token_and_session(ws_url):
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        js = ("(async () => {"
              "  var s = await (await fetch('/api/auth/session',{credentials:'include'})).json();"
              "  var sid=''; try{sid=localStorage.getItem('oai-session-id')||''}catch(e){}"
              "  return JSON.stringify({token: s.accessToken||'', session_id: sid});"
              "})()")
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        for _ in range(60):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            r = json.loads(raw)
            if r.get("id") == 1:
                d = json.loads(r.get("result", {}).get("result", {}).get("value", "{}"))
                return d.get("token", ""), d.get("session_id", "")
        return "", ""


async def get_parent_message_id(ws_url, conv_id, token):
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        tok_js = json.dumps(token)
        js = ("(async () => {"
              "  var r = await fetch('/backend-api/conversation/" + conv_id + "', {"
              "    headers: {'Authorization': 'Bearer ' + " + tok_js + "}, credentials: 'include'});"
              "  if(!r.ok) return JSON.stringify({err: r.status});"
              "  var d = await r.json(); var last=null; var n=d.current_node; var g=0;"
              "  while(n && g<100){g++; var nd=d.mapping[n]||{};"
              "    if(nd.message && nd.message.author && nd.message.author.role && nd.message.author.role!=='unknown'){last=nd.message.id;}"
              "    n=nd.parent;}"
              "  return JSON.stringify({last_msg: last});"
              "})()")
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        for _ in range(60):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                break
            r = json.loads(raw)
            if r.get("id") == 1:
                d = json.loads(r.get("result", {}).get("result", {}).get("value", "{}"))
                return d.get("last_msg") or ""
        return ""


async def main():
    print("=" * 70)
    print("curl_cffi TLS-IMPERSONATION SEND TEST")
    print("=" * 70)

    targets = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5).read())
    page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
    ws_url = page["webSocketDebuggerUrl"]

    print("\n[mint] fresh token...")
    token, session_id = await get_token_and_session(ws_url)
    print(f"[mint] token len: {len(token)}")
    if not token:
        print("FATAL"); return

    base = "https://chatgpt.com/backend-api"
    hdr = dict(STATIC_HEADERS)
    hdr["Authorization"] = f"Bearer {token}"
    if session_id:
        hdr["OAI-Session-Id"] = session_id

    # prepare (urllib, works fine)
    print("\n[prepare] via urllib...")
    now_ts = int(time.time())
    arr = [now_ts, 1,
           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
           "https://chatgpt.com/backend-api/sentinel/sdk.js",
           "prod-497f333866796e100096ad083b51ca949d22e751",
           "en-US", "en-US,en", 0.29999998211860657,
           "logi‍n[object NavigatorLogin]", "location", "toolbar",
           13611746.9000000006, "cb3bb9e0-1e5c-44e0-99a2-bbb2c837f5ba", "",
           16, now_ts * 1000.0, 0, 0, 0, 0, 0, 0, 0]
    p = base64.b64encode(json.dumps(arr, separators=(",", ":")).encode()).decode()
    pa_status, pa_body = http_post_urllib(f"{base}/sentinel/chat-requirements/prepare", hdr, {"p": p})
    print(f"[prepare] {pa_status}")
    prepare_token = json.loads(pa_body).get("prepare_token") if pa_status == 200 else None
    if not prepare_token:
        print("[prepare] FAILED — abort"); return

    # finalize (urllib, works fine)
    print("\n[finalize] via urllib...")
    fb_status, fb_body = http_post_urllib(f"{base}/sentinel/chat-requirements/finalize", hdr, {"prepare_token": prepare_token})
    print(f"[finalize] {fb_status}")
    finalize_token = json.loads(fb_body).get("token") if fb_status == 200 else None
    print(f"[finalize] token: {bool(finalize_token)}")

    # parent_message_id
    print("\n[parent] fetching...")
    parent_id = await get_parent_message_id(ws_url, CONV, token)
    print(f"[parent] {parent_id}")
    if not parent_id:
        print("FATAL: no parent_id"); return

    # SEND via curl_cffi with Chrome impersonation
    print("\n" + "=" * 70)
    print("[SEND] via curl_cffi (impersonate=chrome) — posts real message")
    print("=" * 70)
    import uuid
    send_body = {
        "action": "next",
        "messages": [{
            "id": str(uuid.uuid4()),
            "author": {"role": "user"},
            "create_time": time.time(),
            "content": {"content_type": "text", "parts": ["python-curlcffi-test"]},
            "metadata": {"selected_sources": [], "selected_github_repos": [],
                         "selected_all_github_repos": False,
                         "serialization_metadata": {"custom_symbol_offsets": []}},
        }],
        "conversation_id": CONV,
        "parent_message_id": parent_id,
        "model": "gpt-5-5-thinking",
        "client_prepare_state": "success",
        "timezone_offset_min": -180,
        "timezone": "Asia/Riyadh",
        "conversation_mode": {"kind": "primary_assistant"},
        "enable_message_followups": True,
        "system_hints": [],
        "supports_buffering": True,
        "supported_encodings": ["v1"],
        "client_contextual_info": {"is_dark_mode": True, "time_since_loaded": 13611,
                                   "page_height": 945, "page_width": 1920, "pixel_ratio": 1,
                                   "screen_height": 1080, "screen_width": 1920, "app_name": "chatgpt.com"},
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
        "thinking_effort": "extended",
    }
    send_hdr = dict(hdr)
    send_hdr["openai-sentinel-chat-requirements-token"] = prepare_token
    if finalize_token:
        send_hdr["openai-sentinel-proof-token"] = finalize_token

    try:
        resp = cffi_requests.post(
            f"{base}/f/conversation",
            headers=send_hdr,
            json=send_body,
            impersonate="chrome",
            timeout=30,
        )
        print(f"\n[SEND] STATUS: {resp.status_code}")
        body = resp.text
        print(f"[SEND] BODY (first 1500): {body[:1500]}")
        # If streaming, show a bit more
        if resp.status_code == 200 and len(body) > 1500:
            print(f"[SEND] ... (total {len(body)} bytes)")
    except Exception as e:
        print(f"[SEND] EXCEPTION: {type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    print("DONE — compare against the urllib 403.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

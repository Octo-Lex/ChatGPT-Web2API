"""Three-stage fingerprint-replay test against the live ChatGPT session.

Goal: determine whether a Python client can drive the send flow using a
fabricated/replayed fingerprint, or whether browser-minting is required.

Stages (run in order, stop at first hard failure):
  1. REPLAY  — POST the captured prepare blob VERBATIM. Does the server accept
     a replayed prepare call? (Tests one-time-use vs reusable.)
  2. RECONSTRUCT — build the blob in Python with a FRESH timestamp, same
     values otherwise. Does it accept a Python-built blob?
  3. SEND — POST a real message to /f/conversation using whatever token the
     prepare/finalize flow produced. End-to-end proof.

All three use the live session's access token (minted fresh from /api/auth/session
via CDP). Sends to the existing test conversation. Prints raw server responses.

NOTE: stage 3, if reached, posts a real message ("python test") to the
conversation. That's the only outward action.
"""

import asyncio
import base64
import json
import sys
import time
import urllib.request

import websockets

CONV = "6a36adf9-0fa8-83ed-9b9a-aae468239ae7"
# The chatgpt page tab ws (where we mint the token)
PAGE_TARGET_HINT = "chatgpt.com"

# Headers reconstructed from capture (the constant ones; Authorization + dynamic set later)
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

# The captured prepare blob (the 'p' field), verbatim. We'll reuse it in stage 1.
CAPTURED_PREPARE_P = "gAAAAACWzMwMDAsIlNhdCBKdW4gMjAgMjAyNiAyMTo1OTo0MyBHTVQrMDMwMCAoQXJhYmlhbiBTdGFuZGFyZCBUaW1lKSIsNDM5NTYzMDU5MiwxLCJNb3ppbGxhLzUuMCAoV2luZG93cyBOVCAxMC4wOyBXaW42NDsgeDY0KSBBcHBsZVdlYktpdC81MzcuMzYgKEtIVE1MLCBsaWtlIEdlY2tvKSBDaHJvbWUvMTQ5LjAuMC4wIFNhZmFyaS81MzcuMzYiLCJodHRwczovL2NoYXRncHQuY29tL2JhY2tlbmQtYXBpL3NlbnRpbmVsL3Nkay5qcyIsInByb2QtNDk3ZjMzMzg2Njc5NmUxMDAwOTZhZDA4M2I1MWNhOTQ5ZDIyZTc1MSIsImVuLVVTIiwiZW4tVVMsZW4iLDAuMjk5OTk5OTgyMTE4NjA2NTcsImxvZ2lu4oiSW29iamVjdCBOYXZpZ2F0b3JMb2dpbl0iLCJsb2NhdGlvbiIsInRvb2xiYXIiLDEzNjExNzQ2LjkwMDAwMDAwNiwiY2IzYmI5ZTAtMWU1Yy00OGUwLTk5YTItYmJiMmM4MzdmNWJhIiwiIiwxNiwxNzgxOTY4MzcyMTgyLjcsMCwwLDAsMCwwLDAsMF0="


async def get_token_and_session(ws_url: str) -> tuple[str, str]:
    """Mint a fresh access token + OAI-Session-Id from the page via CDP."""
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        js = (
            "(async () => {"
            "  var s = await (await fetch('/api/auth/session',{credentials:'include'})).json();"
            "  var sid = (function(){try{return localStorage.getItem('oai-session-id')||''}catch(e){return ''}})();"
            "  if(!sid){try{var d=JSON.parse(localStorage.getItem('oai-did-template')||'{}');sid=d.id||''}catch(e){}}"
            "  return JSON.stringify({token: s.accessToken||'', user: s.user?.name||'', session_id: sid});"
            "})()"
        )
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        for _ in range(60):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            r = json.loads(raw)
            if r.get("id") == 1:
                v = r.get("result", {}).get("result", {}).get("value", "{}")
                d = json.loads(v)
                return d.get("token", ""), d.get("session_id", "")
        return "", ""


async def get_parent_message_id(ws_url: str, conv_id: str, token: str) -> str:
    """Fetch the raw conversation mapping and return the ID of the LAST real
    message so we can use it as parent_message_id. Token inlined into JS
    (CDP Runtime.evaluate arguments API was unreliable)."""
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        # Escape the token for safe JS string embedding
        tok_js = json.dumps(token)
        js = (
            "(async () => {"
            "  var r = await fetch('/backend-api/conversation/" + conv_id + "', {"
            "    headers: {'Authorization': 'Bearer ' + " + tok_js + "}, credentials: 'include'"
            "  });"
            "  if(!r.ok) return JSON.stringify({err: r.status});"
            "  var d = await r.json();"
            "  var lastMsg = null;"
            "  var n = d.current_node;"
            "  var guard = 0;"
            "  while(n && guard < 100){"
            "    guard++;"
            "    var nd = d.mapping[n] || {};"
            "    if(nd.message && nd.message.author && nd.message.author.role && nd.message.author.role !== 'unknown'){ lastMsg = nd.message.id; }"
            "    n = nd.parent;"
            "  }"
            "  return JSON.stringify({current: d.current_node, last_msg: lastMsg});"
            "})()"
        )
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        for _ in range(60):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                break
            r = json.loads(raw)
            if r.get("id") == 1:
                v = r.get("result", {}).get("result", {}).get("value", "{}")
                try:
                    d = json.loads(v)
                except Exception:
                    return ""
                if d.get("err"):
                    print(f"[parent] fetch error status: {d['err']}")
                return d.get("last_msg") or ""
        return ""


def http_post(url: str, headers: dict, body: dict) -> tuple[int, str, dict]:
    """Synchronous POST. Returns (status, body_text, response_headers)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), dict(e.headers)
    except Exception as e:
        return -1, f"EXC: {e}", {}


def http_get_raw(url: str, headers: dict) -> tuple[int, str]:
    """GET that returns the streaming body for SSE inspection."""
    req = urllib.request.Request(url, method="GET")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return -1, f"EXC: {e}"


async def main():
    print("=" * 70)
    print("FINGERPRINT REPLAY TEST — three stages, stop at first hard failure")
    print("=" * 70)

    # Find page target
    targets = json.loads(urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5).read())
    page = next((t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")), None)
    if not page:
        print("ERROR: no chatgpt page"); return
    ws_url = page["webSocketDebuggerUrl"]

    print("\n[mint] getting fresh token from live session...")
    token, session_id = await get_token_and_session(ws_url)
    print(f"[mint] token len: {len(token)}")
    print(f"[mint] user: {token[:20]}... (truncated)")
    if not token:
        print("FATAL: no token"); return
    # Decode JWT payload to confirm it's current
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        exp = payload.get("exp", 0)
        iat = payload.get("iat", 0)
        print(f"[mint] JWT iat={iat} exp={exp} (expires in {exp - int(time.time())}s)")
    except Exception as e:
        print(f"[mint] (couldn't decode JWT: {e})")

    base = "https://chatgpt.com/backend-api"
    hdr = dict(STATIC_HEADERS)
    hdr["Authorization"] = f"Bearer {token}"
    if session_id:
        hdr["OAI-Session-Id"] = session_id

    # ── STAGE 1: REPLAY captured prepare blob verbatim ─────────────
    print("\n" + "─" * 70)
    print("STAGE 1: REPLAY — POST captured prepare blob verbatim")
    print("─" * 70)
    body1 = {"p": CAPTURED_PREPARE_P}
    s1_status, s1_body, s1_hdr = http_post(f"{base}/sentinel/chat-requirements/prepare", hdr, body1)
    print(f"[1] status: {s1_status}")
    print(f"[1] body (first 800): {s1_body[:800]}")
    # Look for a token in the response
    s1_token = None
    try:
        j = json.loads(s1_body)
        s1_token = j.get("token") or j.get("prepare_token") or j.get("id")
    except Exception:
        pass
    print(f"[1] token found: {bool(s1_token)}")

    if s1_status != 200:
        print("\n[1] *** NON-200 — replay rejected. Examining before stage 2.")
        print("[1] (If 401/403: token issue. If 400: blob rejected. If 429: rate limited.)")

    # Continue regardless — we want to see stage 2 even if stage 1 was informative-failure

    # ── STAGE 2: RECONSTRUCT blob in Python with fresh timestamp ───
    print("\n" + "─" * 70)
    print("STAGE 2: RECONSTRUCT — build blob in Python, fresh timestamp")
    print("─" * 70)
    # Rebuild the same array but with current time
    now_js = time.strftime("%a %b %d %Y %H:%M:%S GMT+0300 (Arabian Standard Time)", time.localtime())
    # The decoded blob was a JSON-ish array; reconstruct minimally
    # Using the same values but fresh timestamp + fresh epoch ms
    arr = [
        4395630592, 1,
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "https://chatgpt.com/backend-api/sentinel/sdk.js",
        "prod-497f333866796e100096ad083b51ca949d22e751",
        "en-US", "en-US,en",
        0.29999998211860657,
        "logi‍n[object NavigatorLogin]", "location", "toolbar",
        13611746.9000000006,  # timing value
        "cb3bb9e0-1e5c-44e0-99a2-bbb2c837f5ba", "",
        16, 1781968372182.7,  # epoch ms
        0, 0, 0, 0, 0, 0, 0,
    ]
    # Override the parts that should be fresh
    arr[0] = int(time.time())  # the leading big number was a unix timestamp
    arr_json = json.dumps(arr, separators=(",", ":"))
    p2 = base64.b64encode(arr_json.encode("utf-8")).decode("ascii")
    body2 = {"p": p2}
    print(f"[2] reconstructed p len: {len(p2)} (vs captured {len(CAPTURED_PREPARE_P)})")
    s2_status, s2_body, s2_hdr = http_post(f"{base}/sentinel/chat-requirements/prepare", hdr, body2)
    print(f"[2] status: {s2_status}")
    print(f"[2] body (first 800): {s2_body[:800]}")
    s2_token = None
    try:
        j = json.loads(s2_body)
        s2_token = j.get("token") or j.get("prepare_token") or j.get("id")
    except Exception:
        pass
    print(f"[2] token found: {bool(s2_token)}")

    # ── STAGE 3: full prepare → finalize → send (captured order) ────
    print("\n" + "─" * 70)
    print("STAGE 3: full prepare → finalize → SEND (captured order)")
    print("─" * 70)

    # 3a: fresh prepare (reuse stage-2 reconstruction logic)
    now_ts = int(time.time())
    arr_fresh = [
        now_ts, 1,
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "https://chatgpt.com/backend-api/sentinel/sdk.js",
        "prod-497f333866796e100096ad083b51ca949d22e751",
        "en-US", "en-US,en",
        0.29999998211860657,
        "logi‍n[object NavigatorLogin]", "location", "toolbar",
        13611746.9000000006,
        "cb3bb9e0-1e5c-44e0-99a2-bbb2c837f5ba", "",
        16, now_ts * 1000.0,
        0, 0, 0, 0, 0, 0, 0,
    ]
    p_fresh = base64.b64encode(json.dumps(arr_fresh, separators=(",", ":")).encode()).decode()
    s3a_status, s3a_body, _ = http_post(f"{base}/sentinel/chat-requirements/prepare", hdr, {"p": p_fresh})
    print(f"[3a] prepare: {s3a_status}")
    prepare_token = None
    try:
        prepare_token = json.loads(s3a_body).get("prepare_token")
    except Exception:
        pass
    print(f"[3a] prepare_token: {bool(prepare_token)} ({str(prepare_token)[:40] if prepare_token else 'none'})")
    if not prepare_token:
        print("[3] ABORT — no prepare_token"); return

    # 3b: finalize with the prepare_token (matches captured finalize body)
    fin_body = {"prepare_token": prepare_token}
    s3b_status, s3b_body, s3b_hdr = http_post(f"{base}/sentinel/chat-requirements/finalize", hdr, fin_body)
    print(f"\n[3b] finalize: {s3b_status}")
    print(f"[3b] finalize body (first 800): {s3b_body[:800]}")
    finalize_token = None
    try:
        fj = json.loads(s3b_body)
        finalize_token = fj.get("token") or fj.get("finalize_token") or fj.get("proof_token")
    except Exception:
        pass
    print(f"[3b] finalize returned a token: {bool(finalize_token)}")
    # Some flows return nothing on finalize (just 200 OK); the prepare_token IS the auth.
    # Capture all candidate token names for inspection.
    try:
        fj_keys = list(json.loads(s3b_body).keys())
        print(f"[3b] finalize response keys: {fj_keys}")
    except Exception:
        print(f"[3b] finalize response not JSON")

    # 3c: fetch parent_message_id
    print("\n[3c] fetching parent_message_id...")
    parent_id = await get_parent_message_id(ws_url, CONV, token)
    if not parent_id:
        print("[3c] could not determine parent_message_id — ABORT (would 422).")
        return
    print(f"[3c] parent_message_id: {parent_id}")

    # 3d: SEND — try BOTH tokens in the header (prepare and finalize) to maximize chance
    print("\n[3d] SEND — posting real message ('python-fingerprint-test-finalize')...")
    print("[3d] NOTE: posts a real message to the conversation.")
    import uuid
    send_body = {
        "action": "next",
        "messages": [{
            "id": str(uuid.uuid4()),
            "author": {"role": "user"},
            "create_time": time.time(),
            "content": {"content_type": "text", "parts": ["python-fingerprint-test-finalize"]},
            "metadata": {
                "selected_sources": [], "selected_github_repos": [],
                "selected_all_github_repos": False,
                "serialization_metadata": {"custom_symbol_offsets": []},
            },
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
        "client_contextual_info": {
            "is_dark_mode": True, "time_since_loaded": 13611,
            "page_height": 945, "page_width": 1920,
            "pixel_ratio": 1, "screen_height": 1080, "screen_width": 1920,
            "app_name": "chatgpt.com",
        },
        "paragen_cot_summary_display_override": "allow",
        "force_parallel_switch": "auto",
        "thinking_effort": "extended",
    }
    # Set ALL plausible token headers so we don't fail on a header-name guess
    send_hdr = dict(hdr)
    send_hdr["openai-sentinel-chat-requirements-token"] = prepare_token
    if finalize_token:
        send_hdr["openai-sentinel-proof-token"] = finalize_token
    s3d_status, s3d_body, _ = http_post(f"{base}/f/conversation", send_hdr, send_body)
    print(f"\n[3d] SEND status: {s3d_status}")
    print(f"[3d] SEND body (first 1500): {s3d_body[:1500]}")

    print("\n" + "=" * 70)
    print("DONE — raw results above. Interpret after.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

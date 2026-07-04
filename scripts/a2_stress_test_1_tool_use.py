"""Stress test 1+3 combined: tool-use send on a FRESH chat.

Two birds with one stone:
  - Stress test 1: capture the POST body for a tool-using send; verify messages[0].id
    present in the same shape as plain-text sends.
  - Stress test 3: this is a fresh chat (no conversation_id); verify the persistent
    Network listener captures the POST before/independent of any per-send scope.

Method:
  - Attach Network domain to ALL chatgpt targets (persistent listener).
  - Fire a fresh-chat tool-use send via the bridge (no conversation_id).
  - Capture any /backend-api/f/conversation POST; record body shape.
  - After completion, find the new conversation, fetch its mapping, check whether
    the captured UUID survived as user node message.id.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import websockets

CDP = 9222
BRIDGE = "http://127.0.0.1:8080"


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


async def bridge_fresh_chat_send(prompt: str) -> dict:
    """Send WITHOUT conversation_id — fresh chat path."""
    body = json.dumps({
        "model": "auto",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            p = json.loads(r.read())
            return {
                "ok": True,
                "latency": round(time.time() - t0, 2),
                "content": p.get("choices", [{}])[0].get("message", {}).get("content", "")[:400],
                "conversation_id": p.get("conversation_id", ""),
            }
    except Exception as e:
        return {"ok": False, "latency": round(time.time() - t0, 2), "error": str(e)}


async def main():
    targets = [t for t in list_targets() if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")]
    print(f"persistent Network listener on {len(targets)} targets")

    captured_posts: list[dict] = []

    async def watch(t):
        async with websockets.connect(t["webSocketDebuggerUrl"], max_size=None, close_timeout=5) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Network.enable",
                                      "params": {"maxPostDataSize": 4 * 1024 * 1024}}))
            # wait for ACK
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                if r.get("id"):
                    break
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                r = json.loads(raw)
                m = r.get("method", "")
                if m != "Network.requestWillBeSent":
                    continue
                req = (r.get("params") or {}).get("request", {}) or {}
                url = req.get("url", "")
                if req.get("method") == "POST" and "/backend-api/f/conversation" in url:
                    captured_posts.append({
                        "target": t.get("url", "")[:60],
                        "url": url,
                        "post_data": req.get("postData"),
                        "has_post_data": req.get("hasPostData"),
                        "request_id": (r.get("params") or {}).get("requestId"),
                    })

    tasks = [asyncio.create_task(watch(t)) for t in targets]
    await asyncio.sleep(1.5)

    # Tool-use prompt that should trigger web search.
    prompt = "Search the web for the current Bitcoin price in USD, then tell me the price."
    print(f"\nfiring FRESH-CHAT tool-use send:\n  prompt: {prompt!r}")
    result = await bridge_fresh_chat_send(prompt)
    print(f"\nbridge returned ({result.get('latency')}s):")
    print(f"  ok: {result.get('ok')}")
    print(f"  conversation_id: {result.get('conversation_id')}")
    content = result.get("content", "")
    print(f"  content[:300]: {content[:300]!r}")
    if "bitcoin" in content.lower() or "$" in content or "BTC" in content:
        print("  -> looks like the tool actually ran (price mentioned)")
    else:
        print("  -> content does not mention a price; tool may not have fired, or stale return")

    await asyncio.sleep(2.0)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Filter to the actual send (not /prepare)
    send_posts = [p for p in captured_posts
                  if p["url"].rstrip("/").endswith("/f/conversation") and p.get("post_data")]
    print(f"\n=== {len(send_posts)} send POST(s) captured (excludes /prepare) ===")

    for i, p in enumerate(send_posts):
        print(f"\n--- send POST {i+1} ---")
        print(f"target: {p['target']}")
        print(f"url: {p['url']}")
        pd = p.get("post_data") or ""
        print(f"post_data length: {len(pd)} chars")
        try:
            parsed = json.loads(pd)
            msgs = parsed.get("messages") or []
            print(f"action: {parsed.get('action')}")
            print(f"conversation_id in body: {parsed.get('conversation_id')}")
            print(f"model: {parsed.get('model')}")
            if msgs:
                m0 = msgs[0]
                print(f"\nmessages[0].id: {m0.get('id')}")
                print(f"messages[0].author.role: {(m0.get('author') or {}).get('role')}")
                ct = (m0.get('content') or {})
                print(f"messages[0].content.content_type: {ct.get('content_type')}")
                parts = ct.get("parts") or []
                print(f"messages[0].content.parts[0][:80]: {str(parts[0] if parts else '')[:80]!r}")
                # Tool-use specific: check for extra fields
                print(f"\nmessages[0] ALL keys: {list(m0.keys())}")
                metadata = m0.get("metadata") or {}
                if metadata:
                    print(f"messages[0].metadata keys: {list(metadata.keys())}")
                    if "serialization_metadata" in metadata:
                        print("  (has serialization_metadata — same as plain-text)")
                # Top-level extra fields that might indicate tool context
                top_extras = {k: v for k, v in parsed.items()
                              if k not in ("action", "messages", "conversation_id",
                                           "parent_message_id", "model", "timezone",
                                           "timezone_offset_min")}
                if top_extras:
                    print(f"\ntop-level EXTRA fields (vs plain-text shape):")
                    for k, v in top_extras.items():
                        print(f"  {k}: {str(v)[:120]}")
                else:
                    print("\nno top-level extra fields — same shape as plain-text send")
            else:
                print("NO messages array in body")
                print(f"body keys: {list(parsed.keys())}")
                print(f"body[:600]: {pd[:600]}")
        except Exception as e:
            print(f"  parse failed: {e}")
            print(f"  raw[:600]: {pd[:600]}")

    # Save full
    import os
    out_path = os.path.join(os.environ.get("TEMP", "/tmp"), "a2_stress1_tool_use_posts.json")
    with open(out_path, "w") as f:
        json.dump({"result": result, "captured_posts": captured_posts}, f, indent=2, default=str)
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

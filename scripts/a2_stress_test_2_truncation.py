"""Stress test 2: long-prompt postData truncation.

Sends prompts of increasing size (5KB, 50KB, 200KB, 500KB) and checks whether
CDP's request.postData is complete or truncated, and whether the UUID is still
extractable.

The UUID sits at the START of the JSON body (messages[0].id), so even heavy
truncation should preserve it — but maxPostDataSize=1MB may be exceeded by the
real request body for large agentic prompts.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import websockets

CDP = 9222
BRIDGE = "http://127.0.0.1:8080"
CONV = "6a48625b-34a4-83ed-93ba-a7153c2e6295"


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


def find_target():
    targets = list_targets()
    for t in targets:
        if t.get("type") == "page" and CONV in t.get("url", ""):
            return t
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com/c/" in t.get("url", ""):
            return t
    raise RuntimeError("no target")


def make_prompt(target_bytes: int) -> str:
    """Build a prompt of approximately target_bytes.
    The filler is distinctive so we can detect truncation."""
    header = f"Reply with exactly: TRUNC-TEST. Then ignore the following filler:\n\n"
    # Each filler line ~80 chars
    filler_line = "FILLER-{:06d}-" + ("x" * 60) + "\n"
    body = ""
    i = 0
    while len(body) < target_bytes:
        body += filler_line.format(i)
        i += 1
    return header + body


async def bridge_send(prompt: str) -> dict:
    body = json.dumps({
        "model": "auto", "conversation_id": CONV,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            p = json.loads(r.read())
            return {"ok": True, "latency": round(time.time() - t0, 2),
                    "content": p.get("choices", [{}])[0].get("message", {}).get("content", "")[:80]}
    except Exception as e:
        return {"ok": False, "latency": round(time.time() - t0, 2), "error": str(e)}


async def main():
    target = find_target()
    print(f"target: {target['url'][:80]}")
    ws_url = target["webSocketDebuggerUrl"]

    # Sizes to test (in KB)
    sizes_kb = [5, 50, 200, 500]

    async with websockets.connect(ws_url, max_size=None) as ws:
        nxt = [0]
        def nid():
            nxt[0] += 1; return nxt[0]

        # Enable Network with maxPostDataSize=4MB (well above any test size)
        await ws.send(json.dumps({"id": nid(), "method": "Network.enable",
                                  "params": {"maxPostDataSize": 8 * 1024 * 1024}}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if r.get("id"):
                break

        results = []
        for size_kb in sizes_kb:
            prompt = make_prompt(size_kb * 1024)
            actual_prompt_bytes = len(prompt.encode())
            print(f"\n=== size target: {size_kb}KB (actual prompt: {actual_prompt_bytes} bytes) ===")

            # Drain pending events
            while True:
                try:
                    _ = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    break

            send_task = asyncio.create_task(bridge_send(prompt))
            captured = None
            deadline = time.time() + 180
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    if send_task.done():
                        break
                    continue
                r = json.loads(raw)
                if r.get("method") == "Network.requestWillBeSent":
                    req = (r.get("params") or {}).get("request", {}) or {}
                    url = req.get("url", "")
                    if req.get("method") == "POST" and url.rstrip("/").endswith("/f/conversation"):
                        pd = req.get("postData")
                        if pd:
                            captured = {
                                "post_data_len": len(pd),
                                "has_post_data": req.get("hasPostData"),
                                "post_data": pd,
                            }
                            # Don't break — let the send finish
            send_result = await send_task

            # Analyze capture
            analysis = {
                "size_target_kb": size_kb,
                "actual_prompt_bytes": actual_prompt_bytes,
                "send_result": send_result,
            }
            if captured:
                pd = captured["post_data"]
                analysis["captured_post_data_len"] = captured["post_data_len"]
                # Is it complete JSON? (truncation would break parsing)
                try:
                    parsed = json.loads(pd)
                    analysis["json_parses"] = True
                    msgs = parsed.get("messages") or []
                    if msgs:
                        mid = msgs[0].get("id")
                        analysis["messages_0_id"] = mid
                        analysis["uuid_extractable"] = bool(mid)
                    # Check if the prompt text is fully present (last filler line)
                    parts = (msgs[0].get("content") or {}).get("parts") or [] if msgs else []
                    if parts:
                        prompt_in_body = str(parts[0])
                        # The prompt should END with a filler line (not truncated mid-body)
                        analysis["prompt_fully_in_body"] = prompt_in_body.endswith("\n") and "FILLER-" in prompt_in_body[-100:]
                        analysis["prompt_in_body_len"] = len(prompt_in_body)
                except json.JSONDecodeError as e:
                    analysis["json_parses"] = False
                    analysis["json_error"] = str(e)
                    # Even if JSON is truncated, can we extract the UUID by regex?
                import re
                m = re.search(r'"id"\s*:\s*"([0-9a-f-]{36})"', pd[:500])
                analysis["uuid_via_regex_in_first_500"] = m.group(1) if m else None
            else:
                analysis["captured_post_data_len"] = 0
                analysis["note"] = "no POST captured"

            results.append(analysis)
            print(f"  send ok: {send_result.get('ok')}, content: {send_result.get('content','')[:50]!r}")
            print(f"  captured post_data: {analysis.get('captured_post_data_len', 0)} bytes")
            print(f"  JSON parses: {analysis.get('json_parses', False)}")
            print(f"  UUID extractable: {analysis.get('uuid_extractable', analysis.get('uuid_via_regex_in_first_500'))}")
            print(f"  prompt fully in body: {analysis.get('prompt_fully_in_body', 'n/a')}")

            await asyncio.sleep(3.0)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"  {r['size_target_kb']:>4}KB prompt -> captured {r.get('captured_post_data_len',0):>8} bytes, "
              f"JSON={'OK' if r.get('json_parses') else 'BROKEN'}, "
              f"UUID={'OK' if r.get('uuid_extractable') or r.get('uuid_via_regex_in_first_500') else 'MISSING'}")

    import os
    out_path = os.path.join(os.environ.get("TEMP", "/tmp"), "a2_stress2_truncation.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

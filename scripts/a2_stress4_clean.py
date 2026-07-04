"""Stress test 4 v3: clean skew measurement on a fresh conversation.

The polluted conversation (30+ stale markers) produced wild skew variance.
This version creates a fresh conversation and measures skew there, to determine
whether the +5.1s tight cluster from Phase 0.5 was real or an artifact.

Also handles websocket keepalive by reconnecting per-batch instead of holding
one connection across long sleeps.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
import urllib.request

import websockets

CDP = 9222
BRIDGE = "http://127.0.0.1:8080"


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


def find_target_for_conv(conv_id):
    targets = list_targets()
    for t in targets:
        if t.get("type") == "page" and conv_id and conv_id in t.get("url", ""):
            return t
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com/c/" in t.get("url", ""):
            return t
    raise RuntimeError("no target")


def bridge_send_fresh(prompt: str) -> dict:
    """Fresh-chat send (no conversation_id) — creates a new conversation."""
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
            return {"ok": True, "latency": round(time.time() - t0, 2),
                    "content": p.get("choices", [{}])[0].get("message", {}).get("content", "")[:60],
                    "conversation_id": p.get("conversation_id", "")}
    except Exception as e:
        return {"ok": False, "latency": round(time.time() - t0, 2), "error": str(e)}


def bridge_send_to_conv(prompt: str, conv_id: str) -> dict:
    """Send to a specific conversation."""
    body = json.dumps({
        "model": "auto", "conversation_id": conv_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            p = json.loads(r.read())
            return {"ok": True, "latency": round(time.time() - t0, 2),
                    "content": p.get("choices", [{}])[0].get("message", {}).get("content", "")[:60]}
    except Exception as e:
        return {"ok": False, "latency": round(time.time() - t0, 2), "error": str(e)}


async def measure_skews_on_conv(conv_id: str, n: int = 8) -> list:
    """Run n instrumented sends on conv_id, capturing skew + uuid survival.
    Uses a fresh websocket per measurement to avoid keepalive issues."""
    target = find_target_for_conv(conv_id)
    ws_url = target["webSocketDebuggerUrl"]
    results = []

    # One persistent connection for the whole batch (batch is short, ~2-3 min)
    async with websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=60) as ws:
        nxt = [0]
        def nid():
            nxt[0] += 1; return nxt[0]

        await ws.send(json.dumps({"id": nid(), "method": "Network.enable",
                                  "params": {"maxPostDataSize": 4 * 1024 * 1024}}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if r.get("id"):
                break

        async def evaluate(expr, timeout_ms=15000):
            my_id = nid()
            await ws.send(json.dumps({"id": my_id, "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "awaitPromise": True,
                                                 "returnByValue": True, "timeout": timeout_ms}}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == my_id:
                    return r

        # Token (once)
        token_resp = await evaluate(
            '(async()=>{var r=await fetch("/api/auth/session");var j=await r.json();return j.accessToken||""})()',
            timeout_ms=10000)
        token = token_resp.get("result", {}).get("result", {}).get("value", "")

        for i in range(1, n + 1):
            marker = f"S4V3-{i}-{int(time.time())}"
            # Drain
            while True:
                try:
                    _ = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    break

            t_pre_wall = time.time()
            send_task = asyncio.create_task(asyncio.to_thread(bridge_send_to_conv, f"Reply with exactly: {marker}", conv_id))

            captured_uuid = None
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    if send_task.done() and time.time() > t_pre_wall + 8:
                        break
                    continue
                r = json.loads(raw)
                if r.get("method") == "Network.requestWillBeSent":
                    req_obj = (r.get("params") or {}).get("request", {}) or {}
                    url = req_obj.get("url", "")
                    if req_obj.get("method") == "POST" and url.rstrip("/").endswith("/f/conversation"):
                        pd = req_obj.get("postData")
                        if pd:
                            try:
                                parsed = json.loads(pd)
                                msgs = parsed.get("messages") or []
                                if msgs and msgs[0].get("id"):
                                    captured_uuid = msgs[0]["id"]
                            except Exception:
                                pass

            try:
                sr = await asyncio.wait_for(send_task, timeout=60)
                content = sr.get("content", "")
            except Exception as e:
                content = f"<err: {e}>"

            # Fetch mapping
            await asyncio.sleep(1.0)
            fetch_js = (
                "(async()=>{var r=await fetch('/backend-api/conversation/' + __C__ + '?offset=0&limit=50',"
                "{headers:{'Authorization':'Bearer ' + __T__}});"
                "if(!r.ok) return JSON.stringify({__status:r.status});"
                "var j=await r.json();return JSON.stringify(j)})()"
            ).replace("__C__", json.dumps(conv_id)).replace("__T__", json.dumps(token))
            map_resp = await evaluate(fetch_js, timeout_ms=20000)
            map_val = map_resp.get("result", {}).get("result", {}).get("value", "")
            mapping = json.loads(map_val) if map_val else {}
            m = mapping.get("mapping", {}) if isinstance(mapping, dict) else {}

            latest_user_ct = None
            latest_user_id = None
            best_t = -1
            for _nid, node in m.items():
                msg = node.get("message") or {}
                if (msg.get("author") or {}).get("role") == "user":
                    ct = msg.get("create_time") or 0
                    if ct > best_t:
                        best_t = ct
                        latest_user_ct = ct
                        latest_user_id = msg.get("id")

            skew = (latest_user_ct - t_pre_wall) if latest_user_ct else None
            uuid_survived = (captured_uuid == latest_user_id) if (captured_uuid and latest_user_id) else None
            results.append({
                "i": i, "marker": marker,
                "skew": round(skew, 3) if skew is not None else None,
                "uuid_captured": bool(captured_uuid),
                "uuid_survived": uuid_survived,
                "stale": content != marker,
                "content": content[:40],
                "latency": sr.get("latency") if isinstance(sr, dict) else None,
            })
            skew_str = f"{skew:.3f}" if skew is not None else "N/A"
            lat_str = str(sr.get('latency','?')) if isinstance(sr, dict) else '?'
            print(f"  {i}: skew={skew_str}  cap={'Y' if captured_uuid else 'N'}  "
                  f"surv={'Y' if uuid_survived else 'N'}  stale={'Y' if content != marker else 'N'}  "
                  f"lat={lat_str}")
            await asyncio.sleep(3.0)

    return results


async def main():
    print("=== Stress test 4 v3: clean skew on fresh conversation ===")
    print("\nStep 1: create a fresh conversation with one send...")
    init = bridge_send_fresh("Reply with exactly: INIT-FRESH-CONV")
    print(f"  fresh conv: {init.get('conversation_id','?')} (ok={init.get('ok')})")
    conv_id = init.get("conversation_id", "")
    if not conv_id:
        print("FAILED to create fresh conversation")
        return

    print(f"\nStep 2: measure 8 skews on fresh conv {conv_id}...")
    b1 = await measure_skews_on_conv(conv_id, n=8)

    skews = [r["skew"] for r in b1 if r["skew"] is not None]
    print(f"\n=== Batch results ===")
    if skews:
        print(f"n={len(skews)}  min={min(skews):.3f}  max={max(skews):.3f}  "
              f"mean={statistics.mean(skews):.3f}  std={statistics.stdev(skews) if len(skews)>1 else 0:.3f}")
        neg = [s for s in skews if s < 0]
        print(f"negative: {len(neg)}")
        if len(neg) == 0 and statistics.stdev(skews) < 1.0:
            print("VERDICT: skew stable on fresh conversation — Phase 0.5 finding holds.")
        else:
            print(f"VERDICT: skew variance (std={statistics.stdev(skews):.3f}) differs from Phase 0.5 (0.083s).")

    import os
    out_path = os.path.join(os.environ.get("TEMP", "/tmp"), "a2_stress4_v3_clean.json")
    with open(out_path, "w") as f:
        json.dump({"conv_id": conv_id, "batch": b1,
                   "skews": skews,
                   "mean": statistics.mean(skews) if skews else None,
                   "stdev": statistics.stdev(skews) if len(skews) > 1 else None}, f, indent=2, default=str)
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

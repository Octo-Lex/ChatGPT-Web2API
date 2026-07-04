"""A2 investigation — combined Phase 0.5 (skew) + Phase 1 follow-up (UUID capture).

Runs N instrumented sends. For each:
  - Records t_pre_wall before the bridge send.
  - Captures the outgoing /backend-api/f/conversation POST via CDP Network domain,
    recording (a) whether request.postData carries the body, (b) the client UUID
    extracted from messages[0].id.
  - After the send, fetches the mapping and records user.create_time, whether
    the UUID survived into a mapping node, and the skew delta.

Outputs a JSON report at the end with per-send rows + aggregate statistics.
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
CONV = "6a48625b-34a4-83ed-93ba-a7153c2e6295"
NUM_SAMPLES = 12


def list_targets() -> list[dict]:
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


def find_target() -> dict:
    targets = list_targets()
    # Prefer the target showing our conversation.
    for t in targets:
        if t.get("type") == "page" and CONV in t.get("url", ""):
            return t
    # Fallback: any chatgpt.com/c/ page.
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com/c/" in t.get("url", ""):
            return t
    raise RuntimeError("no chatgpt.com/c/ target found")


async def bridge_send(marker: str) -> dict:
    body = json.dumps({
        "model": "auto", "conversation_id": CONV,
        "messages": [{"role": "user", "content": f"Reply with exactly: {marker}"}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            p = json.loads(r.read())
            content = p.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"ok": True, "latency": round(time.time() - t0, 2), "content": content}
    except Exception as e:
        return {"ok": False, "latency": round(time.time() - t0, 2), "error": str(e)}


async def main(num_samples: int) -> None:
    target = find_target()
    print(f"target: {target['url'][:80]}")
    ws_url = target["webSocketDebuggerUrl"]

    rows: list[dict] = []

    async with websockets.connect(ws_url, max_size=None) as ws:
        nxt = [0]
        def nid() -> int:
            nxt[0] += 1
            return nxt[0]

        async def evaluate(expr: str, timeout_ms: int = 15000) -> dict:
            my_id = nid()
            await ws.send(json.dumps({"id": my_id, "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "awaitPromise": True,
                                                 "returnByValue": True, "timeout": timeout_ms}}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == my_id:
                    return r

        # Enable Network domain (with large post-data budget).
        await ws.send(json.dumps({"id": nid(), "method": "Network.enable",
                                  "params": {"maxPostDataSize": 1024 * 1024}}))
        # Drain the ACK.
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if r.get("id"):
                break

        # Get token (for our mapping fetches).
        token_resp = await evaluate(
            '(async()=>{var r=await fetch("/api/auth/session");var j=await r.json();return j.accessToken||""})()',
            timeout_ms=10000,
        )
        token = token_resp.get("result", {}).get("result", {}).get("value", "")
        print(f"access token: {'OK' if token else 'MISSING'} ({len(token)} chars)")

        # Build the mapping-fetch JS (with placeholders to avoid brace hell).
        def fetch_mapping_js() -> str:
            return (
                "(async()=>{"
                "var r=await fetch('/backend-api/conversation/' + __C__ + '?offset=0&limit=50',"
                "{headers:{'Authorization':'Bearer ' + __T__}});"
                "if(!r.ok) return JSON.stringify({__status:r.status});"
                "var j=await r.json();return JSON.stringify(j)"
                "})()"
            ).replace("__C__", json.dumps(CONV)).replace("__T__", json.dumps(token))

        for i in range(1, num_samples + 1):
            marker = f"COMBINED-{i}-{int(time.time())}"
            print(f"\n--- sample {i}/{num_samples}: marker={marker} ---")

            # Drain any pending network events before the send window.
            while True:
                try:
                    _ = await asyncio.wait_for(ws.recv(), timeout=0.1)
                except asyncio.TimeoutError:
                    break

            # Fire send in background; collect Network events meanwhile.
            t_pre_wall = time.time()
            send_task = asyncio.create_task(bridge_send(marker))

            captured_post_body = None
            captured_client_uuid = None
            network_event_count = 0
            deadline = time.time() + 120
            while time.time() < deadline:
                if send_task.done() and (captured_post_body is not None or time.time() > t_pre_wall + 8):
                    # Give a short tail to catch the post-send fetches, then stop.
                    if time.time() > t_pre_wall + 8:
                        break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
                except asyncio.TimeoutError:
                    if send_task.done() and time.time() > t_pre_wall + 8:
                        break
                    continue
                r = json.loads(raw)
                m = r.get("method", "")
                if not m.startswith("Network."):
                    continue
                p = r.get("params", {})
                req = p.get("request", {}) or {}
                url = req.get("url", "")
                if m == "Network.requestWillBeSent" and "/backend-api/f/conversation" in url:
                    network_event_count += 1
                    if req.get("method") == "POST" and url.rstrip("/").endswith("/f/conversation"):
                        post_data = req.get("postData")
                        if post_data:
                            captured_post_body = post_data
                            try:
                                parsed = json.loads(post_data)
                                msgs = parsed.get("messages") or []
                                if msgs and msgs[0].get("id"):
                                    captured_client_uuid = msgs[0]["id"]
                            except Exception:
                                pass

            send_result = await send_task

            # Fetch post-send mapping; extract user.create_time and check UUID survival.
            await asyncio.sleep(1.5)  # let backend settle
            t_post_wall = time.time()
            map_resp = await evaluate(fetch_mapping_js(), timeout_ms=20000)
            map_val = map_resp.get("result", {}).get("result", {}).get("value", "")
            mapping = json.loads(map_val) if map_val else {}
            m = mapping.get("mapping", {}) if isinstance(mapping, dict) else {}

            latest_user_ct = None
            latest_user_id = None
            latest_user_text = None
            uuid_survived = None
            uuid_node_role = None
            best_user_t = -1
            for nid_str, node in m.items():
                msg = node.get("message") or {}
                if (msg.get("author") or {}).get("role") != "user":
                    continue
                ct = msg.get("create_time") or 0
                if ct > best_user_t:
                    best_user_t = ct
                    latest_user_ct = ct
                    latest_user_id = msg.get("id")
                    parts = (msg.get("content") or {}).get("parts") or []
                    latest_user_text = "\n".join(str(p) for p in parts if isinstance(p, str))[:80]
            if captured_client_uuid and latest_user_id:
                uuid_survived = (captured_client_uuid == latest_user_id)
                if not uuid_survived:
                    # Check if it appears anywhere (e.g., as a parent linkage).
                    for _nid, node in m.items():
                        if captured_client_uuid in str(node):
                            uuid_node_role = (node.get("message") or {}).get("author", {}).get("role")
                            break

            skew = None
            if latest_user_ct:
                skew = round(latest_user_ct - t_pre_wall, 3)

            row = {
                "sample": i,
                "marker": marker,
                "t_pre_wall": t_pre_wall,
                "t_post_wall": t_post_wall,
                "send_result": send_result,
                "is_stale": send_result.get("content") != marker,
                "network_event_count": network_event_count,
                "post_body_captured": captured_post_body is not None,
                "client_uuid_observed": captured_client_uuid,
                "latest_user_create_time": latest_user_ct,
                "latest_user_node_id": latest_user_id,
                "latest_user_text": latest_user_text,
                "uuid_survived_as_user_node_id": uuid_survived,
                "uuid_found_elsewhere_role": uuid_node_role,
                "skew_user_ct_minus_t_pre_wall": skew,
            }
            rows.append(row)
            print(f"  send returned : {send_result.get('content','')[:60]!r}")
            print(f"  stale         : {row['is_stale']}")
            print(f"  post_body cap : {row['post_body_captured']}")
            print(f"  client_uuid   : {captured_client_uuid}")
            print(f"  user_node.id  : {latest_user_id}")
            print(f"  uuid survived : {uuid_survived} (elsewhere_role={uuid_node_role})")
            print(f"  skew (s)      : {skew}")

            # Pace sends to avoid rate-limiting.
            await asyncio.sleep(3.0)

    # Aggregate statistics.
    print("\n" + "=" * 70)
    print("AGGREGATE REPORT")
    print("=" * 70)
    n = len(rows)
    stale_count = sum(1 for r in rows if r["is_stale"])
    body_capture_rate = sum(1 for r in rows if r["post_body_captured"]) / n if n else 0
    uuid_survival_rate = sum(1 for r in rows if r["uuid_survived_as_user_node_id"]) / n if n else 0
    skews = [r["skew_user_ct_minus_t_pre_wall"] for r in rows if r["skew_user_ct_minus_t_pre_wall"] is not None]
    print(f"samples                   : {n}")
    print(f"stale returns             : {stale_count}/{n}")
    print(f"post body capture rate    : {body_capture_rate*100:.1f}%")
    print(f"uuid survival rate        : {uuid_survival_rate*100:.1f}%")
    if skews:
        print(f"skew samples              : {len(skews)}")
        print(f"skew min / max            : {min(skews):.3f} / {max(skews):.3f}")
        print(f"skew mean / stdev         : {statistics.mean(skews):.3f} / {statistics.stdev(skews):.3f if len(skews) > 1 else 0:.3f}")
        s = sorted(skews)
        if len(s) >= 4:
            p1 = s[max(0, int(0.01 * len(s)) - 1)]
            p99 = s[min(len(s) - 1, int(0.99 * len(s)))]
            print(f"skew p1 / p99            : {p1:.3f} / {p99:.3f}")
        # Recommended tolerance per ChatGPT's Phase 0.5 formula: max(2.0, abs(p1) + safety_margin)
        if skews:
            min_abs = min(abs(x) for x in skews)
            recommended = max(2.0, min_abs + 2.0)  # +2s safety margin
            recommended = min(recommended, 10.0)  # cap
            print(f"recommended SKEW_TOLERANCE: {recommended:.1f}s (capped at 10)")

    # Save full rows.
    with open("/tmp/a2_investigation_combined.json", "w") as f:
        json.dump({"rows": rows, "num_samples": n,
                   "stale_count": stale_count, "body_capture_rate": body_capture_rate,
                   "uuid_survival_rate": uuid_survival_rate,
                   "skews": skews}, f, indent=2, default=str)
    print("\nfull rows saved to /tmp/a2_investigation_combined.json")


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else NUM_SAMPLES
    asyncio.run(main(n))

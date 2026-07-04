"""A2 investigation — Phase 0.5 (skew) + Phase 1 (identity spike).

Pure observational CDP instrumentation around ONE bridge send.
- Phase 0.5: records t_pre_wall vs backend user.create_time to derive skew.
- Phase 1: captures outgoing /backend-api/conversation/* POST bodies/headers
  during the send to discover whether the protocol carries a usable client
  message ID / idempotency key / parent_message_id that survives into the
  backend mapping.

No DOM mutation. No send interception. Listens to network traffic the bridge
already generates, then fetches the mapping for cross-reference.

Usage:
    python scripts/a2_investigation_spike.py --label RUN-1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request

import websockets


CDP_PORT = 9222
BRIDGE = "http://127.0.0.1:8080"
CONVERSATION_ID = "6a48625b-34a4-83ed-93ba-a7153c2e6295"
CHATGPT_TAB_URL_PREFIX = "chatgpt.com/c/"


def list_targets() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3) as r:
        return json.loads(r.read())


def find_chatgpt_target(conv_id: str) -> dict | None:
    """Find the page target showing the conversation we're about to send to."""
    targets = list_targets()
    needle = f"/c/{conv_id}"
    for t in targets:
        if t.get("type") == "page" and needle in t.get("url", ""):
            return t
    # Fallback: any chatgpt.com/c/ page
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com/c/" in t.get("url", ""):
            return t
    return None


async def cdp_eval(ws: websockets.WebSocketClientProtocol, expr: str, timeout_ms: int = 15000) -> dict:
    """Runtime.evaluate — returns the full result dict."""
    msg = {
        "id": _next_id(),
        "method": "Runtime.evaluate",
        "params": {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
            "timeout": timeout_ms,
        },
    }
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        # Skip Network.* events — we're collecting those separately.
        if resp.get("id") == msg["id"]:
            return resp


_id_counter = [0]
def _next_id() -> int:
    _id_counter[0] += 1
    return _id_counter[0]


async def fetch_mapping_via_target(
    ws: websockets.WebSocketClientProtocol, conv_id: str, token: str
) -> dict:
    """Fetch /backend-api/conversation/{id}?offset=0&limit=50 inside the page.

    Returns the parsed mapping dict (full, unprojected — Phase 5 will define the
    projection; for Phase 0.5/1 we want the raw shape).
    """
    expr = f"""
    (async () => {{
      try {{
        var r = await fetch('/backend-api/conversation/' + {conv_id!r} + '?offset=0&limit=50', {{
          headers: {{'Authorization': 'Bearer ' + {token!r}}}
        }});
        if (!r.ok) return JSON.stringify({{__status: r.status}});
        var conv = await r.json();
        return JSON.stringify(conv);
      }} catch(e) {{ return JSON.stringify({{__error: String(e)}}); }}
    }})()
    """
    resp = await cdp_eval(ws, expr, timeout_ms=20000)
    val = resp.get("result", {}).get("result", {}).get("value", "")
    if not val:
        return {"__error": "empty"}
    return json.loads(val)


async def get_access_token(ws: websockets.WebSocketClientProtocol) -> str:
    """Read the access token the same way the bridge does."""
    expr = """
    (async () => {
      try {
        var r = await fetch('/api/auth/session');
        var j = await r.json();
        return j.accessToken || '';
      } catch(e) { return ''; }
    })()
    """
    resp = await cdp_eval(ws, expr, timeout_ms=10000)
    return resp.get("result", {}).get("result", {}).get("value", "") or ""


async def bridge_send(marker: str) -> dict:
    """Send via the bridge's REST API (the path under investigation)."""
    import urllib.request as ur

    body = json.dumps({
        "model": "auto",
        "conversation_id": CONVERSATION_ID,
        "messages": [{"role": "user", "content": f"Reply with exactly this marker and nothing else: {marker}"}],
        "stream": False,
    }).encode()
    req = ur.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                     headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with ur.urlopen(req, timeout=120) as r:
            payload = json.loads(r.read())
            return {"ok": True, "latency": time.time() - t0, "content": payload.get("choices", [{}])[0].get("message", {}).get("content", "")}
    except Exception as e:
        return {"ok": False, "latency": time.time() - t0, "error": str(e)}


def analyze_mapping(mapping: dict, sent_marker: str) -> dict:
    """Phase 0.5 + Phase 1 analysis on a raw backend mapping.

    Extract: latest user node create_time, latest assistant node create_time,
    whether the sent marker appears in any user node's text, all observed
    id-like fields on the latest user/assistant nodes.
    """
    out = {
        "node_count": 0,
        "user_nodes": [],
        "assistant_nodes": [],
        "latest_user_create_time": None,
        "latest_user_node_id": None,
        "latest_user_text": None,
        "latest_user_id_fields": {},
        "latest_assistant_create_time": None,
        "latest_assistant_node_id": None,
        "latest_assistant_text": None,
        "latest_assistant_id_fields": {},
        "marker_in_latest_user": None,
    }
    if not isinstance(mapping, dict):
        out["__error"] = f"mapping not dict: {type(mapping)}"
        return out
    m = mapping.get("mapping", {})
    out["node_count"] = len(m)

    best_user_t = -1
    best_asst_t = -1
    for nid, node in m.items():
        msg = node.get("message") or {}
        author = msg.get("author") or {}
        role = author.get("role")
        ct = msg.get("create_time") or 0
        out_node = {"id": nid, "role": role, "create_time": ct,
                    "end_turn": msg.get("end_turn"),
                    "content_type": (msg.get("content") or {}).get("content_type"),
                    "id_fields": {}}
        # Capture every id-like field at message level (Phase 1 spike)
        for k, v in msg.items():
            if "id" in k.lower() or k in ("parent", "children"):
                out_node["id_fields"][k] = v
        if role == "user":
            parts = (msg.get("content") or {}).get("parts") or []
            text = "\n".join(str(p) for p in parts if isinstance(p, str))
            out["user_nodes"].append({**out_node, "text": text[:120]})
            if ct > best_user_t:
                best_user_t = ct
                out["latest_user_create_time"] = ct
                out["latest_user_node_id"] = nid
                out["latest_user_text"] = text[:200]
                out["latest_user_id_fields"] = out_node["id_fields"]
                out["marker_in_latest_user"] = sent_marker in text
        elif role == "assistant":
            parts = (msg.get("content") or {}).get("parts") or []
            text = "\n".join(str(p) for p in parts if isinstance(p, str))
            out["assistant_nodes"].append({**out_node, "text": text[:120]})
            if ct > best_asst_t:
                best_asst_t = ct
                out["latest_assistant_create_time"] = ct
                out["latest_assistant_node_id"] = nid
                out["latest_assistant_text"] = text[:200]
                out["latest_assistant_id_fields"] = out_node["id_fields"]
    return out


async def main(label: str) -> None:
    target = find_chatgpt_target(CONVERSATION_ID)
    if not target:
        print(json.dumps({"error": "no chatgpt.com/c/ target found"}, indent=2))
        return
    ws_url = target["webSocketDebuggerUrl"]
    print(f"[spike] attached to target: {target['url'][:80]}")

    sent_marker = f"SPIKE-{label}-{int(time.time())}"
    captured_network = []  # Phase 1 captures

    async with websockets.connect(ws_url, max_size=None) as ws:
        # Enable Network domain to observe outgoing requests during the send.
        await ws.send(json.dumps({"id": _next_id(), "method": "Network.enable"}))

        # Read the access token (for our own mapping fetch) before the send.
        token = await get_access_token(ws)
        print(f"[spike] access token: {'OK' if token else 'MISSING'} ({len(token)} chars)")

        # Capture pre-send mapping baseline (latest user/assistant BEFORE send).
        t_pre_wall = time.time()
        pre_mapping_raw = await fetch_mapping_via_target(ws, CONVERSATION_ID, token)
        pre_analysis = analyze_mapping(pre_mapping_raw, sent_marker)
        print(f"[spike] pre-send: latest_user_node_id={pre_analysis['latest_user_node_id']}, "
              f"latest_user_ct={pre_analysis['latest_user_create_time']}, "
              f"latest_asst_node_id={pre_analysis['latest_assistant_node_id']}")

        # Drain any Network events accumulated so far.
        while True:
            try:
                _ = await asyncio.wait_for(ws.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                break

        # Fire the bridge send in a background task; meanwhile collect Network events.
        send_task = asyncio.create_task(bridge_send(sent_marker))
        network_events = []
        deadline = time.time() + 120
        while time.time() < deadline:
            # Check send completion.
            if send_task.done():
                # Drain a bit more network then break.
                pass
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                if send_task.done():
                    break
                continue
            evt = json.loads(raw)
            method = evt.get("method", "")
            if not method.startswith("Network."):
                continue
            params = evt.get("params", {})
            req = params.get("request", {})
            url = req.get("url", "")
            # Only capture backend-api/conversation traffic (the send path).
            if "/backend-api/conversation/" in url:
                network_events.append({
                    "method": method,
                    "request_id": params.get("requestId"),
                    "url": url,
                    "method_verb": req.get("method"),
                    "has_post_data": req.get("hasPostData"),
                    "post_data": req.get("postData"),
                    "headers": req.get("headers"),
                    # response fields for responseReceived
                    "response_status": (params.get("response") or {}).get("status"),
                    "response_headers": (params.get("response") or {}).get("headers"),
                })

        send_result = await send_task

        # Fetch post-send mapping (give backend 2s to settle).
        await asyncio.sleep(2.0)
        t_post_wall = time.time()
        post_mapping_raw = await fetch_mapping_via_target(ws, CONVERSATION_ID, token)
        post_analysis = analyze_mapping(post_mapping_raw, sent_marker)

    # Phase 0.5: skew computation
    skew_user = None
    if post_analysis.get("latest_user_create_time") and post_analysis.get("marker_in_latest_user"):
        skew_user = post_analysis["latest_user_create_time"] - t_pre_wall

    report = {
        "label": label,
        "sent_marker": sent_marker,
        "conversation_id": CONVERSATION_ID,
        "t_pre_wall": t_pre_wall,
        "t_post_wall": t_post_wall,
        "send_result": send_result,
        # Phase 1: network observations
        "network_event_count": len(network_events),
        "network_events": network_events,
        # Phase 0.5: skew
        "skew_user_create_minus_t_pre_wall": skew_user,
        "pre_send_mapping_summary": {
            "node_count": pre_analysis["node_count"],
            "latest_user_node_id": pre_analysis["latest_user_node_id"],
            "latest_user_create_time": pre_analysis["latest_user_create_time"],
            "latest_assistant_node_id": pre_analysis["latest_assistant_node_id"],
            "latest_assistant_create_time": pre_analysis["latest_assistant_create_time"],
        },
        "post_send_mapping_summary": {
            "node_count": post_analysis["node_count"],
            "latest_user_node_id": post_analysis["latest_user_node_id"],
            "latest_user_create_time": post_analysis["latest_user_create_time"],
            "latest_user_text": post_analysis["latest_user_text"],
            "latest_user_id_fields": post_analysis["latest_user_id_fields"],
            "marker_in_latest_user": post_analysis["marker_in_latest_user"],
            "latest_assistant_node_id": post_analysis["latest_assistant_node_id"],
            "latest_assistant_create_time": post_analysis["latest_assistant_create_time"],
            "latest_assistant_text": post_analysis["latest_assistant_text"],
            "latest_assistant_id_fields": post_analysis["latest_assistant_id_fields"],
        },
    }

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="RUN-1")
    args = p.parse_args()
    asyncio.run(main(args.label))

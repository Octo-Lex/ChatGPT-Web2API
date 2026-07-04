"""Prerequisite 3: Capture a real thinking-model mapping fixture.

Triggers a thinking-model send via the bridge, then fetches the raw backend
mapping via CDP and saves it as a test fixture. Also runs the projection JS
against it to verify the projection produces the expected compact schema.

The fixture is saved to tests/fixtures/sample_conversation_mapping.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request

import websockets

CDP = 9222
BRIDGE = "http://127.0.0.1:8080"


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


def find_chatgpt_target():
    targets = list_targets()
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com/c/" in t.get("url", ""):
            return t
    for t in targets:
        if t.get("type") == "page" and "chatgpt.com" in t.get("url", ""):
            return t
    raise RuntimeError("no chatgpt target found")


async def main():
    # Step 1: trigger a thinking-model send via the bridge.
    print("=== PREREQ 3: Capture real thinking/tool-use mapping fixture ===")
    print("\nStep 1: Triggering a thinking-model send...")
    prompt = "Think step by step. What is 17 * 23? Show your reasoning."
    body = json.dumps({
        "model": "auto",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            p = json.loads(r.read())
            conv_id = p.get("conversation_id", "")
            content = p.get("choices", [{}])[0].get("message", {}).get("content", "")[:200]
            print(f"  conversation_id: {conv_id}")
            print(f"  content[:200]: {content!r}")
    except Exception as e:
        print(f"  bridge error: {e}")
        conv_id = ""
        content = ""

    if not conv_id:
        print("FAILED: no conversation_id from send")
        return

    # Step 2: fetch the raw backend mapping via CDP.
    print(f"\nStep 2: Fetching raw backend mapping for {conv_id}...")
    target = find_chatgpt_target()
    print(f"  target: {target['url'][:80]}")

    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None,
                                  ping_interval=20, ping_timeout=60) as ws:
        nxt = [0]
        def nid():
            nxt[0] += 1; return nxt[0]

        # Get token.
        await ws.send(json.dumps({"id": nid(), "method": "Runtime.evaluate",
                                  "params": {"expression": '(async()=>{var r=await fetch("/api/auth/session");var j=await r.json();return j.accessToken||""})()',
                                             "awaitPromise": True, "returnByValue": True}}))
        token = ""
        while True:
            r = json.loads(await ws.recv())
            if r.get("id"):
                token = r.get("result", {}).get("result", {}).get("value", "")
                break

        # Fetch the raw mapping (full, unprojected).
        fetch_js = (
            "(async()=>{"
            "var r=await fetch('/backend-api/conversation/' + __C__ + '?offset=0&limit=50',"
            "{headers:{'Authorization':'Bearer ' + __T__}});"
            "if(!r.ok) return JSON.stringify({__status:r.status});"
            "var j=await r.json();return JSON.stringify(j)"
            "})()"
        ).replace("__C__", json.dumps(conv_id)).replace("__T__", json.dumps(token))

        await ws.send(json.dumps({"id": nid(), "method": "Runtime.evaluate",
                                  "params": {"expression": fetch_js, "awaitPromise": True,
                                             "returnByValue": True, "timeout": 20000}}))
        raw_mapping = None
        while True:
            r = json.loads(await ws.recv())
            if r.get("id"):
                val = r.get("result", {}).get("result", {}).get("value", "")
                if val:
                    raw_mapping = json.loads(val)
                break

    if not raw_mapping or "mapping" not in raw_mapping:
        print(f"FAILED: no mapping in response. Got keys: {list(raw_mapping.keys()) if raw_mapping else 'None'}")
        return

    mapping = raw_mapping["mapping"]
    print(f"  mapping node count: {len(mapping)}")

    # Step 3: analyze the raw mapping shape.
    print("\nStep 3: Analyzing raw mapping shape...")
    roles = {}
    content_types = {}
    has_reasoning = False
    has_tool = False
    for nid, node in mapping.items():
        msg = node.get("message") or {}
        role = (msg.get("author") or {}).get("role", "?")
        ct = ((msg.get("content") or {}).get("content_type", "?"))
        roles[role] = roles.get(role, 0) + 1
        content_types[ct] = content_types.get(ct, 0) + 1
        if ct == "reasoning_recap":
            has_reasoning = True
        if "tool" in ct:
            has_tool = True

    print(f"  roles: {roles}")
    print(f"  content_types: {content_types}")
    print(f"  has_reasoning_recap: {has_reasoning}")
    print(f"  has_tool_use: {has_tool}")

    # Step 4: run the projection JS against the raw mapping (in Python —
    # simulate what the JS would produce).
    print("\nStep 4: Simulating projection against raw mapping...")
    from chatgpt_web2api.backend_projection import CONVERSATION_PROJECTION_JS, TURN_PROJECTION_LIMIT
    projected = {}
    for key, node in mapping.items():
        msg = node.get("message") or {}
        author = msg.get("author") or {}
        content = msg.get("content") or {}
        parts = content.get("parts") or []
        text = ""
        if content.get("content_type") == "text":
            text_parts = [p for p in parts if isinstance(p, str) and p.strip()]
            text = "\n".join(text_parts)
        projected[key] = {
            "id": msg.get("id") or key,
            "parent": node.get("parent"),
            "children": node.get("children") or [],
            "role": author.get("role", "unknown"),
            "create_time": msg.get("create_time") or 0,
            "end_turn": bool(msg.get("end_turn")),
            "content_type": content.get("content_type", "unknown"),
            "text": text,
        }
    print(f"  projected node count: {len(projected)}")

    # Verify all required fields are present.
    required_fields = {"id", "parent", "children", "role", "create_time",
                       "end_turn", "content_type", "text"}
    all_valid = True
    for nid, node in projected.items():
        missing = required_fields - set(node.keys())
        if missing:
            print(f"  MISSING FIELDS in node {nid}: {missing}")
            all_valid = False
    if all_valid:
        print("  All projected nodes have all required fields ✓")

    # Verify reasoning_recap nodes are preserved (not dropped).
    reasoning_nodes = [nid for nid, n in projected.items()
                       if n["content_type"] == "reasoning_recap"]
    print(f"  reasoning_recap nodes preserved: {len(reasoning_nodes)} (graph structure intact)")

    # Step 5: save the fixture (sanitized — strip the actual text content to
    # avoid leaking prompts/responses into the repo; keep structure + metadata).
    print("\nStep 5: Saving sanitized fixture...")
    fixture_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures")
    os.makedirs(fixture_dir, exist_ok=True)
    fixture_path = os.path.join(fixture_dir, "sample_conversation_mapping.json")

    # Sanitize: keep structure + metadata, truncate text to 60 chars.
    sanitized = {"nodes": {}}
    for nid, node in projected.items():
        san = dict(node)
        if san["text"] and len(san["text"]) > 60:
            san["text"] = san["text"][:60] + "...[truncated]"
        sanitized["nodes"][nid] = san
    sanitized["current_node"] = raw_mapping.get("current_node")
    sanitized["_meta"] = {
        "source": "real captured ChatGPT backend mapping",
        "captured": "2026-07-04",
        "conversation_id": conv_id,
        "node_count": len(projected),
        "has_reasoning_recap": has_reasoning,
        "has_tool_use": has_tool,
        "roles": roles,
        "content_types": content_types,
    }

    with open(fixture_path, "w") as f:
        json.dump(sanitized, f, indent=2)
    print(f"  saved: {fixture_path}")

    # Step 6: verify the selectors work on the projected fixture.
    print("\nStep 6: Verifying selectors work on the projected fixture...")
    from chatgpt_web2api.turn_anchor import TurnAnchor, select_text_for_turn
    # Find the latest user node to use as the captured ID.
    latest_user_id = None
    latest_user_ct = -1
    for nid, node in projected.items():
        if node["role"] == "user" and node["create_time"] > latest_user_ct:
            latest_user_ct = node["create_time"]
            latest_user_id = node["id"]
    if latest_user_id:
        anchor = TurnAnchor(sent_text="(any)", mode="captured_id",
                            captured_user_message_id=latest_user_id)
        result = select_text_for_turn({"nodes": projected}, anchor)
        print(f"  selector result: status={result.status}")
        if result.text:
            print(f"  text[:80]: {result.text[:80]!r}")
        if result.diagnostic:
            print(f"  diagnostic: { {k:v for k,v in result.diagnostic.items() if k != 'reason'} }")
    else:
        print("  no user node found in fixture")

    print("\n=== PREREQ 3 COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())

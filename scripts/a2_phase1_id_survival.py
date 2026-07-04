"""Phase 1 verdict: does the client-generated message UUID from the
/backend-api/f/conversation POST body survive into the backend mapping?"""
import asyncio, json, urllib.request, websockets

CDP = 9222
CONV = "6a48625b-34a4-83ed-93ba-a7153c2e6295"
CLIENT_MSG_ID = "1596fd21-7868-4e38-8f49-be7ae7cb457a"  # from captured POST body


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


async def main():
    targets = list_targets()
    tgt = next(t for t in targets if t["type"] == "page" and CONV in t.get("url", ""))
    print(f"attached: {tgt['url'][:80]}")

    async with websockets.connect(tgt["webSocketDebuggerUrl"], max_size=None) as ws:
        # Get token
        token_expr = (
            '(async()=>{var r=await fetch("/api/auth/session");'
            'var j=await r.json();return j.accessToken||""})()'
        )
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": token_expr, "awaitPromise": True, "returnByValue": True}}))
        token = ""
        while True:
            r = json.loads(await ws.recv())
            if r.get("id") == 1:
                token = r["result"]["result"]["value"]
                break

        # Build the fetch JS with placeholder tokens (avoids brace-escaping issues)
        fetch_js = (
            "(async()=>{"
            "var r=await fetch('/backend-api/conversation/' + __CONV__ + '?offset=0&limit=50',"
            "{headers:{'Authorization':'Bearer ' + __TOKEN__}});"
            "if(!r.ok) return JSON.stringify({__status:r.status});"
            "var j=await r.json();return JSON.stringify(j)"
            "})()"
        ).replace("__CONV__", json.dumps(CONV)).replace("__TOKEN__", json.dumps(token))

        await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                  "params": {"expression": fetch_js, "awaitPromise": True, "returnByValue": True, "timeout": 20000}}))
        mapping = None
        while True:
            r = json.loads(await ws.recv())
            if r.get("id") == 2:
                result = r.get("result", {})
                if "exceptionDetails" in result:
                    print(f"JS threw: {result['exceptionDetails']}")
                    print(f"expression was:\n{fetch_js}")
                    return
                val = result.get("result", {}).get("value")
                if val is None:
                    print(f"no value returned. full result: {json.dumps(result, default=str)[:500]}")
                    return
                mapping = json.loads(val)
                break

    m = mapping.get("mapping", {})
    print(f"mapping node count: {len(m)}")
    print(f"searching for CLIENT_MSG_ID: {CLIENT_MSG_ID}")
    print()

    found_node = None
    for nid, node in m.items():
        if CLIENT_MSG_ID in str(node):
            found_node = (nid, node)
            break

    if found_node:
        nid, node = found_node
        msg = node.get("message") or {}
        print(f"*** CLIENT_MSG_ID FOUND in mapping node {nid} ***")
        print(f"  mapping_key (nid)   : {nid}")
        print(f"  message.id          : {msg.get('id')}")
        print(f"  author.role         : {(msg.get('author') or {}).get('role')}")
        print(f"  create_time         : {msg.get('create_time')}")
        print(f"  end_turn            : {msg.get('end_turn')}")
        print()
        # CRITICAL: where exactly does CLIENT_MSG_ID appear in this node?
        # Search every field for the ID to understand the linkage.
        print("  Fields containing CLIENT_MSG_ID:")
        def find_id(obj, path=""):
            hits = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    hits.extend(find_id(v, f"{path}.{k}"))
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    hits.extend(find_id(v, f"{path}[{i}]"))
            elif isinstance(obj, str) and CLIENT_MSG_ID in obj:
                hits.append((path, obj[:80]))
            return hits
        for path, val in find_id(node):
            print(f"    {path} = {val!r}")
        print()
        # Also check: is there a USER node whose message.id == CLIENT_MSG_ID?
        user_match = None
        for nid2, node2 in m.items():
            msg2 = node2.get("message") or {}
            if (msg2.get("author") or {}).get("role") == "user" and msg2.get("id") == CLIENT_MSG_ID:
                user_match = nid2
                break
        if user_match:
            print(f"  USER node with message.id == CLIENT_MSG_ID: {user_match}")
            print("VERDICT: STRONG SUCCESS — user node's message.id == client UUID.")
            print("A2 can correlate the user turn by observed client UUID directly.")
        else:
            print(f"  No USER node has message.id == CLIENT_MSG_ID.")
            print(f"  The client UUID appears as a parent_message_id / linkage field,")
            print(f"  NOT as a user node's primary id.")
            print()
            print("VERDICT: PARTIAL SUCCESS — client UUID survives as a linkage reference")
            print("(parent_message_id of the assistant response), not as the user node's id.")
            print("A2 can still use it: find the assistant node whose parent_message_id matches")
            print("the observed client UUID — that IS the correlated assistant response.")
    else:
        print("CLIENT_MSG_ID NOT FOUND in mapping.")
        print()
        print("All user nodes in mapping:")
        for nid, node in m.items():
            msg = node.get("message") or {}
            if (msg.get("author") or {}).get("role") == "user":
                ct = msg.get("create_time")
                parts = (msg.get("content") or {}).get("parts") or []
                txt = "\n".join(str(p) for p in parts if isinstance(p, str))[:60]
                print(f"  mapping_key={nid!r}  msg.id={msg.get('id')!r}  ct={ct}  text={txt!r}")
        print()
        print("VERDICT: FAILURE — UUID does not survive. Dual-anchor remains primary.")


asyncio.run(main())

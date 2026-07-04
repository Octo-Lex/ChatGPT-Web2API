"""Phase 3 spike: accessibility-tree completion oracle.

Question: can the a11y tree provide a more stable completion signal than the
current DOM heuristics (html_len > 50, has_action)?

Method:
  1. Attach to the ChatGPT tab.
  2. Take a baseline a11y snapshot when NO generation is in flight.
  3. Fire a bridge send.
  4. Take a11y snapshots DURING generation (every ~1s) and AFTER completion.
  5. Compare: do any a11y nodes/roles/states reliably indicate
     "generating" vs "done"?

This spike does NOT modify A2's design. It informs whether a future
completion-oracle workstream is viable.
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
    raise RuntimeError("no chatgpt target")


async def get_a11y_tree(ws, nxt):
    """Capture the accessibility tree via CDP Accessibility.getFullAXTree."""
    my_id = nxt[0] + 1
    nxt[0] = my_id
    await ws.send(json.dumps({"id": my_id, "method": "Accessibility.getFullAXTree"}))
    while True:
        r = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if r.get("id") == my_id:
            return r.get("result", {}).get("nodes", [])


def summarize_a11y(nodes):
    """Reduce a full a11y tree to a compact summary focused on completion signals."""
    # Count roles, look for live regions, "generating"/"loading" states,
    # and any node whose value/description mentions generation.
    role_counts = {}
    live_regions = []
    gen_markers = []
    action_markers = []
    for n in nodes:
        role = n.get("role", {}).get("value", "?")
        role_counts[role] = role_counts.get(role, 0) + 1
        # Live regions (polite/assertive) are the standard a11y "streaming" signal.
        props = {p.get("name", ""): p.get("value", {}).get("value") for p in n.get("properties", [])}
        if props.get("live") in ("polite", "assertive"):
            live_regions.append({
                "role": role,
                "name": (n.get("name") or {}).get("value", "")[:60],
                "live": props.get("live"),
            })
        # Look for explicit generation/loading/done markers in name/description/value.
        name_val = (n.get("name") or {}).get("value", "") or ""
        desc_val = (n.get("description") or {}).get("value", "") or ""
        value_val = (n.get("value") or {}).get("value", "") or ""
        combined = f"{name_val} {desc_val} {value_val}".lower()
        if any(k in combined for k in ("generating", "streaming", "thinking", "loading", "stop generating", "screenshot")):
            gen_markers.append({"role": role, "name": name_val[:60], "desc": desc_val[:60]})
        if any(k in combined for k in ("copy", "regenerate", "edit", "good response", "bad response")):
            action_markers.append({"role": role, "name": name_val[:60]})
    return {
        "node_count": len(nodes),
        "role_counts_top10": dict(sorted(role_counts.items(), key=lambda x: -x[1])[:10]),
        "live_region_count": len(live_regions),
        "live_regions_sample": live_regions[:5],
        "gen_markers": gen_markers[:8],
        "action_markers_count": len(action_markers),
        "action_markers_sample": action_markers[:5],
    }


async def main():
    target = find_target()
    print(f"target: {target['url'][:80]}")
    ws_url = target["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=None) as ws:
        nxt = [0]

        # Enable Accessibility domain
        nxt[0] += 1
        await ws.send(json.dumps({"id": nxt[0], "method": "Accessibility.enable"}))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if r.get("id"):
                break

        print("\n=== BASELINE (no generation in flight) ===")
        baseline_nodes = await get_a11y_tree(ws, nxt)
        baseline_summary = summarize_a11y(baseline_nodes)
        print(json.dumps(baseline_summary, indent=2, default=str)[:1200])

        # Fire send and sample during generation
        marker = f"A11Y-{int(time.time())}"
        print(f"\n=== FIRING SEND: {marker} ===")
        body = json.dumps({"model": "auto", "conversation_id": CONV,
                           "messages": [{"role": "user", "content": f"Write a 4-sentence paragraph about oceans. Then stop. Marker: {marker}"}],
                           "stream": False}).encode()
        req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        send_task = asyncio.create_task(asyncio.to_thread(urllib.request.urlopen, req, timeout=120))

        # Sample during generation
        during_summaries = []
        for sample_i in range(1, 6):
            await asyncio.sleep(1.0)
            try:
                nodes = await get_a11y_tree(ws, nxt)
                s = summarize_a11y(nodes)
                s["sample"] = sample_i
                during_summaries.append(s)
                print(f"\n--- DURING sample {sample_i} (t+{sample_i}s) ---")
                print(f"  nodes={s['node_count']}  live_regions={s['live_region_count']}  "
                      f"gen_markers={len(s['gen_markers'])}  action_markers={s['action_markers_count']}")
                if s["gen_markers"]:
                    for g in s["gen_markers"][:3]:
                        print(f"    gen: role={g['role']} name={g['name']!r}")
            except Exception as e:
                print(f"  sample {sample_i} failed: {e}")

        # Wait for send to complete
        try:
            resp = await asyncio.wait_for(send_task, timeout=90)
            p = json.loads(resp.read())
            content = p.get("choices", [{}])[0].get("message", {}).get("content", "")[:60]
            print(f"\nbridge returned: {content!r}")
        except Exception as e:
            print(f"\nbridge error: {e}")

        print("\n=== AFTER COMPLETION ===")
        await asyncio.sleep(1.0)
        after_nodes = await get_a11y_tree(ws, nxt)
        after_summary = summarize_a11y(after_nodes)
        print(json.dumps(after_summary, indent=2, default=str)[:1200])

        # Diff baseline vs after for node count and role changes
        print("\n=== DIFF baseline → after ===")
        print(f"node count: {baseline_summary['node_count']} → {after_summary['node_count']}")
        for role, count in after_summary["role_counts_top10"].items():
            bc = baseline_summary["role_counts_top10"].get(role, 0)
            if count != bc:
                print(f"  {role}: {bc} → {count}")

        # Compare action_markers (should appear after completion)
        print(f"\naction_markers: baseline={baseline_summary['action_markers_count']} "
              f"during(last)={during_summaries[-1]['action_markers_count'] if during_summaries else '?'} "
              f"after={after_summary['action_markers_count']}")
        if after_summary["action_markers_sample"]:
            print("after action markers:")
            for a in after_summary["action_markers_sample"]:
                print(f"  role={a['role']} name={a['name']!r}")


asyncio.run(main())

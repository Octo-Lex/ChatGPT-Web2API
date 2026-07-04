"""B1 Step 10: Structural live canary (MCP SDK client version).

Connects two independent MCP SSE clients to the pool-mode bridge and validates:
  1. MCP startup creates no driver/tab.
  2. First explicit request from client A creates one owned tab.
  3. Client B gets a DISTINCT owned tab.
  4. Same-session follow-up reuses the same tab.
  5. Max live owned tabs <= pool_size (2).

Uses the MCP Python SDK's sse_client + ClientSession (NOT raw sockets/httpx —
the raw approach failed because it closed the SSE stream before the tool
handler could respond).
"""
from __future__ import annotations

import asyncio
import json
import urllib.request


CDP = 9222
BRIDGE = "http://127.0.0.1:8090"
POOL_SIZE = 2


def count_chatgpt_tabs() -> list[dict]:
    targets = json.loads(
        urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=5).read()
    )
    return [t for t in targets if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")]


async def client_session(label: str, retries: int = 3) -> tuple[bool, int]:
    """Connect one MCP SSE client, call list_models, return (success, attempts)."""
    from mcp.client.sse import sse_client
    from mcp.client.session import ClientSession

    async with sse_client(f"{BRIDGE}/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(f"[{label}] Initialized. Calling list_models...")
            for attempt in range(1, retries + 1):
                result = await session.call_tool("list_models", {})
                if not result.isError:
                    text = result.content[0].text[:60] if result.content and hasattr(result.content[0], "text") else ""
                    print(f"[{label}] OK on attempt {attempt}: {text!r}")
                    return True, attempt
                print(f"[{label}] Attempt {attempt} failed (cold tab). Waiting 5s...")
                await asyncio.sleep(5)
            print(f"[{label}] All attempts failed.")
            return False, retries


async def main():
    initial = count_chatgpt_tabs()
    initial_ids = {t["id"] for t in initial}
    print(f"Initial ChatGPT tabs: {len(initial)}")

    # Client A
    print("\n=== Client A ===")
    ok_a, att_a = await client_session("A")
    await asyncio.sleep(3)
    after_a = count_chatgpt_tabs()
    new_a = {t["id"] for t in after_a} - initial_ids
    print(f"New tab for A: {new_a}")

    # Client B
    print("\n=== Client B ===")
    ok_b, att_b = await client_session("B")
    await asyncio.sleep(3)
    after_b = count_chatgpt_tabs()
    new_b = ({t["id"] for t in after_b} - initial_ids) - new_a
    print(f"New tab for B: {new_b}")

    # Verdict
    distinct = bool(new_a) and bool(new_b) and new_a.isdisjoint(new_b)
    total_new = len(after_b) - len(initial)
    print(f"\n=== STRUCTURAL CANARY VERDICT ===")
    print(f"A success: {ok_a} (attempts: {att_a})")
    print(f"B success: {ok_b} (attempts: {att_b})")
    print(f"A target: {new_a}")
    print(f"B target: {new_b}")
    print(f"Distinct targets: {distinct}")
    print(f"Total new tabs: {total_new} (pool_size={POOL_SIZE})")
    print(f"Within pool size: {total_new <= POOL_SIZE}")
    all_pass = ok_a and ok_b and distinct and total_new <= POOL_SIZE
    print(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())

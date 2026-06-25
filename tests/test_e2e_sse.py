"""E2E for the MCP **SSE transport** over a real HTTP/SSE network path.

Unlike ``test_e2e_mcp.py`` (which uses the in-memory transport and bypasses
uvicorn / ``/sse`` / ``/messages``), these tests connect a real
``mcp.client.sse.sse_client`` to a real uvicorn SSE server started by the
``e2e_sse_server`` session fixture. This is the only place the SSE network
path — the recommended ZCode transport — is exercised.

The fixture runs on a non-8090 port (default 18090) so it never contends with
an operational SSE server. ``chat_completion`` over SSE here is also the live
regression test for issue #10/#11 (the SSE completion deadlock fix).

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_sse.py -m e2e -v
"""

import json
import urllib.request

import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client

pytestmark = pytest.mark.e2e


def _chatgpt_target_count(cdp_port: int) -> int:
    """Count open Chrome CDP targets whose URL is on chatgpt.com.

    Used by the no-growth test: the SSE invariant is that repeated client
    connections do NOT spawn a new Chrome tab per connection. We count CDP
    targets (not OS processes) because that is the resource we actually care
    about. The CDP port is passed in (not hard-coded) so non-default-port
    e2e runs query the correct Chrome instance.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/list", timeout=5) as r:
            targets = json.loads(r.read())
    except Exception:
        return -1  # unreachable — test will skip the strict assertion
    return sum(1 for t in targets if "chatgpt.com" in (t.get("url") or ""))


# ── 1. initialize handshake ────────────────────────────────────────────


async def test_sse_initialize_handshake(e2e_sse_server: str):
    """A real SSE client completes the MCP initialize handshake."""
    async with sse_client(e2e_sse_server) as (read, write):
        async with ClientSession(read, write) as session:
            result = await session.initialize()
            assert result.protocolVersion
            assert hasattr(result, "capabilities")


# ── 2. list_tools (gated surface) ──────────────────────────────────────


async def test_sse_list_tools(e2e_sse_server: str):
    """list_tools over SSE returns the same gated surface as in-memory."""
    async with sse_client(e2e_sse_server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            names = {t.name for t in resp.tools}
            assert "list_models" in names
            assert "chat_completion" in names
            # Destructive tools hidden without W2A_ENABLE_DESTRUCTIVE
            assert "delete_memory" not in names


# ── 3. list_models ─────────────────────────────────────────────────────


async def test_sse_list_models(e2e_sse_server: str):
    """list_models round-trips over the real SSE transport to ChatGPT."""
    async with sse_client(e2e_sse_server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_models", {})
            assert result.isError is not True
            text = result.content[0].text
            assert "gpt" in text.lower() or "auto" in text.lower(), (
                f"no model slug in response: {text[:200]!r}"
            )


# ── 4. chat_completion (regression for #10/#11) ────────────────────────


async def test_sse_chat_completion(e2e_sse_server: str, e2e_created: dict):
    """chat_completion works end-to-end over the SSE network path.

    This is the live regression test for issue #10/#11: the SSE completion
    deadlock that timed out at 120s before the conv_id resolution fix. It must
    complete in seconds, not time out.
    """
    marker = "W2A-E2E-SSE-CHAT-OK"
    async with sse_client(e2e_sse_server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "chat_completion",
                {"message": f"Reply with exactly this token: {marker}"},
            )
            assert result.isError is not True
            data = result.structuredContent or {}
            cid = data.get("conversation_id", "")
            if cid:
                e2e_created["conversations"].add(cid)
            assert marker in str(data.get("content", "")), (
                f"marker missing from structured content: {data}"
            )


# ── 5. repeated connections do not grow Chrome tabs ────────────────────


async def test_sse_repeated_connections_no_growth(e2e_sse_server: str, e2e_config):
    """The core SSE selling point: N client sessions share one MCP server
    with no per-connection Chrome tab growth.

    Count CDP targets before and after opening 3 fresh SSE sessions. The
    invariant is "no per-connection growth," not exact equality — a cold
    start or send-readiness path may create/reclaim one owned tab, so we
    allow ``after <= before + 1``.
    """
    cdp_port = e2e_config.chrome.cdp_port
    before = _chatgpt_target_count(cdp_port)
    for _ in range(3):
        async with sse_client(e2e_sse_server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
    after = _chatgpt_target_count(cdp_port)
    if before < 0 or after < 0:
        pytest.skip("CDP /json/list unreachable; cannot assert target count")
    assert after <= before + 1, (
        f"SSE caused tab growth: before={before} after={after} (3 connections)"
    )

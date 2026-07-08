"""End-to-end integration tests over the real MCP protocol.

These run a genuine MCP client session against the real server using an
in-memory transport (``mcp.shared.memory``). They exercise the full
request path — initialize -> list_tools -> call_tool — that unit tests
which poke handlers directly cannot see:

  - The client observes exactly the gated tool surface.
  - A gated tool returns a protocol-level error result when called.
  - A safe tool reaches the driver and returns a normal result.
  - Gate env vars change what the client sees.

No real Chrome / ChatGPT connection is required: the CDPDriver is mocked,
so we verify the *server's* behavior, not the upstream account.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import chatgpt_web2api.mcp_server as mod
from chatgpt_web2api.cdp_driver import StreamChunk
from chatgpt_web2api.mcp_server import (
    DESTRUCTIVE_ENV,
    WRITE_ENV,
    ToolName,
)

GATE_ENVS = [WRITE_ENV, DESTRUCTIVE_ENV]


def clear_gate_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GATE_ENVS:
        monkeypatch.delenv(name, raising=False)


def make_mock_driver():
    """A CDPDriver-shaped mock for safe tools to reach real business logic."""
    driver = MagicMock()
    driver._current_conv_id = None
    driver._current_model = None
    driver.is_connected = True
    driver._access_token = "test-token"

    async def _stream(text, timeout=120, *, budgets=None, model=None):
        yield StreamChunk(delta="Mocked ChatGPT response")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.select_model = AsyncMock(return_value=True)
    driver.send_and_stream = _stream
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.navigate_gpt = AsyncMock()
    driver.get_models = AsyncMock(return_value=[
        {"slug": "auto", "title": "Auto"},
        {"slug": "gpt-5-5", "title": "GPT-5.5"},
    ])
    driver.get_projects = AsyncMock(return_value=[])
    driver.get_conversations = AsyncMock(return_value=[])
    driver.get_conversation = AsyncMock(return_value={"id": "x", "mapping": {}})
    driver.get_memories = AsyncMock(return_value=[])
    driver.list_gpts = AsyncMock(return_value=[])
    driver.get_project_files = AsyncMock(return_value=[])
    return driver


async def _session_for(server):
    """Create an initialized client session connected to ``server``."""
    ctx = create_connected_server_and_client_session(server)
    session = await ctx.__aenter__()
    await session.initialize()
    return ctx, session


# ── Client observes the gated surface ─────────────────────────

@pytest.mark.asyncio
async def test_client_sees_default_safe_surface(monkeypatch):
    """A real MCP client sees exactly the 9 safe tools by default."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(mod, "_driver", make_mock_driver())
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        resp = await session.list_tools()
        names = {t.name for t in resp.tools}

        expected_safe = {
            ToolName.CHAT_COMPLETION.value, ToolName.CHAT_WITH_GPT.value,
            ToolName.LIST_MODELS.value, ToolName.LIST_PROJECTS.value,
            ToolName.LIST_CONVERSATIONS.value, ToolName.GET_CONVERSATION.value,
            ToolName.LIST_MEMORIES.value, ToolName.LIST_GPTS.value,
            ToolName.LIST_PROJECT_FILES.value,
        }
        assert names == expected_safe
        # Hidden tools must not leak to the client
        assert ToolName.DELETE_MEMORY.value not in names
        assert ToolName.CREATE_PROJECT.value not in names
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_client_sees_full_surface_with_both_gates(monkeypatch):
    """With both gate envs set, the client sees all 15 tools."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(WRITE_ENV, "1")
    monkeypatch.setenv(DESTRUCTIVE_ENV, "1")
    monkeypatch.setattr(mod, "_driver", make_mock_driver())
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        resp = await session.list_tools()
        names = {t.name for t in resp.tools}
        assert len(names) == 16
        assert ToolName.DELETE_MEMORY.value in names
        assert ToolName.CREATE_PROJECT.value in names
    finally:
        await ctx.__aexit__(None, None, None)


# ── Safe tool reaches the driver over the protocol ────────────

@pytest.mark.asyncio
async def test_client_calls_safe_tool_and_gets_response(monkeypatch):
    """A safe tool (list_models) round-trips to the driver and back."""
    clear_gate_envs(monkeypatch)
    driver = make_mock_driver()
    monkeypatch.setattr(mod, "_driver", driver)
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        result = await session.call_tool("list_models", {})
        assert result.isError is not True
        # The driver's mock returned 2 models; the response text should mention them
        text = result.content[0].text
        assert "auto" in text
        assert "GPT-5.5" in text
        driver.get_models.assert_awaited()
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_client_calls_chat_completion(monkeypatch):
    """The primary tool (chat_completion) works over the real protocol."""
    clear_gate_envs(monkeypatch)
    driver = make_mock_driver()
    monkeypatch.setattr(mod, "_driver", driver)
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        result = await session.call_tool(
            "chat_completion", {"message": "Hello"}
        )
        assert result.isError is not True
        assert "Mocked ChatGPT response" in result.content[0].text
    finally:
        await ctx.__aexit__(None, None, None)


# ── Gated tool refused over the protocol ──────────────────────

@pytest.mark.asyncio
async def test_gated_tool_returns_protocol_error(monkeypatch):
    """A gated tool called by name returns an MCP error result, not execution.

    This is the full defense-in-depth path: even if a client knows the tool
    name, the call is refused before reaching the driver.
    """
    clear_gate_envs(monkeypatch)
    driver = make_mock_driver()
    driver.delete_memory = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "_driver", driver)
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        result = await session.call_tool(
            "delete_memory", {"memory_id": "mem-1"}
        )
        # Must be an error result, and the message must name the gate env var
        assert result.isError is True
        assert DESTRUCTIVE_ENV in result.content[0].text
        # The driver must NOT have been reached
        driver.delete_memory.assert_not_called()
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_gated_tool_executes_when_enabled(monkeypatch):
    """With the destructive gate on, delete_memory reaches the driver."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(DESTRUCTIVE_ENV, "1")
    driver = make_mock_driver()
    driver.delete_memory = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "_driver", driver)
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        result = await session.call_tool(
            "delete_memory", {"memory_id": "mem-1"}
        )
        assert result.isError is not True
        driver.delete_memory.assert_awaited_once()
    finally:
        await ctx.__aexit__(None, None, None)


# ── Auth metadata visible to clients ─────────────────────────

@pytest.mark.asyncio
async def test_client_sees_noauth_metadata(monkeypatch):
    """The client can read the honest noauth security metadata."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setattr(mod, "_driver", make_mock_driver())
    monkeypatch.setattr(mod, "_config", None)

    server = mod.create_server()
    ctx, session = await _session_for(server)
    try:
        resp = await session.list_tools()
        for tool in resp.tools:
            assert tool.meta == {"securitySchemes": [{"type": "noauth"}]}, (
                f"{tool.name} missing noauth meta"
            )
    finally:
        await ctx.__aexit__(None, None, None)

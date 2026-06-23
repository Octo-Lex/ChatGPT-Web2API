"""Tests for MCP tool access gating.

Mirrors the hermes-gpt graduated-access model:
  - Safe reads + core chat visible by default
  - Write/mutating tools hidden unless W2A_ENABLE_WRITE=1
  - Destructive tools hidden unless W2A_ENABLE_DESTRUCTIVE=1
  - Hidden tools refuse to execute even if called directly
  - Every tool advertises honest auth metadata
  - Non-loopback binding warns the operator
"""

import pytest

from chatgpt_web2api.mcp_server import (
    DESTRUCTIVE_ENV,
    NOAUTH_META,
    WRITE_ENV,
    ToolName,
    build_tools,
    is_loopback_host,
    tool_meta,
)

GATE_ENVS = [WRITE_ENV, DESTRUCTIVE_ENV]


def clear_gate_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GATE_ENVS:
        monkeypatch.delenv(name, raising=False)


def names_of(tools) -> set[str]:
    return {t.name for t in tools}


# ── Default (safe) surface ────────────────────────────────────

def test_default_surface_excludes_write_and_destructive(monkeypatch):
    """With no gate envs set, only safe + core-chat tools are visible."""
    clear_gate_envs(monkeypatch)
    visible = names_of(build_tools())

    # Core chat must always be available — it's the primary use case
    assert ToolName.CHAT_COMPLETION.value in visible
    assert ToolName.CHAT_WITH_GPT.value in visible

    # All reads must be visible
    for tn in [
        ToolName.LIST_MODELS, ToolName.LIST_PROJECTS,
        ToolName.LIST_CONVERSATIONS, ToolName.GET_CONVERSATION,
        ToolName.LIST_MEMORIES, ToolName.LIST_GPTS,
        ToolName.LIST_PROJECT_FILES,
    ]:
        assert tn.value in visible, f"{tn.value} should be visible by default"

    # Write tools must be hidden
    for tn in [
        ToolName.CREATE_PROJECT, ToolName.UPDATE_PROJECT_INSTRUCTIONS,
        ToolName.CREATE_MEMORY, ToolName.ARCHIVE_CONVERSATION,
    ]:
        assert tn.value not in visible, f"{tn.value} must be hidden by default"

    # Destructive tools must be hidden
    for tn in [ToolName.DELETE_CONVERSATION, ToolName.DELETE_MEMORY]:
        assert tn.value not in visible, f"{tn.value} must be hidden by default"


def test_default_surface_exact_name_set(monkeypatch):
    """The default visible surface is exactly this set — catches accidental exposure."""
    clear_gate_envs(monkeypatch)
    expected = {
        ToolName.CHAT_COMPLETION.value,
        ToolName.CHAT_WITH_GPT.value,
        ToolName.LIST_MODELS.value,
        ToolName.LIST_PROJECTS.value,
        ToolName.LIST_CONVERSATIONS.value,
        ToolName.GET_CONVERSATION.value,
        ToolName.LIST_MEMORIES.value,
        ToolName.LIST_GPTS.value,
        ToolName.LIST_PROJECT_FILES.value,
    }
    assert names_of(build_tools()) == expected


# ── Write gate ────────────────────────────────────────────────

def test_write_env_exposes_mutating_tools(monkeypatch):
    """W2A_ENABLE_WRITE=1 surfaces project/memory/archive mutation tools."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(WRITE_ENV, "1")
    visible = names_of(build_tools())

    for tn in [
        ToolName.CREATE_PROJECT, ToolName.UPDATE_PROJECT_INSTRUCTIONS,
        ToolName.CREATE_MEMORY, ToolName.ARCHIVE_CONVERSATION,
    ]:
        assert tn.value in visible, f"{tn.value} should appear with {WRITE_ENV}=1"

    # Destructive tools still hidden
    for tn in [ToolName.DELETE_CONVERSATION, ToolName.DELETE_MEMORY]:
        assert tn.value not in visible


# ── Destructive gate ──────────────────────────────────────────

def test_destructive_env_exposes_delete_tools(monkeypatch):
    """W2A_ENABLE_DESTRUCTIVE=1 surfaces the two irreversible delete tools."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(DESTRUCTIVE_ENV, "1")
    visible = names_of(build_tools())

    for tn in [ToolName.DELETE_CONVERSATION, ToolName.DELETE_MEMORY]:
        assert tn.value in visible, f"{tn.value} should appear with {DESTRUCTIVE_ENV}=1"


def test_both_gates_expose_all_sixteen(monkeypatch):
    """With both gates on, the full 16-tool surface is restored."""
    clear_gate_envs(monkeypatch)
    monkeypatch.setenv(WRITE_ENV, "1")
    monkeypatch.setenv(DESTRUCTIVE_ENV, "1")
    assert len(build_tools()) == 16


# ── Block-at-call: hidden tools refuse to execute ─────────────

@pytest.mark.asyncio
async def test_hidden_tool_refuses_to_run(monkeypatch):
    """A hidden tool must be refused even if a client calls it by name.

    The MCP SDK converts raised exceptions into a ``CallToolResult`` with
    ``isError=True``, so we assert that error contract (the gate env var
    appears in the message) rather than a raised exception. This is
    defense-in-depth: a buggy/malicious client could call a tool without
    it appearing in list_tools — the call must still be refused.
    """
    clear_gate_envs(monkeypatch)
    import chatgpt_web2api.mcp_server as mod
    monkeypatch.setattr(mod, "_driver", object())  # truthy: passes the driver check
    monkeypatch.setattr(mod, "_config", None)
    monkeypatch.setattr(mod, "_lock_cdp_port", None)

    server = mod.create_server()
    from mcp import types as t

    handler = server.request_handlers[t.CallToolRequest]
    result = await handler(t.CallToolRequest(
        method="tools/call",
        params=t.CallToolRequestParams(name="delete_memory", arguments={"memory_id": "x"}),
    ))

    inner = result.root
    assert inner.isError is True
    assert DESTRUCTIVE_ENV in inner.content[0].text


@pytest.mark.asyncio
async def test_visible_tool_is_not_blocked(monkeypatch):
    """A safe tool runs through to the driver (no gate refusal)."""
    clear_gate_envs(monkeypatch)
    from unittest.mock import AsyncMock, MagicMock

    import chatgpt_web2api.mcp_server as mod

    driver = MagicMock()
    driver.get_models = AsyncMock(return_value=[{"slug": "auto", "title": "Auto"}])
    monkeypatch.setattr(mod, "_driver", driver)
    monkeypatch.setattr(mod, "_config", None)
    monkeypatch.setattr(mod, "_lock_cdp_port", None)

    server = mod.create_server()
    from mcp import types as t
    handler = server.request_handlers[t.CallToolRequest]
    result = await handler(t.CallToolRequest(
        method="tools/call",
        params=t.CallToolRequestParams(name="list_models", arguments={}),
    ))
    inner = result.root
    assert inner.isError is not True  # safe tool must not be flagged as an error


# ── Auth metadata honesty ─────────────────────────────────────

def test_every_visible_tool_carries_noauth_meta(monkeypatch):
    """When no API keys are configured, tools must advertise noauth."""
    clear_gate_envs(monkeypatch)
    for tool in build_tools():
        assert tool.meta == NOAUTH_META, f"{tool.name} missing noauth meta"


def test_tool_meta_passthrough_and_override():
    """tool_meta() returns noauth by default and merges extras."""
    assert tool_meta() == NOAUTH_META
    merged = tool_meta({"extra": True})
    assert merged["securitySchemes"] == NOAUTH_META["securitySchemes"]
    assert merged["extra"] is True


# ── Loopback detection ────────────────────────────────────────

@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True),
    ("localhost", True),
    ("::1", True),
    ("0.0.0.0", False),
    ("192.168.1.5", False),
    ("example.com", False),
])
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_warn_non_loopback_emits_when_exposed(caplog):
    """Binding a no-auth server off loopback must log a warning."""
    import logging

    from chatgpt_web2api.mcp_server import warn_non_loopback

    with caplog.at_level(logging.WARNING, logger="chatgpt_web2api.mcp_server"):
        warn_non_loopback("0.0.0.0", "sse")

    assert any("0.0.0.0" in r.message and "no authentication" in r.message
               for r in caplog.records)


def test_warn_non_loopback_silent_on_loopback(caplog):
    """Loopback binding must not warn."""
    import logging

    from chatgpt_web2api.mcp_server import warn_non_loopback

    with caplog.at_level(logging.WARNING, logger="chatgpt_web2api.mcp_server"):
        warn_non_loopback("127.0.0.1", "sse")

    assert not any("no authentication" in r.message for r in caplog.records)


def test_warn_non_loopback_respects_api_keys(caplog, monkeypatch):
    """When api_keys are configured, the no-auth warning is suppressed."""
    import logging

    from chatgpt_web2api.config import Config
    from chatgpt_web2api.mcp_server import warn_non_loopback

    cfg = Config.load(None)
    cfg.server.api_keys = ["sk-test"]
    monkeypatch.setattr("chatgpt_web2api.mcp_server._config", cfg)

    with caplog.at_level(logging.WARNING, logger="chatgpt_web2api.mcp_server"):
        warn_non_loopback("0.0.0.0", "sse")

    assert not any("no authentication" in r.message for r in caplog.records)

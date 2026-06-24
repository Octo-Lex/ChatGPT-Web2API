"""MCP Server — expose ChatGPT-Web2API as an MCP server for AI agents.

Implements the Model Context Protocol following official reference patterns
from the `modelcontextprotocol/servers` repository:

  - Pydantic BaseModel input schemas (mcp-server-git pattern)
  - Enum for tool names to prevent typos
  - ToolAnnotations on every tool with all 4 hints
  - outputSchema + structuredContent on every tool
  - Resource templates for dynamic URIs
  - Rich descriptions with domain knowledge baked in
  - Pure business logic with thin tool handlers
  - raise_exceptions=True for proper error propagation

Transports:
    stdio  — for Claude Desktop, Cursor, etc. (default)
    sse    — for web clients (Craft Agent, custom hosts)

Run:
    chatgpt-web2api-mcp                         # stdio (default)
    chatgpt-web2api-mcp --transport sse          # SSE on port 8090
    chatgpt-web2api-mcp --transport sse --port 3000

Prerequisites:
    Run 'chatgpt-web2api' first to start Chrome with an authenticated session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from mcp import types as mcp_types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, Field

from .cdp_driver import (
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
    RateLimitError,
)
from .config import Config
from .cross_process_lock import CrossProcessLock, LockAcquisitionError
from .resilience import retry_on_rate_limit
from .tab_registry import TabRegistry

logger = logging.getLogger(__name__)

# How many streamed chunks between coalesced progress notifications. The
# underlying DOM poll yields roughly one delta per ~0.5s, so notifying every
# 10 chunks ≈ one progress signal every ~5s — enough to reset an MCP client's
# idle/timeout timer without flooding it. Tunable.
_PROGRESS_EVERY_N_CHUNKS = 10

# A progress notifier built per-request from the MCP request context. Receives
# a short human-readable status string; None means the client can't receive
# progress (no token) and the business function must skip emitting.
ProgressCallback = Callable[[str], Awaitable[None]]


# ═══════════════════════════════════════════════════════════════
# Input Schemas — Pydantic BaseModel (official pattern from mcp-server-git)
# ═══════════════════════════════════════════════════════════════

class ChatCompletionInput(BaseModel):
    """Input schema for chat_completion tool."""
    message: str = Field(description="The user message to send to ChatGPT")
    system_prompt: str | None = Field(
        default=None,
        description=(
            "System instructions prepended to the message. "
            "Changing this value starts a new conversation. "
            "For persistent instructions, use a project instead — "
            "project instructions apply to all conversations in the project."
        ),
    )
    model: str = Field(
        default="auto",
        description=(
            "Model slug to use. Common values: auto (default), "
            "gpt-5-5 (latest, reasoning), gpt-5-mini (fast, simple tasks). "
            "Use list_models to see all available slugs."
        ),
    )
    conversation_id: str | None = Field(
        default=None,
        description=(
            "UUID of an existing conversation to continue. "
            "When omitted, the tool auto-continues the last conversation "
            "(if system_prompt and project_id haven't changed). "
            "Pass a specific ID to resume a particular conversation."
        ),
    )
    project_id: str | None = Field(
        default=None,
        description=(
            "ChatGPT project gizmo ID (e.g. g-p-abc123) for project-scoped "
            "persistent memory, custom instructions, and file attachments. "
            "Changing this value starts a new conversation. "
            "Use list_projects to discover available projects."
        ),
    )


class ListModelsInput(BaseModel):
    """No inputs needed — empty schema."""
    pass


class ListProjectsInput(BaseModel):
    """No inputs needed — empty schema."""
    pass


class GetConversationInput(BaseModel):
    """Input for retrieving conversation history.

    Messages are returned oldest-first. ``limit`` caps how many messages a
    single call returns; ``offset`` skips earlier messages so the agent can
    page through an arbitrarily long conversation in chunks that each fit a
    tool-result budget. ``total`` + ``has_more`` in the response tell it when
    to stop. Defaults (offset=0, limit=50) preserve the old behavior.
    """

    conversation_id: str = Field(
        description="UUID of the conversation to retrieve",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Skip this many messages from the start. Page through by "
        "increasing offset by `limit` each call until has_more is false.",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Max messages to return per call. Lower this (e.g. 15) if "
        "the conversation has very long messages and the result is being "
        "truncated before reaching you.",
    )


class ListConversationsInput(BaseModel):
    """Input for listing recent conversations."""
    limit: int = Field(
        default=28,
        description="Maximum number of conversations to return (default: 28)",
    )
    offset: int = Field(
        default=0,
        description="Number of conversations to skip for pagination",
    )


class DeleteConversationInput(BaseModel):
    """Input for deleting a conversation."""
    conversation_id: str = Field(
        description="UUID of the conversation to delete",
    )


class CreateProjectInput(BaseModel):
    """Input for creating a new ChatGPT project."""
    name: str = Field(
        description=(
            "Display name for the project. This name appears in the ChatGPT sidebar."
        ),
    )
    instructions: str = Field(
        default="",
        description=(
            "Custom instructions (system prompt) for the project. "
            "These instructions apply to ALL conversations created within this project. "
            "Example: 'You are a specialist in Python async programming. Always provide "
            "type hints and docstrings.'"
        ),
    )
    memory_scope: str = Field(
        default="project_v2",
        description=(
            "Memory scope for the project. "
            "'project_v2' = dedicated memory (isolated, no shared memory from other chats) "
            "'global' = shared memory (uses the global ChatGPT memory pool). "
            "Use 'project_v2' when you want isolated context for a specific task."
        ),
    )


class UpdateProjectInstructionsInput(BaseModel):
    """Input for updating a project's custom instructions."""
    project_id: str = Field(
        description="Project gizmo ID (e.g. g-p-abc123)",
    )
    instructions: str = Field(
        description=(
            "New custom instructions for the project. "
            "These replace any existing instructions. "
            "They apply to all new conversations in the project."
        ),
    )


class ArchiveConversationInput(BaseModel):
    """Input for archiving or unarchiving a conversation."""
    conversation_id: str = Field(description="UUID of the conversation")
    archive: bool = Field(
        default=True,
        description="True to archive, False to unarchive",
    )


class ListMemoriesInput(BaseModel):
    """No inputs needed."""
    pass


class CreateMemoryInput(BaseModel):
    """Input for creating a new ChatGPT memory."""
    content: str = Field(
        description=(
            "The fact or information to store in ChatGPT's memory. "
            "ChatGPT will remember this across future conversations. "
            "Example: 'The user prefers concise answers with code examples.'"
        ),
    )


class DeleteMemoryInput(BaseModel):
    """Input for deleting a ChatGPT memory."""
    memory_id: str = Field(description="ID of the memory to delete")


class DeleteProjectInput(BaseModel):
    """Input for deleting a ChatGPT project."""
    project_id: str = Field(description="ID of the project to delete (g-p-...)")


class ListGptsInput(BaseModel):
    """No inputs needed."""
    pass


class ListProjectFilesInput(BaseModel):
    """Input for listing project files."""
    project_id: str = Field(
        description="Project gizmo ID to list files for",
    )


class ChatWithGptInput(BaseModel):
    """Input for chatting with a specific Custom GPT."""
    gpt_id: str = Field(
        description=(
            "Custom GPT gizmo ID (e.g. g-hkJGhxxx). "
            "Use list_gpts to discover available GPTs."
        ),
    )
    message: str = Field(description="The message to send to the GPT")


# ═══════════════════════════════════════════════════════════════
# Tool Name Enum — prevents typos (official pattern from mcp-server-git)
# ═══════════════════════════════════════════════════════════════

class ToolName(str, Enum):
    # Core chat
    CHAT_COMPLETION = "chat_completion"
    # Discovery
    LIST_MODELS = "list_models"
    LIST_PROJECTS = "list_projects"
    LIST_GPTS = "list_gpts"
    # Conversations
    GET_CONVERSATION = "get_conversation"
    LIST_CONVERSATIONS = "list_conversations"
    DELETE_CONVERSATION = "delete_conversation"
    ARCHIVE_CONVERSATION = "archive_conversation"
    # Projects
    CREATE_PROJECT = "create_project"
    UPDATE_PROJECT_INSTRUCTIONS = "update_project_instructions"
    DELETE_PROJECT = "delete_project"
    LIST_PROJECT_FILES = "list_project_files"
    # Memory
    LIST_MEMORIES = "list_memories"
    CREATE_MEMORY = "create_memory"
    DELETE_MEMORY = "delete_memory"
    # Custom GPTs
    CHAT_WITH_GPT = "chat_with_gpt"


# ═══════════════════════════════════════════════════════════════
# Output Schemas — structured output validation (Memory server pattern)
# ═══════════════════════════════════════════════════════════════

CHAT_COMPLETION_OUTPUT = {
    "type": "object",
    "properties": {
        "content": {"type": "string", "description": "The assistant response text"},
        "model": {"type": "string", "description": "Model slug used for generation"},
        "conversation_id": {
            "type": "string",
            "description": "UUID of the conversation for multi-turn follow-up",
        },
    },
    "required": ["content", "model", "conversation_id"],
}

MODEL_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Model slug for use in chat_completion"},
        "title": {"type": "string", "description": "Human-readable model name"},
    },
    "required": ["id", "title"],
}

LIST_MODELS_OUTPUT = {
    "type": "object",
    "properties": {
        "models": {"type": "array", "items": MODEL_ITEM},
    },
    "required": ["models"],
}

PROJECT_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Project gizmo ID (use as project_id)"},
        "name": {"type": "string"},
        "memory_scope": {
            "type": "string",
            "description": "'project_v2' (dedicated) or 'global' (shared)",
        },
    },
    "required": ["id", "name"],
}

LIST_PROJECTS_OUTPUT = {
    "type": "object",
    "properties": {
        "projects": {"type": "array", "items": PROJECT_ITEM},
    },
    "required": ["projects"],
}

GET_CONVERSATION_OUTPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "role": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["role", "content"],
            },
        },
        "offset": {
            "type": "integer",
            "description": "Number of messages skipped from the start (echoed from the request).",
        },
        "limit": {
            "type": "integer",
            "description": "Max messages requested per call (echoed from the request).",
        },
        "total": {
            "type": "integer",
            "description": "Total messages in the conversation (across all pages).",
        },
        "has_more": {
            "type": "boolean",
            "description": "True if more pages remain; page through by increasing offset by limit.",
        },
    },
    "required": ["id", "total", "has_more"],
}

CONVERSATION_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Conversation UUID"},
        "title": {"type": "string"},
        "update_time": {"type": "number", "description": "Unix timestamp"},
        "gizmo_id": {
            "type": "string",
            "description": "Project ID if conversation belongs to a project, null otherwise",
        },
    },
    "required": ["id", "title"],
}

LIST_CONVERSATIONS_OUTPUT = {
    "type": "object",
    "properties": {
        "conversations": {"type": "array", "items": CONVERSATION_ITEM},
    },
    "required": ["conversations"],
}

DELETE_RESULT_OUTPUT = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "conversation_id": {"type": "string"},
    },
    "required": ["success", "conversation_id"],
}

# Distinct from DELETE_RESULT_OUTPUT: delete_memory returns memory_id,
# not conversation_id. Previously the two shared a schema, which made
# every delete_memory call fail MCP output validation.
DELETE_MEMORY_RESULT_OUTPUT = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "memory_id": {"type": "string"},
    },
    "required": ["success", "memory_id"],
}

DELETE_PROJECT_RESULT_OUTPUT = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "project_id": {"type": "string"},
    },
    "required": ["success", "project_id"],
}

CREATE_PROJECT_OUTPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "New project gizmo ID"},
        "name": {"type": "string"},
        "memory_scope": {"type": "string"},
        "instructions": {"type": "string"},
    },
    "required": ["id", "name"],
}

UPDATE_INSTRUCTIONS_OUTPUT = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "project_id": {"type": "string"},
    },
    "required": ["success", "project_id"],
}

ARCHIVE_RESULT_OUTPUT = {
    "type": "object",
    "properties": {
        "success": {"type": "boolean"},
        "conversation_id": {"type": "string"},
        "archived": {"type": "boolean"},
    },
    "required": ["success", "conversation_id", "archived"],
}

MEMORY_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "content": {"type": "string"},
        "created_at": {"type": "string"},
    },
    "required": ["id", "content"],
}

LIST_MEMORIES_OUTPUT = {
    "type": "object",
    "properties": {
        "memories": {"type": "array", "items": MEMORY_ITEM},
    },
    "required": ["memories"],
}

CREATE_MEMORY_OUTPUT = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["content"],
}

GPT_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["id", "name"],
}

LIST_GPTS_OUTPUT = {
    "type": "object",
    "properties": {
        "gpts": {"type": "array", "items": GPT_ITEM},
    },
    "required": ["gpts"],
}

PROJECT_FILE_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"},
        "size": {"type": "number"},
        "mime_type": {"type": "string"},
    },
    "required": ["id", "name"],
}

LIST_PROJECT_FILES_OUTPUT = {
    "type": "object",
    "properties": {
        "files": {"type": "array", "items": PROJECT_FILE_ITEM},
        "project_id": {"type": "string"},
    },
    "required": ["files", "project_id"],
}


# ═══════════════════════════════════════════════════════════════
# Access Control — graduated gating (modeled on hermes-gpt)
# ═══════════════════════════════════════════════════════════════
#
# Three risk tiers:
#   SAFE         — reads + core chat. Always visible. This is the
#                  out-of-box surface; the primary use case works
#                  without any configuration.
#   WRITE gated  — account mutation (create/alter projects, memories,
#                  conversations). Hidden from list_tools unless
#                  W2A_ENABLE_WRITE=1.
#   DESTRUCTIVE  — irreversible deletes. Hidden unless
#                  W2A_ENABLE_DESTRUCTIVE=1.
#
# Hidden tools are also refused at call time (defense-in-depth):
# a client that calls a tool by name without it being listed still
# gets a PermissionError, not silent execution.

WRITE_ENV = "W2A_ENABLE_WRITE"
DESTRUCTIVE_ENV = "W2A_ENABLE_DESTRUCTIVE"

# Tools requiring W2A_ENABLE_WRITE=1 to be visible/callable
_WRITE_GATED_TOOLS = frozenset({
    ToolName.CREATE_PROJECT.value,
    ToolName.UPDATE_PROJECT_INSTRUCTIONS.value,
    ToolName.CREATE_MEMORY.value,
    ToolName.ARCHIVE_CONVERSATION.value,
})

# Tools requiring W2A_ENABLE_DESTRUCTIVE=1 (irreversible account changes)
_DESTRUCTIVE_TOOLS = frozenset({
    ToolName.DELETE_CONVERSATION.value,
    ToolName.DELETE_MEMORY.value,
    ToolName.DELETE_PROJECT.value,
})

# Auth metadata advertised to clients. When api_keys is empty the
# server genuinely has no authentication — saying so lets MCP clients
# (ChatGPT, Claude Desktop) configure their connector correctly.
NOAUTH_META = {"securitySchemes": [{"type": "noauth"}]}


def _env_enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def tool_meta(extra: dict | None = None) -> dict:
    """Return auth metadata, optionally merged with extras."""
    meta = dict(NOAUTH_META)
    if extra:
        meta.update(extra)
    return meta


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def warn_non_loopback(host: str, transport: str) -> None:
    """Warn when a no-auth server binds to a non-loopback address.

    The MCP server has no authentication of its own — any reachable host
    can invoke exposed tools. Binding off loopback without configuring
    ``api_keys`` exposes those tools to the network, so we surface that
    loudly. Suppressed when the operator has set API keys.
    """
    if is_loopback_host(host):
        return
    if _config is not None and _config.server.api_keys:
        return  # operator has added authentication
    logger.warning(
        "%s transport bound to %s with no authentication. Exposed tools "
        "are reachable from the network. Bind to 127.0.0.1 or set api_keys.",
        transport, host,
    )


def _tool_gate_env(tool_name: str) -> str | None:
    """Return the env var gating this tool, or None if always visible."""
    if tool_name in _DESTRUCTIVE_TOOLS:
        return DESTRUCTIVE_ENV
    if tool_name in _WRITE_GATED_TOOLS:
        return WRITE_ENV
    return None


def _visible_tool_names() -> set[str]:
    """Tool names visible given the current environment."""
    visible = set()
    for member in ToolName:
        gate = _tool_gate_env(member.value)
        if gate is None or _env_enabled(gate):
            visible.add(member.value)
    return visible


# ═══════════════════════════════════════════════════════════════
# Global State
# ═══════════════════════════════════════════════════════════════

_driver: CDPDriver | None = None
_config: Config | None = None
# Cross-process lock factory — creates a fresh CrossProcessLock per critical
# section, keyed on the CDP port so all processes sharing a Chrome instance
# serialize. None until run_mcp() sets it. Read-only tools run lock-free.
_lock_cdp_port: int | None = None

# Tools that mutate browser state — must hold the lock
_MUTATING_TOOLS = frozenset({
    ToolName.CHAT_COMPLETION.value,
    ToolName.CHAT_WITH_GPT.value,
    ToolName.DELETE_CONVERSATION.value,
    ToolName.CREATE_PROJECT.value,
    ToolName.UPDATE_PROJECT_INSTRUCTIONS.value,
    ToolName.DELETE_PROJECT.value,
    ToolName.ARCHIVE_CONVERSATION.value,
    ToolName.CREATE_MEMORY.value,
    ToolName.DELETE_MEMORY.value,
})


# ═══════════════════════════════════════════════════════════════
# Business Logic — pure functions (official pattern from mcp-server-git)
# ═══════════════════════════════════════════════════════════════

async def _notify(on_progress: ProgressCallback | None, message: str) -> None:
    """Invoke a progress callback if present, swallowing any error.

    Defense-in-depth: even if the caller hands us a raw (non-helper-built)
    callback that raises on a transport blip, we must never abort the tool
    call — a dropped notification is not worth killing a 40s generation.
    The helper-built _cb already guards internally; this wraps every call
    site so the contract holds regardless of callback provenance.
    """
    if on_progress is None:
        return
    try:
        await on_progress(message)
    except Exception:
        logger.debug("progress notification dropped", exc_info=True)

async def do_chat_completion(
    driver: CDPDriver, args: dict, config: Config,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Execute a chat completion through the CDP driver."""
    validated = ChatCompletionInput(**args)
    project_id = validated.project_id or (
        config.chatgpt.default_project_id if config else None
    )

    # Build the full text with optional system prompt
    if validated.system_prompt:
        full_text = f"[System Instructions]\n{validated.system_prompt}\n\n[User]\n{validated.message}"
    else:
        full_text = validated.message

    # Select model if specified (non-fatal on failure)
    if validated.model and validated.model != "auto":
        selected = await driver.select_model(validated.model)
        if not selected:
            logger.warning(
                "Could not select model '%s', proceeding with active model",
                validated.model,
            )

    # Navigate to correct conversation context
    if validated.conversation_id:
        await driver.navigate_conversation(validated.conversation_id)
    elif driver._current_conv_id and not validated.system_prompt and not project_id:
        # Auto-continue: reconcile against the live tab before sending. Another
        # process sharing the Chrome tab may have navigated it, leaving
        # _current_conv_id stale. ensure_current_conversation verifies the live
        # URL and navigates back if needed (fail-closed). Raises rather than
        # typing into the wrong conversation.
        logger.info("Auto-continuing conversation: %s", driver._current_conv_id)
        await driver.ensure_current_conversation(driver._current_conv_id)
    else:
        await driver.navigate_new_chat(gizmo_id=project_id)

    # Send and collect response. Progress notifications reset the MCP client's
    # idle timer during long generations so the tool call isn't killed at
    # ~30s. on_progress is None when the client can't receive progress.
    # NOTE: across a rate-limit retry ChatGPT re-types and re-streams the
    # response from scratch, so the message may visually "reset" even though
    # the numeric progress counter keeps climbing — see _make_progress_callback.
    full_response = ""
    conv_id = ""
    chunk_count = 0
    async for chunk in driver.send_and_stream(full_text, timeout=120):
        if chunk.delta:
            full_response += chunk.delta
            chunk_count += 1
            if chunk_count == 1:
                await _notify(on_progress, "Assistant is responding…")
            elif chunk_count % _PROGRESS_EVERY_N_CHUNKS == 0:
                await _notify(on_progress, f"Streaming… {len(full_response)} chars")
        if chunk.finish_reason:
            conv_id = driver._current_conv_id or ""
            await _notify(on_progress, "Finalizing…")

    return {
        "content": full_response,
        "model": validated.model,
        "conversation_id": conv_id,
    }


async def do_list_models(driver: CDPDriver) -> dict:
    """List available models."""
    models = await driver.get_models()
    return {
        "models": [
            {"id": m.get("slug", ""), "title": m.get("title", "")}
            for m in models
        ],
    }


async def do_list_projects(driver: CDPDriver) -> dict:
    """List available projects."""
    projects = await driver.get_projects()
    return {
        "projects": [
            {
                "id": p.get("id", ""),
                "name": p.get("name", "Unknown"),
                "memory_scope": p.get("memory_scope", "project_v2"),
            }
            for p in projects
            if p.get("id")
        ],
    }


async def do_get_conversation(driver: CDPDriver, args: dict) -> dict:
    """Retrieve conversation history (paginated, oldest-first)."""
    validated = GetConversationInput(**args)
    data = await driver.get_conversation(validated.conversation_id)

    # Walk the conversation tree from current_node backwards
    mapping = data.get("mapping", {})
    current_node = data.get("current_node")
    chain = []
    visited = set()
    node_id = current_node

    while node_id and node_id not in visited:
        visited.add(node_id)
        node_data = mapping.get(node_id, {})
        msg = node_data.get("message")
        if msg and msg.get("content"):
            role = msg.get("author", {}).get("role", "unknown")
            parts = msg.get("content", {}).get("parts", [])
            text = " ".join(p for p in parts if isinstance(p, str))
            if text and role in ("user", "assistant"):
                chain.append({"role": role, "content": text})
        node_id = node_data.get("parent")

    chain.reverse()

    total = len(chain)
    page = chain[validated.offset : validated.offset + validated.limit]

    return {
        "id": data.get("id", validated.conversation_id),
        "title": data.get("title", ""),
        "messages": page,
        "offset": validated.offset,
        "limit": validated.limit,
        "total": total,
        "has_more": validated.offset + len(page) < total,
    }


async def do_list_conversations(driver: CDPDriver, args: dict) -> dict:
    """List recent conversations."""
    validated = ListConversationsInput(**args)
    conversations = await driver.get_conversations(
        offset=validated.offset,
        limit=validated.limit,
    )
    return {
        "conversations": [
            {
                "id": c.get("id", ""),
                "title": c.get("title", "Untitled"),
                "update_time": c.get("update_time"),
                "gizmo_id": c.get("gizmo_id"),
            }
            for c in conversations
        ],
    }


async def do_delete_conversation(driver: CDPDriver, args: dict) -> dict:
    """Delete a conversation."""
    validated = DeleteConversationInput(**args)
    success = await driver.delete_conversation(validated.conversation_id)
    return {
        "success": success,
        "conversation_id": validated.conversation_id,
    }


async def do_create_project(driver: CDPDriver, args: dict) -> dict:
    """Create a new ChatGPT project."""
    validated = CreateProjectInput(**args)
    result = await driver.create_project(
        name=validated.name,
        instructions=validated.instructions,
        memory_scope=validated.memory_scope,
    )
    return result


async def do_delete_project(driver: CDPDriver, args: dict) -> dict:
    """Delete a ChatGPT project."""
    validated = DeleteProjectInput(**args)
    return await driver.delete_project(validated.project_id)


async def do_update_project_instructions(driver: CDPDriver, args: dict) -> dict:
    """Update a project's custom instructions."""
    validated = UpdateProjectInstructionsInput(**args)
    success = await driver.update_project_instructions(
        project_id=validated.project_id,
        instructions=validated.instructions,
    )
    return {
        "success": success,
        "project_id": validated.project_id,
    }


async def do_archive_conversation(driver: CDPDriver, args: dict) -> dict:
    """Archive or unarchive a conversation."""
    validated = ArchiveConversationInput(**args)
    success = await driver.archive_conversation(
        conversation_id=validated.conversation_id,
        archive=validated.archive,
    )
    return {
        "success": success,
        "conversation_id": validated.conversation_id,
        "archived": validated.archive,
    }


async def do_list_memories(driver: CDPDriver) -> dict:
    """List all ChatGPT memories."""
    memories = await driver.get_memories()
    # Normalize memory items
    result = []
    for m in memories:
        if isinstance(m, dict):
            result.append({
                "id": m.get("id", ""),
                "content": m.get("content", m.get("text", "")),
                "created_at": m.get("created_at", ""),
            })
    return {"memories": result}


async def do_create_memory(
    driver: CDPDriver, args: dict,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Create a new ChatGPT memory.

    create_memory drives a short ChatGPT exchange internally (it uses
    send_and_stream), so it accepts the same on_progress hook for parity.
    The response is usually one sentence, so the hook rarely fires here —
    but if it does, it keeps a slow confirmation from tripping the client
    timeout just like a full chat_completion.
    """
    validated = CreateMemoryInput(**args)
    await _notify(on_progress, "Creating memory…")
    result = await driver.create_memory(content=validated.content)
    return result


async def do_delete_memory(driver: CDPDriver, args: dict) -> dict:
    """Delete a ChatGPT memory."""
    validated = DeleteMemoryInput(**args)
    success = await driver.delete_memory(memory_id=validated.memory_id)
    return {"success": success, "memory_id": validated.memory_id}


async def do_list_gpts(driver: CDPDriver) -> dict:
    """List Custom GPTs."""
    gpts = await driver.list_gpts()
    return {
        "gpts": [
            {
                "id": g.get("id", ""),
                "name": g.get("name", ""),
                "description": g.get("description", ""),
            }
            for g in gpts
        ],
    }


async def do_list_project_files(driver: CDPDriver, args: dict) -> dict:
    """List files in a project."""
    validated = ListProjectFilesInput(**args)
    files = await driver.get_project_files(project_id=validated.project_id)
    return {
        "files": files,
        "project_id": validated.project_id,
    }


async def do_chat_with_gpt(
    driver: CDPDriver, args: dict,
    on_progress: ProgressCallback | None = None,
) -> dict:
    """Chat with a specific Custom GPT."""
    validated = ChatWithGptInput(**args)
    await driver.navigate_gpt(gizmo_id=validated.gpt_id)
    full_response = ""
    conv_id = ""
    chunk_count = 0
    async for chunk in driver.send_and_stream(validated.message, timeout=120):
        if chunk.delta:
            full_response += chunk.delta
            chunk_count += 1
            if chunk_count == 1:
                await _notify(on_progress, "Assistant is responding…")
            elif chunk_count % _PROGRESS_EVERY_N_CHUNKS == 0:
                await _notify(on_progress, f"Streaming… {len(full_response)} chars")
        if chunk.finish_reason:
            conv_id = driver._current_conv_id or ""
            await _notify(on_progress, "Finalizing…")
    return {
        "content": full_response,
        "model": "gpt",
        "conversation_id": conv_id,
        "gpt_id": validated.gpt_id,
    }


# ═══════════════════════════════════════════════════════════════
# Tool Definitions — declarative list with full annotations
# ═══════════════════════════════════════════════════════════════

def _build_tools() -> list[mcp_types.Tool]:
    """Build the FULL list of tool definitions (all 15), unfiltered.

    Returns every tool regardless of access gates. Used by tests that
    assert the complete catalog. Runtime tool exposure goes through
    :func:`build_tools`, which applies the access gates.
    """
    return [
        # ── Core: Chat ────────────────────────────────────────
        mcp_types.Tool(
            name=ToolName.CHAT_COMPLETION.value,
            title="ChatGPT Completion",
            description=(
                "Send a message to ChatGPT and receive a response. "
                "This is the primary tool for interacting with ChatGPT.\n\n"
                "Context persistence levels:\n"
                "• No project_id → ephemeral chat (or auto-continue last conversation)\n"
                "• With project_id → project-scoped persistent memory and custom instructions\n"
                "• With conversation_id → resume a specific conversation\n\n"
                "Auto-continue behavior: if you omit conversation_id, the tool continues "
                "the last conversation automatically (unless system_prompt or project_id changes). "
                "This makes multi-turn conversations seamless — just call this tool again "
                "with the next message."
            ),
            inputSchema=ChatCompletionInput.model_json_schema(),
            outputSchema=CHAT_COMPLETION_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="ChatGPT Completion",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        ),
        # ── Core: Discovery ──────────────────────────────────
        mcp_types.Tool(
            name=ToolName.LIST_MODELS.value,
            title="List Models",
            description=(
                "List all ChatGPT models available on your account. "
                "Returns model slugs (like 'auto', 'gpt-5-5', 'gpt-5-mini') "
                "that can be used as the 'model' parameter in chat_completion.\n\n"
                "Model selection guide:\n"
                "• auto — best for most tasks (system picks the right model)\n"
                "• gpt-5-5 — latest with reasoning, 34K context\n"
                "• gpt-5-mini — fast and cheap, no reasoning, 8K context"
            ),
            inputSchema=ListModelsInput.model_json_schema(),
            outputSchema=LIST_MODELS_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="List Models",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.LIST_PROJECTS.value,
            title="List Projects",
            description=(
                "List all ChatGPT projects. Each project is an isolated workspace with:\n"
                "• Persistent memory — ChatGPT remembers facts across conversations in the project\n"
                "• Custom instructions — a system prompt that applies to all project conversations\n"
                "• File attachments — a knowledge base for the project\n\n"
                "Memory scopes:\n"
                "• 'project_v2' — dedicated memory (isolated, no cross-contamination)\n"
                "• 'global' — shared memory (uses global ChatGPT memory pool)\n\n"
                "Use the project's 'id' as the 'project_id' parameter in chat_completion."
            ),
            inputSchema=ListProjectsInput.model_json_schema(),
            outputSchema=LIST_PROJECTS_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="List Projects",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        # ── Conversations ─────────────────────────────────────
        mcp_types.Tool(
            name=ToolName.LIST_CONVERSATIONS.value,
            title="List Conversations",
            description=(
                "List recent ChatGPT conversations, ordered by last update. "
                "Returns conversation IDs, titles, and timestamps. "
                "Use this to find a conversation_id for resuming with chat_completion "
                "or for retrieving full history with get_conversation.\n\n"
                "Each conversation's 'gizmo_id' field indicates which project it belongs to "
                "(null means it's a standalone conversation)."
            ),
            inputSchema=ListConversationsInput.model_json_schema(),
            outputSchema=LIST_CONVERSATIONS_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="List Conversations",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.GET_CONVERSATION.value,
            title="Get Conversation",
            description=(
                "Retrieve the message history of a conversation as a chronological "
                "list of user and assistant messages (oldest-first). "
                "Useful for reviewing what was discussed before continuing a conversation.\n\n"
                "Pagination: returns `limit` messages starting at `offset` (defaults: "
                "offset=0, limit=50). To read the ENTIRE conversation (not just the "
                "most recent page), page through by increasing offset by limit each "
                "call until has_more is false: "
                "get_conversation(id, offset=0, limit=50), then offset=50, offset=100, … . "
                "If a single page's result is truncated before reaching you, lower limit "
                "(e.g. 15) and retry — long messages can overflow a tool-result budget."
            ),
            inputSchema=GetConversationInput.model_json_schema(),
            outputSchema=GET_CONVERSATION_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Get Conversation",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.DELETE_CONVERSATION.value,
            title="Delete Conversation",
            description=(
                "Delete a conversation permanently. The conversation is removed from "
                "the ChatGPT sidebar and cannot be recovered. "
                "Use list_conversations first to find the conversation_id."
            ),
            inputSchema=DeleteConversationInput.model_json_schema(),
            outputSchema=DELETE_RESULT_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Delete Conversation",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        # ── Projects (write) ──────────────────────────────────
        mcp_types.Tool(
            name=ToolName.CREATE_PROJECT.value,
            title="Create Project",
            description=(
                "Create a new ChatGPT project — an isolated workspace with persistent memory, "
                "custom instructions, and file attachments.\n\n"
                "Memory scope options:\n"
                "• 'project_v2' (default) — dedicated memory. ChatGPT only remembers facts "
                "from conversations within this project. No cross-contamination with other chats. "
                "Best for: isolated tasks, specific domains, sensitive contexts.\n"
                "• 'global' — shared memory. Uses ChatGPT's global memory pool. "
                "Best for: general-purpose projects that benefit from cross-chat context.\n\n"
                "After creating a project, use its 'id' as the 'project_id' in chat_completion "
                "to start conversations within the project."
            ),
            inputSchema=CreateProjectInput.model_json_schema(),
            outputSchema=CREATE_PROJECT_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Create Project",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.DELETE_PROJECT.value,
            title="Delete Project",
            description=(
                "Permanently delete a ChatGPT project by ID. Use list_projects first to "
                "find the project_id (g-p-...). Deletion is irreversible — the project, "
                "its instructions, and its dedicated memory are removed. Hidden behind "
                "W2A_ENABLE_DESTRUCTIVE=1."
            ),
            inputSchema=DeleteProjectInput.model_json_schema(),
            outputSchema=DELETE_PROJECT_RESULT_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Delete Project",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.UPDATE_PROJECT_INSTRUCTIONS.value,
            title="Update Project Instructions",
            description=(
                "Update the custom instructions (system prompt) for an existing ChatGPT project. "
                "The new instructions replace any existing ones and apply to all future "
                "conversations created within the project. "
                "Existing conversations are not retroactively affected.\n\n"
                "Instructions act as a persistent system prompt for the project — "
                "unlike the system_prompt parameter in chat_completion which is per-message, "
                "project instructions persist across all conversations in the project."
            ),
            inputSchema=UpdateProjectInstructionsInput.model_json_schema(),
            outputSchema=UPDATE_INSTRUCTIONS_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Update Project Instructions",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        # ── Archive ────────────────────────────────────────────
        mcp_types.Tool(
            name=ToolName.ARCHIVE_CONVERSATION.value,
            title="Archive Conversation",
            description=(
                "Archive or unarchive a conversation. "
                "Archived conversations are hidden from the sidebar but not deleted. "
                "Pass archive=false to unarchive. "
                "This is reversible — use this instead of delete_conversation when unsure."
            ),
            inputSchema=ArchiveConversationInput.model_json_schema(),
            outputSchema=ARCHIVE_RESULT_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Archive Conversation",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        # ── Memory ─────────────────────────────────────────────
        mcp_types.Tool(
            name=ToolName.LIST_MEMORIES.value,
            title="List Memories",
            description=(
                "List all facts ChatGPT remembers about the user. "
                "ChatGPT's memory stores personal preferences, context, and facts "
                "that persist across all conversations. "
                "Memory is separate from conversation history — it survives even after conversations are deleted."
            ),
            inputSchema=ListMemoriesInput.model_json_schema(),
            outputSchema=LIST_MEMORIES_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="List Memories",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.CREATE_MEMORY.value,
            title="Create Memory",
            description=(
                "Instruct ChatGPT to remember a new fact. "
                "This works by sending a chat message asking ChatGPT to remember — "
                "the POST /backend-api/memories endpoint returns 405, so memory "
                "creation must go through conversation. ChatGPT may paraphrase "
                "or decline the request. Use list_memories to verify.\n\n"
                "Examples: 'I prefer Python over JavaScript', 'My project uses PostgreSQL 16', "
                "'Always respond in markdown with code examples'."
            ),
            inputSchema=CreateMemoryInput.model_json_schema(),
            outputSchema=CREATE_MEMORY_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Create Memory",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=False,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.DELETE_MEMORY.value,
            title="Delete Memory",
            description=(
                "Delete a specific memory from ChatGPT's persistent memory. "
                "Use list_memories first to find the memory_id. "
                "Deletion is permanent — ChatGPT will no longer remember this fact."
            ),
            inputSchema=DeleteMemoryInput.model_json_schema(),
            outputSchema=DELETE_MEMORY_RESULT_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Delete Memory",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
        # ── Custom GPTs ────────────────────────────────────────
        mcp_types.Tool(
            name=ToolName.LIST_GPTS.value,
            title="List Custom GPTs",
            description=(
                "List all Custom GPTs available to the account. "
                "Custom GPTs are specialized assistants created by users or OpenAI — "
                "each has unique capabilities, knowledge, and personality. "
                "Use chat_with_gpt to interact with a specific GPT."
            ),
            inputSchema=ListGptsInput.model_json_schema(),
            outputSchema=LIST_GPTS_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="List Custom GPTs",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        ),
        mcp_types.Tool(
            name=ToolName.CHAT_WITH_GPT.value,
            title="Chat with Custom GPT",
            description=(
                "Send a message to a specific Custom GPT and receive a response. "
                "Each GPT has its own system prompt, knowledge base, and capabilities. "
                "Use list_gpts to discover available GPTs, then pass the gpt_id to this tool.\n\n"
                "This navigates to the GPT's page in the browser, so it's slower than "
                "chat_completion for simple tasks. Use it when you need GPT-specific capabilities."
            ),
            inputSchema=ChatWithGptInput.model_json_schema(),
            outputSchema=CHAT_COMPLETION_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="Chat with Custom GPT",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=False,
                openWorldHint=True,
            ),
        ),
        # ── Project Files ──────────────────────────────────────
        mcp_types.Tool(
            name=ToolName.LIST_PROJECT_FILES.value,
            title="List Project Files",
            description=(
                "List files attached to a ChatGPT project. "
                "Projects can have uploaded documents that serve as a knowledge base. "
                "ChatGPT references these files when answering questions in the project."
            ),
            inputSchema=ListProjectFilesInput.model_json_schema(),
            outputSchema=LIST_PROJECT_FILES_OUTPUT,
            annotations=mcp_types.ToolAnnotations(
                title="List Project Files",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        ),
    ]


def build_tools() -> list[mcp_types.Tool]:
    """Build the *visible* tool list, applying access gates.

    Filters :func:`_build_tools` by the current environment:
      - SAFE tools (reads + chat) are always returned.
      - Write tools require ``W2A_ENABLE_WRITE=1``.
      - Destructive tools require ``W2A_ENABLE_DESTRUCTIVE=1``.

    This is the runtime entrypoint used by ``list_tools``. Every
    returned tool carries honest ``noauth`` auth metadata when the
    server has no API keys configured.
    """
    visible = _visible_tool_names()
    gated = [t for t in _build_tools() if t.name in visible]
    # Stamp auth metadata on every exposed tool
    for tool in gated:
        tool.meta = tool_meta()
    return gated


# ═══════════════════════════════════════════════════════════════
# Server Factory
# ═══════════════════════════════════════════════════════════════

def create_server() -> Server:
    """Create and configure the MCP server with all capabilities."""

    server = Server("chatgpt-web2api")

    def _make_progress_callback() -> ProgressCallback | None:
        """Build a best-effort progress notifier from the in-flight MCP request.

        Returns None when there is no usable progress channel: outside a
        request (direct call / unit test) or when the client sent no
        ``_meta.progressToken``. Business functions check for None and skip
        emitting.

        The returned counter is monotonic and persists across rate-limit
        retries (it's bound to the outer call_tool invocation, and the retry
        wrapper re-enters the business function without rebuilding it). Note
        for future debugging: the *message* may visually "reset" across a
        retry because ChatGPT re-types and re-streams the response from
        scratch, while the numeric progress counter keeps climbing. This is
        expected — the text genuinely restarts; only the counter is stable.
        """
        try:
            ctx = server.request_context
        except LookupError:
            return None  # not inside a request (direct call / test)
        token = ctx.meta.progressToken if ctx.meta else None
        if token is None:
            return None  # client didn't ask for progress
        counter = 0

        async def _cb(message: str) -> None:
            nonlocal counter
            counter += 1
            # Best-effort: a dropped notification (network blip, session
            # closed mid-stream) must NEVER abort the tool call — that would
            # kill a 40s generation over a transient transport issue.
            try:
                await ctx.session.send_progress_notification(
                    progress_token=token,
                    progress=counter,
                    message=message,
                )
            except Exception:
                logger.debug(
                    "progress notification dropped (transport error)",
                    exc_info=True,
                )

        return _cb

    # ── Tools (model-controlled) ──────────────────────────────

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return build_tools()

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict
    ) -> tuple[list[mcp_types.TextContent], dict] | list[mcp_types.TextContent] | dict:
        """Route tool calls to business logic functions."""
        if _driver is None:
            raise ConnectionError(
                "Not connected to Chrome. Run 'chatgpt-web2api' first."
            )

        # Build once per request: the callback reads server.request_context,
        # which is request-scoped. None when the client can't receive progress.
        on_progress = _make_progress_callback()

        handlers = {
            ToolName.CHAT_COMPLETION.value: lambda: do_chat_completion(_driver, arguments, _config, on_progress),
            ToolName.LIST_MODELS.value: lambda: do_list_models(_driver),
            ToolName.LIST_PROJECTS.value: lambda: do_list_projects(_driver),
            ToolName.GET_CONVERSATION.value: lambda: do_get_conversation(_driver, arguments),
            ToolName.LIST_CONVERSATIONS.value: lambda: do_list_conversations(_driver, arguments),
            ToolName.DELETE_CONVERSATION.value: lambda: do_delete_conversation(_driver, arguments),
            ToolName.CREATE_PROJECT.value: lambda: do_create_project(_driver, arguments),
            ToolName.DELETE_PROJECT.value: lambda: do_delete_project(_driver, arguments),
            ToolName.UPDATE_PROJECT_INSTRUCTIONS.value: lambda: do_update_project_instructions(_driver, arguments),
            ToolName.ARCHIVE_CONVERSATION.value: lambda: do_archive_conversation(_driver, arguments),
            ToolName.LIST_MEMORIES.value: lambda: do_list_memories(_driver),
            ToolName.CREATE_MEMORY.value: lambda: do_create_memory(_driver, arguments, on_progress),
            ToolName.DELETE_MEMORY.value: lambda: do_delete_memory(_driver, arguments),
            ToolName.LIST_GPTS.value: lambda: do_list_gpts(_driver),
            ToolName.CHAT_WITH_GPT.value: lambda: do_chat_with_gpt(_driver, arguments, on_progress),
            ToolName.LIST_PROJECT_FILES.value: lambda: do_list_project_files(_driver, arguments),
        }

        handler = handlers.get(name)
        if not handler:
            raise ValueError(f"Unknown tool: {name}")

        # Defense-in-depth: refuse gated tools even when called by name.
        # A client could call a tool that isn't in list_tools; the call
        # must be rejected rather than silently executed.
        gate = _tool_gate_env(name)
        if gate is not None and not _env_enabled(gate):
            raise PermissionError(
                f"Tool '{name}' is not enabled. Set {gate}=1 to expose it."
            )

        # Tools whose business logic drives ChatGPT chat (and can hit the rate
        # limit). These get transparent retry: a transient "Too many requests"
        # pop-up is dismissed and retried so the agent never sees it. Only a
        # persistent limit surfaces — as a structured error result below.
        _CHAT_TOOLS = frozenset({
            ToolName.CHAT_COMPLETION.value, ToolName.CHAT_WITH_GPT.value,
            ToolName.CREATE_MEMORY.value,  # uses send_and_stream internally
        })

        async def _run() -> dict:
            """Execute the handler, with transparent rate-limit retry for chat tools."""
            if name in _CHAT_TOOLS:
                # on_progress is the same callback the lambda captures and
                # passes into the business function; here it's also used by
                # retry_on_rate_limit to signal the backoff pause. Same object
                # by design — two injection points, one notifier.
                return await retry_on_rate_limit(_driver, handler, on_progress=on_progress)
            return await handler()

        # Serialize mutating tools through the cross-process lock
        try:
            if name in _MUTATING_TOOLS and _lock_cdp_port is not None:
                async with CrossProcessLock(cdp_port=_lock_cdp_port):
                    result = await _run()
            else:
                result = await _run()
        except RateLimitError as e:
            # Persistent limit (transparent retries exhausted). Signal it as an
            # error result with a recognizable marker the agent can parse to
            # decide "pause, then retry this tool." isError=True with text only
            # — no structuredContent, because the MCP SDK validates
            # structuredContent against the tool's outputSchema and this error
            # payload deliberately doesn't match any tool's success schema.
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text=(
                        f"ChatGPT rate limit reached. Retry in {e.retry_after}s. "
                        f"(rate_limit_exceeded, retry_after={e.retry_after})"
                    ),
                )],
                isError=True,
            )
        except AuthExpiredError:
            # The access token is stale/rejected. Surface a clear signal so the
            # agent/user can prompt re-login instead of misdiagnosing the
            # resulting empty reads as a different bug. isError=True with text
            # content only — no structuredContent, because the MCP SDK validates
            # structuredContent against the tool's outputSchema and this error
            # payload deliberately doesn't match any tool's success schema.
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text="ChatGPT session expired — re-login required. (auth_expired)",
                )],
                isError=True,
            )
        except GenerationStuckError as e:
            # Generation hung (no DOM progress within the stall window). Distinct
            # from a slow generation (which keeps progressing and is allowed the
            # full timeout). Phase + duration in the text for diagnosis.
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text=(
                        f"Generation stuck in {e.phase} for {e.stalled_for_s:.0f}s "
                        f"— no DOM progress. (generation_stuck)"
                    ),
                )],
                isError=True,
            )
        except LockAcquisitionError:
            # Another process holds the cross-process lock for this Chrome tab
            # and didn't release it within the timeout. The caller should retry.
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text="Browser busy — another operation in progress. Retry later. (lock_timeout)",
                )],
                isError=True,
            )

        # chat_completion and chat_with_gpt return both text + structured output
        if name in (ToolName.CHAT_COMPLETION.value, ToolName.CHAT_WITH_GPT.value):
            text_content = [mcp_types.TextContent(type="text", text=result["content"])]
            return text_content, result

        # Status operations return status text + structured output
        status_tools = (
            ToolName.DELETE_CONVERSATION.value,
            ToolName.UPDATE_PROJECT_INSTRUCTIONS.value,
            ToolName.DELETE_PROJECT.value,
            ToolName.ARCHIVE_CONVERSATION.value,
            ToolName.DELETE_MEMORY.value,
        )
        if name in status_tools:
            status = "succeeded" if result.get("success") else "failed"
            text_content = [mcp_types.TextContent(type="text", text=f"Operation {status}")]
            return text_content, result

        # Everything else returns structured only (SDK auto-wraps as text JSON)
        return result

    # ── Resources (application-controlled) ────────────────────

    @server.list_resources()
    async def list_resources() -> list[mcp_types.Resource]:
        """List static resources — models and account info."""
        resources = [
            mcp_types.Resource(
                uri="chatgpt://models",
                name="Available Models",
                description="All ChatGPT model slugs available on the account",
                mimeType="application/json",
            ),
            mcp_types.Resource(
                uri="chatgpt://account",
                name="Account Info",
                description="Current ChatGPT account status and user info",
                mimeType="application/json",
            ),
        ]

        if _driver:
            try:
                projects = await _driver.get_projects()
                for p in projects:
                    if p.get("id"):
                        resources.append(
                            mcp_types.Resource(
                                uri=f"chatgpt://projects/{p['id']}",
                                name=p.get("name", "Unknown Project"),
                                description=(
                                    f"ChatGPT project: {p.get('name', 'Unknown')} "
                                    f"({p.get('memory_scope', 'project_v2')} memory)"
                                ),
                                mimeType="application/json",
                            )
                        )
            except Exception as e:
                logger.warning("Failed to list project resources: %s", e)

        return resources

    @server.list_resource_templates()
    async def list_resource_templates() -> list[mcp_types.ResourceTemplate]:
        """Declare URI templates for dynamic resource access."""
        return [
            mcp_types.ResourceTemplate(
                uriTemplate="chatgpt://projects/{project_id}",
                name="ChatGPT Project",
                description="Access a specific ChatGPT project by ID",
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def read_resource(
        request: mcp_types.ReadResourceRequest,
    ) -> str | list[mcp_types.ResourceContents]:
        """Read a specific resource by URI."""
        uri = str(request.params.uri)

        if _driver is None:
            raise ConnectionError("Not connected to Chrome")

        if uri == "chatgpt://models":
            models = await _driver.get_models()
            data = [{"id": m.get("slug", ""), "title": m.get("title", "")} for m in models]
            return json.dumps(data, ensure_ascii=False, indent=2)

        elif uri == "chatgpt://account":
            return json.dumps(
                {
                    "user": _driver._user_name,
                    "connected": _driver.is_connected,
                },
                ensure_ascii=False,
                indent=2,
            )

        elif uri.startswith("chatgpt://projects/"):
            project_id = uri.split("/")[-1]
            projects = await _driver.get_projects()
            for p in projects:
                if p.get("id") == project_id:
                    return json.dumps(p, ensure_ascii=False, indent=2)
            raise ValueError(f"Project not found: {project_id}")

        raise ValueError(f"Unknown resource URI: {uri}")

    # ── Prompts (user-controlled) ─────────────────────────────

    @server.list_prompts()
    async def list_prompts() -> list[mcp_types.Prompt]:
        return [
            mcp_types.Prompt(
                name="ask-chatgpt",
                description=(
                    "Send a question to ChatGPT with optional project context. "
                    "The model will use the chat_completion tool to get an answer."
                ),
                arguments=[
                    mcp_types.PromptArgument(
                        name="question",
                        description="The question to ask",
                        required=True,
                    ),
                    mcp_types.PromptArgument(
                        name="project",
                        description=(
                            "Project name or ID for scoped memory "
                            "(optional — uses default project if omitted)"
                        ),
                        required=False,
                    ),
                ],
            ),
            mcp_types.Prompt(
                name="continue-chat",
                description=(
                    "Continue the last conversation with a follow-up message. "
                    "Automatically uses the active conversation context."
                ),
                arguments=[
                    mcp_types.PromptArgument(
                        name="message",
                        description="Follow-up message to send",
                        required=True,
                    ),
                ],
            ),
        ]

    @server.get_prompt()
    async def get_prompt(
        request: mcp_types.GetPromptRequest,
    ) -> mcp_types.GetPromptResult:
        name = request.params.name
        args = request.params.arguments or {}

        if name == "ask-chatgpt":
            question = args.get("question", "")
            project = args.get("project", "")
            if not question:
                raise ValueError("question argument is required")

            tool_args: dict[str, Any] = {"message": question}

            if project and _driver:
                try:
                    projects = await _driver.get_projects()
                    for p in projects:
                        if project.lower() in (
                            p.get("name", "").lower(),
                            p.get("id", "").lower(),
                        ):
                            tool_args["project_id"] = p["id"]
                            break
                except Exception as e:
                    logger.warning("Project resolution failed: %s", e)

            return mcp_types.GetPromptResult(
                description=f"Ask ChatGPT: {question[:60]}",
                messages=[
                    mcp_types.SamplingMessage(
                        role="user",
                        content=mcp_types.TextContent(
                            type="text",
                            text=(
                                f"Use the chat_completion tool to answer this question. "
                                f"Call it with these arguments:\n"
                                f"```json\n{json.dumps(tool_args, indent=2)}\n```\n\n"
                                f"Return the response content directly to the user."
                            ),
                        ),
                    ),
                ],
            )

        elif name == "continue-chat":
            message = args.get("message", "")
            if not message:
                raise ValueError("message argument is required")

            return mcp_types.GetPromptResult(
                description=f"Continue chat: {message[:60]}",
                messages=[
                    mcp_types.SamplingMessage(
                        role="user",
                        content=mcp_types.TextContent(
                            type="text",
                            text=(
                                f"Use the chat_completion tool with this message. "
                                f"Do NOT pass conversation_id — the tool will auto-continue "
                                f"the last conversation.\n\n"
                                f"Message: {message}"
                            ),
                        ),
                    ),
                ],
            )

        raise ValueError(f"Unknown prompt: {name}")

    # ── Completion (argument autocomplete) ────────────────────

    @server.completion()
    async def handle_completion(
        ref: mcp_types.PromptReference | mcp_types.ResourceTemplateReference,
        argument: mcp_types.CompletionArgument,
        context: mcp_types.CompletionContext | None,
    ) -> mcp_types.Completion | None:
        """Provide autocomplete suggestions for prompt arguments."""
        if isinstance(ref, mcp_types.PromptReference):
            # Autocomplete 'project' argument in ask-chatgpt prompt
            if ref.name == "ask-chatgpt" and argument.name == "project":
                if _driver:
                    try:
                        projects = await _driver.get_projects()
                        names = [p.get("name", "") for p in projects if p.get("name")]
                        # Filter by what user has typed
                        prefix = argument.value.lower()
                        matches = [n for n in names if prefix in n.lower()]
                        return mcp_types.Completion(
                            values=matches[:20],
                            total=len(matches),
                            hasMore=len(matches) > 20,
                        )
                    except Exception:
                        pass
        return None

    return server


# ═══════════════════════════════════════════════════════════════
# Transport Layer
# ═══════════════════════════════════════════════════════════════


async def run_mcp(
    config: Config, transport: str = "stdio", port: int = 8090
) -> None:
    """Connect to Chrome and run the MCP server."""
    global _driver, _config, _lock_cdp_port

    _config = config
    _lock_cdp_port = config.chrome.cdp_port

    _driver = CDPDriver(
        cdp_port=config.chrome.cdp_port,
        tab_mode=config.chatgpt.tab_mode,
        instance_id=TabRegistry.derive_instance_id(
            cdp_port=config.chrome.cdp_port, server_identity="mcp",
        ),
    )
    try:
        await _driver.connect()
        logger.info("Connected to Chrome on CDP port %d", config.chrome.cdp_port)
    except Exception as e:
        logger.error(
            "Cannot connect to Chrome on CDP port %d. "
            "Run 'chatgpt-web2api' first to start Chrome. Error: %s",
            config.chrome.cdp_port,
            e,
        )
        # Clean up any tab that connect() may have created before failing
        await _driver.close()
        return

    server = create_server()
    init_options = server.create_initialization_options()

    try:
        if transport == "stdio":
            async with stdio_server() as (read, write):
                await server.run(read, write, init_options, raise_exceptions=True)
        elif transport == "sse":
            await _run_sse(server, init_options, config, port)
    finally:
        await _driver.close()


async def _run_sse(
    server: Server, init_options, config: Config, port: int
) -> None:
    """Run MCP server with SSE transport via Starlette + uvicorn.

    The MCP library's ``SseServerTransport`` is ASGI-native (built on
    ``sse_starlette``, designed for Starlette). The previous
    implementation tried to bridge aiohttp requests into ASGI scopes —
    that was broken since inception (``request.scope`` doesn't exist on
    aiohttp) and the aiohttp→ASGI rewrite couldn't flush SSE chunks to
    the wire. Instead of fighting the framework mismatch, we run a
    proper Starlette ASGI app under uvicorn for the SSE transport.

    The stdio transport is unaffected — it stays on its existing path.
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    warn_non_loopback(config.server.host, "SSE")

    sse = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], init_options, raise_exceptions=True
            )
        return Response()

    # handle_post_message is a raw ASGI app (scope, receive, send) that
    # sends its own HTTP response. Mount it directly — not as a Starlette
    # endpoint, which would try to wrap it in a second response.
    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages", app=sse.handle_post_message),
        ]
    )

    logger.info("MCP SSE server on http://%s:%d/sse", config.server.host, port)

    uconfig = uvicorn.Config(
        app,
        host=config.server.host,
        port=port,
        log_level="warning",
        loop="asyncio",
    )
    uvi = uvicorn.Server(uconfig)
    await uvi.serve()


# ═══════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="chatgpt-web2api-mcp",
        description="MCP server for ChatGPT-Web2API",
    )
    parser.add_argument("--config", "-c", help="Config file path")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport layer (default: stdio)",
    )
    parser.add_argument(
        "--port", type=int, default=8090, help="SSE port (default: 8090)"
    )
    parser.add_argument(
        "--cdp-port", type=int, help="Chrome CDP port (default: from config)"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)

    config = Config.load(args.config)
    if args.cdp_port:
        config.chrome.cdp_port = args.cdp_port

    try:
        asyncio.run(run_mcp(config, transport=args.transport, port=args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

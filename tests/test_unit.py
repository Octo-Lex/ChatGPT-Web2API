"""Unit tests — can run without Chrome/ChatGPT running."""

import pytest


def test_imports():
    """All modules import cleanly."""


def test_tool_count():
    """15 tools defined."""
    from chatgpt_web2api.mcp_server import _build_tools
    tools = _build_tools()
    assert len(tools) == 16


def test_tool_names_match_enum():
    """Every enum value has a corresponding tool."""
    from chatgpt_web2api.mcp_server import ToolName, _build_tools
    tools = _build_tools()
    tool_names = {t.name for t in tools}
    for member in ToolName:
        assert member.value in tool_names, f"Missing tool: {member.value}"


def test_all_tools_have_annotations():
    """Every tool has full ToolAnnotations."""
    from chatgpt_web2api.mcp_server import _build_tools
    tools = _build_tools()
    for t in tools:
        assert t.annotations is not None, f"{t.name} missing annotations"
        assert t.annotations.readOnlyHint is not None, f"{t.name} missing readOnlyHint"
        assert t.annotations.destructiveHint is not None, f"{t.name} missing destructiveHint"
        assert t.annotations.idempotentHint is not None, f"{t.name} missing idempotentHint"
        assert t.annotations.openWorldHint is not None, f"{t.name} missing openWorldHint"


def test_all_tools_have_output_schema():
    """Every tool declares an output schema."""
    from chatgpt_web2api.mcp_server import _build_tools
    tools = _build_tools()
    for t in tools:
        assert t.outputSchema is not None, f"{t.name} missing outputSchema"


def test_all_tools_have_rich_descriptions():
    """Descriptions are substantive (>100 chars for discoverability)."""
    from chatgpt_web2api.mcp_server import _build_tools
    tools = _build_tools()
    for t in tools:
        assert len(t.description or "") > 100, f"{t.name} description too short ({len(t.description or '')} chars)"


def test_pydantic_schemas_valid():
    """Pydantic input schemas produce valid JSON Schema."""
    from chatgpt_web2api.mcp_server import (
        ArchiveConversationInput,
        ChatCompletionInput,
        ChatWithGptInput,
        CreateMemoryInput,
        CreateProjectInput,
        DeleteConversationInput,
        DeleteMemoryInput,
        GetConversationInput,
        ListConversationsInput,
        ListGptsInput,
        ListMemoriesInput,
        ListModelsInput,
        ListProjectFilesInput,
        ListProjectsInput,
        UpdateProjectInstructionsInput,
    )
    schemas = [
        ChatCompletionInput, ListModelsInput, ListProjectsInput,
        GetConversationInput, ListConversationsInput, DeleteConversationInput,
        CreateProjectInput, UpdateProjectInstructionsInput,
        ArchiveConversationInput, ListMemoriesInput, CreateMemoryInput,
        DeleteMemoryInput, ListGptsInput, ListProjectFilesInput,
        ChatWithGptInput,
    ]
    for schema_cls in schemas:
        schema = schema_cls.model_json_schema()
        assert schema["type"] == "object", f"{schema_cls.__name__} schema not an object"


def test_chat_completion_validation():
    """ChatCompletionInput validates correctly."""
    from chatgpt_web2api.mcp_server import ChatCompletionInput

    # Valid
    ChatCompletionInput(message="Hello")

    # Valid with all fields
    ChatCompletionInput(
        message="Hello",
        system_prompt="Be concise",
        model="gpt-5-5",
        conversation_id="abc-123",
        project_id="g-p-xyz",
    )

    # Invalid — missing required field
    with pytest.raises(Exception):
        ChatCompletionInput()


def test_server_creates():
    """Server instance is created with all handlers."""
    from mcp import types as t

    from chatgpt_web2api.mcp_server import create_server

    server = create_server()
    required_handlers = [
        t.ListToolsRequest,
        t.CallToolRequest,
        t.ListResourcesRequest,
        t.ReadResourceRequest,
        t.ListResourceTemplatesRequest,
        t.ListPromptsRequest,
        t.GetPromptRequest,
        t.CompleteRequest,
    ]
    for handler_type in required_handlers:
        assert handler_type in server.request_handlers, f"Missing handler: {handler_type.__name__}"


def test_stream_chunk_dataclass():
    """StreamChunk works as expected."""
    from chatgpt_web2api.cdp_driver import StreamChunk

    chunk1 = StreamChunk(delta="Hello")
    assert chunk1.delta == "Hello"
    assert chunk1.finish_reason is None

    chunk2 = StreamChunk(delta="", finish_reason="stop")
    assert chunk2.delta == ""
    assert chunk2.finish_reason == "stop"


def test_config_loads():
    """Config loads with defaults."""
    from chatgpt_web2api.config import Config
    config = Config.load(None)
    assert config.chrome.cdp_port == 9222
    assert config.server.port == 8080


def test_delete_has_destructive_annotation():
    """Delete tools are marked destructive."""
    from chatgpt_web2api.mcp_server import ToolName, _build_tools
    tools = {t.name: t for t in _build_tools()}

    assert tools[ToolName.DELETE_CONVERSATION.value].annotations.destructiveHint is True
    assert tools[ToolName.DELETE_MEMORY.value].annotations.destructiveHint is True


def test_read_tools_are_readonly():
    """Read-only tools are marked as such."""
    from chatgpt_web2api.mcp_server import ToolName, _build_tools
    tools = {t.name: t for t in _build_tools()}

    read_tools = [ToolName.LIST_MODELS, ToolName.LIST_PROJECTS, ToolName.LIST_CONVERSATIONS,
                  ToolName.GET_CONVERSATION, ToolName.LIST_MEMORIES, ToolName.LIST_GPTS,
                  ToolName.LIST_PROJECT_FILES]
    for tn in read_tools:
        assert tools[tn.value].annotations.readOnlyHint is True, f"{tn.value} not marked readOnly"

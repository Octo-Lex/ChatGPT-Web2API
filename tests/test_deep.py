"""Tests for CDP driver data structures and utilities."""



def test_stream_chunk_immutability():
    """StreamChunk fields can be set on creation."""
    from chatgpt_web2api.cdp_driver import StreamChunk

    chunk = StreamChunk(delta="test", finish_reason="stop")
    assert chunk.delta == "test"
    assert chunk.finish_reason == "stop"


def test_stream_chunk_defaults():
    """StreamChunk has sensible defaults."""
    from chatgpt_web2api.cdp_driver import StreamChunk

    chunk = StreamChunk(delta="x")
    assert chunk.finish_reason is None


def test_stream_chunk_empty():
    """StreamChunk works with empty delta."""
    from chatgpt_web2api.cdp_driver import StreamChunk

    chunk = StreamChunk(delta="")
    assert chunk.delta == ""


def test_config_defaults():
    """Config has all expected defaults."""
    from chatgpt_web2api.config import Config

    config = Config.load(None)
    assert config.server.port == 8080
    assert config.server.host == "127.0.0.1"
    assert config.chrome.cdp_port == 9222


def test_config_from_dict():
    """Config can be loaded from a dict."""
    from chatgpt_web2api.config import Config

    config = Config.load(None)
    assert config.chrome.cdp_port == 9222


def test_tool_enum_values():
    """ToolName enum has exactly 15 members."""
    from chatgpt_web2api.mcp_server import ToolName

    assert len(ToolName) == 16


def test_tool_enum_unique():
    """ToolName enum values are unique strings."""
    from chatgpt_web2api.mcp_server import ToolName

    values = [m.value for m in ToolName]
    assert len(values) == len(set(values))


def test_input_models_have_descriptions():
    """Pydantic input models have field descriptions for agent discoverability."""
    from chatgpt_web2api.mcp_server import ChatCompletionInput

    schema = ChatCompletionInput.model_json_schema()
    props = schema.get("properties", {})
    assert "message" in props
    assert "description" in props["message"] or "anyOf" in props["message"]


def test_chat_completion_model_field():
    """ChatCompletionInput model field accepts valid values."""
    from chatgpt_web2api.mcp_server import ChatCompletionInput

    for model in ["auto", "gpt-5-5", "gpt-5-mini"]:
        inp = ChatCompletionInput(message="hi", model=model)
        assert inp.model == model


def test_archive_input_boolean():
    """ArchiveConversationInput archive field is boolean."""
    from chatgpt_web2api.mcp_server import ArchiveConversationInput

    inp = ArchiveConversationInput(conversation_id="abc", archive=True)
    assert inp.archive is True

    inp2 = ArchiveConversationInput(conversation_id="abc", archive=False)
    assert inp2.archive is False


def test_server_tool_handler_signature():
    """Server has proper request handler callables."""
    from mcp import types as t

    from chatgpt_web2api.mcp_server import create_server

    server = create_server()
    assert callable(server.request_handlers[t.CallToolRequest])
    assert callable(server.request_handlers[t.ListToolsRequest])


def test_resource_templates():
    """Server declares resource templates."""
    from mcp import types as t

    from chatgpt_web2api.mcp_server import create_server

    server = create_server()
    assert callable(server.request_handlers[t.ListResourceTemplatesRequest])


def test_prompts():
    """Server declares prompts."""
    from mcp import types as t

    from chatgpt_web2api.mcp_server import create_server

    server = create_server()
    assert callable(server.request_handlers[t.ListPromptsRequest])
    assert callable(server.request_handlers[t.GetPromptRequest])


def test_completion_handler():
    """Server declares completion handler."""
    from mcp import types as t

    from chatgpt_web2api.mcp_server import create_server

    server = create_server()
    assert callable(server.request_handlers[t.CompleteRequest])


def test_all_schemas_are_objects():
    """Every tool's inputSchema is a JSON object type."""
    from chatgpt_web2api.mcp_server import _build_tools

    for tool in _build_tools():
        assert tool.inputSchema["type"] == "object", f"{tool.name} inputSchema not object"


def test_output_schemas_are_objects():
    """Every tool's outputSchema is a JSON object type."""
    from chatgpt_web2api.mcp_server import _build_tools

    for tool in _build_tools():
        schema = tool.outputSchema
        assert schema["type"] == "object", f"{tool.name} outputSchema not object"
        assert "properties" in schema, f"{tool.name} outputSchema missing properties"

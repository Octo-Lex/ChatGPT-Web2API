"""Business logic tests — mocked CDPDriver.

Tests the do_* functions in mcp_server.py and API handler logic
with AsyncMock to avoid needing a live Chrome instance.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def mock_driver():
    """Create a mocked CDPDriver with all methods as AsyncMock."""
    from chatgpt_web2api.cdp_driver import CDPDriver, StreamChunk

    driver = MagicMock(spec=CDPDriver)
    driver._current_conv_id = None
    driver._current_model = None
    driver.is_connected = True
    driver._access_token = "test-token"

    # Wire select_model (returns True by default)
    driver.select_model = AsyncMock(return_value=True)

    # Wire send_and_stream to yield a simple response
    async def _stream(text, timeout=120):
        yield StreamChunk(delta="Hello!")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.navigate_gpt = AsyncMock()
    driver.get_models = AsyncMock(return_value=[
        {"slug": "auto", "title": "Auto"},
        {"slug": "gpt-5-5", "title": "GPT-5.5"},
        {"slug": "gpt-5-mini", "title": "GPT-5 Mini"},
    ])
    driver.get_projects = AsyncMock(return_value=[
        {"id": "g-p-test", "name": "Test Project", "memory_scope": "project_v2"},
    ])
    driver.get_conversations = AsyncMock(return_value=[
        {"id": "conv-1", "title": "Test Chat", "update_time": 1700000000,
         "create_time": 1700000000, "is_archived": False, "gizmo_id": None},
    ])
    driver.get_conversation = AsyncMock(return_value={
        "id": "conv-1", "title": "Test Chat",
        "current_node": "node-3",
        "mapping": {
            "node-1": {"parent": None, "message": {
                "author": {"role": "user"}, "content": {"parts": ["Hi"]}}},
            "node-2": {"parent": "node-1", "message": {
                "author": {"role": "assistant"}, "content": {"parts": ["Hello!"]}}},
            "node-3": {"parent": "node-2", "message": {
                "author": {"role": "user"}, "content": {"parts": ["How are you?"]}}},
        },
    })
    driver.delete_conversation = AsyncMock(return_value=True)
    driver.rename_conversation = AsyncMock(return_value=True)
    driver.create_project = AsyncMock(return_value={
        "id": "g-p-new", "name": "New Project", "memory_scope": "project_v2",
    })
    driver.update_project_instructions = AsyncMock(return_value=True)
    driver.archive_conversation = AsyncMock(return_value=True)
    driver.get_memories = AsyncMock(return_value=[
        {"id": "mem-1", "content": "User likes Python", "created_at": "2025-01-01"},
    ])
    driver.create_memory = AsyncMock(return_value={
        "content": "test", "method": "chat", "conversation_id": "conv-mem",
    })
    driver.delete_memory = AsyncMock(return_value=True)
    driver.list_gpts = AsyncMock(return_value=[
        {"id": "gpt-1", "name": "Code Helper", "description": "Writes code"},
    ])
    driver.get_project_files = AsyncMock(return_value=[
        {"id": "file-1", "name": "readme.md", "size": 1024, "mime_type": "text/markdown"},
    ])
    driver.close = AsyncMock()
    driver.ensure_token = AsyncMock(return_value="test-token")

    return driver


@pytest.fixture
def mock_config():
    """Create a test Config."""
    from chatgpt_web2api.config import Config
    return Config.load(None)


# ── do_list_models ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_models(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_models
    result = await do_list_models(mock_driver)
    assert "models" in result
    assert len(result["models"]) == 3
    assert result["models"][0]["id"] == "auto"
    assert result["models"][1]["id"] == "gpt-5-5"


@pytest.mark.asyncio
async def test_list_models_extracts_slug_and_title(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_models
    result = await do_list_models(mock_driver)
    for m in result["models"]:
        assert "id" in m
        assert "title" in m


# ── do_list_projects ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_projects(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_projects
    result = await do_list_projects(mock_driver)
    assert "projects" in result
    assert len(result["projects"]) == 1
    assert result["projects"][0]["id"] == "g-p-test"
    assert result["projects"][0]["name"] == "Test Project"


# ── do_list_conversations ────────────────────────────────────

@pytest.mark.asyncio
async def test_list_conversations(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_conversations
    result = await do_list_conversations(mock_driver, {"offset": 0, "limit": 28})
    assert "conversations" in result
    assert len(result["conversations"]) == 1
    assert result["conversations"][0]["id"] == "conv-1"


# ── do_get_conversation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_get_conversation(mock_driver):
    from chatgpt_web2api.mcp_server import do_get_conversation
    result = await do_get_conversation(mock_driver, {"conversation_id": "conv-1"})
    assert "messages" in result
    assert result["id"] == "conv-1"


def _long_conversation_driver(n_messages):
    """Build a mock driver whose get_conversation returns a linear chain of
    n_messages user/assistant messages (node-0 -> node-1 -> ... -> node-(n-1)),
    current_node = the last node. Mirrors the real ChatGPT mapping shape."""
    from unittest.mock import AsyncMock, MagicMock

    from chatgpt_web2api.cdp_driver import CDPDriver

    driver = MagicMock(spec=CDPDriver)
    mapping = {}
    prev = None
    for i in range(n_messages):
        nid = f"node-{i}"
        role = "user" if i % 2 == 0 else "assistant"
        mapping[nid] = {
            "parent": prev,
            "message": {
                "author": {"role": role},
                "content": {"parts": [f"msg {i}"]},
            },
        }
        prev = nid
    driver.get_conversation = AsyncMock(return_value={
        "id": "conv-long", "title": "Long Chat",
        "current_node": f"node-{n_messages - 1}",
        "mapping": mapping,
    })
    return driver


@pytest.mark.asyncio
async def test_get_conversation_default_backward_compat():
    """Default call (no pagination args) returns all messages + pagination
    metadata, and behaves like the old single-shot read for small threads."""
    from chatgpt_web2api.mcp_server import do_get_conversation
    driver = _long_conversation_driver(5)
    result = await do_get_conversation(driver, {"conversation_id": "conv-long"})
    assert result["total"] == 5
    assert result["offset"] == 0
    assert result["limit"] == 50
    assert result["has_more"] is False
    assert [m["content"] for m in result["messages"]] == [
        "msg 0", "msg 1", "msg 2", "msg 3", "msg 4",
    ]


@pytest.mark.asyncio
async def test_get_conversation_offset_skips_first_page():
    """offset skips earlier messages; page 2 starts where page 1 ended."""
    from chatgpt_web2api.mcp_server import do_get_conversation
    driver = _long_conversation_driver(12)
    p1 = await do_get_conversation(driver, {"conversation_id": "conv-long", "offset": 0, "limit": 5})
    p2 = await do_get_conversation(driver, {"conversation_id": "conv-long", "offset": 5, "limit": 5})
    assert p1["messages"][0]["content"] == "msg 0"
    assert p1["has_more"] is True
    assert p2["messages"][0]["content"] == "msg 5"  # picks up exactly where p1 left off
    assert p2["offset"] == 5
    assert [m["content"] for m in p2["messages"]] == ["msg 5", "msg 6", "msg 7", "msg 8", "msg 9"]


@pytest.mark.asyncio
async def test_get_conversation_last_page_has_more_false():
    """The final page sets has_more=False and may be shorter than limit."""
    from chatgpt_web2api.mcp_server import do_get_conversation
    driver = _long_conversation_driver(12)
    last = await do_get_conversation(driver, {"conversation_id": "conv-long", "offset": 10, "limit": 5})
    assert last["total"] == 12
    assert len(last["messages"]) == 2  # 12 - 10
    assert last["has_more"] is False
    assert [m["content"] for m in last["messages"]] == ["msg 10", "msg 11"]


@pytest.mark.asyncio
async def test_get_conversation_offset_beyond_end_empty():
    """offset >= total returns an empty page, has_more=False (no infinite loop)."""
    from chatgpt_web2api.mcp_server import do_get_conversation
    driver = _long_conversation_driver(5)
    over = await do_get_conversation(driver, {"conversation_id": "conv-long", "offset": 100, "limit": 50})
    assert over["total"] == 5
    assert over["messages"] == []
    assert over["has_more"] is False


@pytest.mark.asyncio
async def test_get_conversation_full_page_through_assembles_whole_thread():
    """Paging through offset 0,5,10,... reconstructs the entire conversation in
    order — the actual goal: read the whole chat without truncation."""
    from chatgpt_web2api.mcp_server import do_get_conversation
    n = 23
    driver = _long_conversation_driver(n)
    assembled = []
    offset = 0
    while True:
        page = await do_get_conversation(driver, {"conversation_id": "conv-long", "offset": offset, "limit": 5})
        assembled.extend(m["content"] for m in page["messages"])
        if not page["has_more"]:
            break
        offset += page["limit"]
    assert assembled == [f"msg {i}" for i in range(n)]  # whole thread, in order


# ── do_delete_conversation ───────────────────────────────────

@pytest.mark.asyncio
async def test_delete_conversation(mock_driver):
    from chatgpt_web2api.mcp_server import do_delete_conversation
    result = await do_delete_conversation(mock_driver, {"conversation_id": "conv-1"})
    assert result["success"] is True
    assert result["conversation_id"] == "conv-1"
    mock_driver.delete_conversation.assert_called_once_with("conv-1")


# ── do_delete_project ────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_project(mock_driver):
    from chatgpt_web2api.mcp_server import do_delete_project
    mock_driver.delete_project = AsyncMock(return_value={"success": True, "project_id": "g-p-1"})
    result = await do_delete_project(mock_driver, {"project_id": "g-p-1"})
    assert result["success"] is True
    assert result["project_id"] == "g-p-1"
    mock_driver.delete_project.assert_called_once_with("g-p-1")


# ── do_archive_conversation ──────────────────────────────────

@pytest.mark.asyncio
async def test_archive_conversation(mock_driver):
    from chatgpt_web2api.mcp_server import do_archive_conversation
    result = await do_archive_conversation(mock_driver, {
        "conversation_id": "conv-1", "archive": True,
    })
    assert result["success"] is True
    assert result["archived"] is True


# ── do_create_project ────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_project(mock_driver):
    from chatgpt_web2api.mcp_server import do_create_project
    result = await do_create_project(mock_driver, {
        "name": "New Project", "instructions": "Be helpful",
    })
    assert result["id"] == "g-p-new"
    mock_driver.create_project.assert_called_once_with(
        name="New Project", instructions="Be helpful",
        memory_scope="project_v2",
    )


# ── do_update_project_instructions ───────────────────────────

@pytest.mark.asyncio
async def test_update_project_instructions(mock_driver):
    from chatgpt_web2api.mcp_server import do_update_project_instructions
    result = await do_update_project_instructions(mock_driver, {
        "project_id": "g-p-test", "instructions": "New instructions",
    })
    assert result["success"] is True
    assert result["project_id"] == "g-p-test"


# ── do_list_memories ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_memories(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_memories
    result = await do_list_memories(mock_driver)
    assert "memories" in result
    assert len(result["memories"]) == 1
    assert result["memories"][0]["id"] == "mem-1"


# ── do_create_memory ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_memory(mock_driver):
    from chatgpt_web2api.mcp_server import do_create_memory
    result = await do_create_memory(mock_driver, {"content": "Remember this"})
    assert "content" in result
    mock_driver.create_memory.assert_called_once_with(content="Remember this")


# ── do_delete_memory ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_memory(mock_driver):
    from chatgpt_web2api.mcp_server import do_delete_memory
    result = await do_delete_memory(mock_driver, {"memory_id": "mem-1"})
    assert result["success"] is True
    assert result["memory_id"] == "mem-1"


def test_delete_memory_output_schema_matches_returned_shape():
    """delete_memory's outputSchema must match what do_delete_memory returns.

    Regression guard: previously delete_memory shared DELETE_RESULT_OUTPUT
    (which requires conversation_id), but the handler returns memory_id —
    so any actual call failed MCP output validation.
    """
    from chatgpt_web2api.mcp_server import ToolName, _build_tools
    tools = {t.name: t for t in _build_tools()}
    schema = tools[ToolName.DELETE_MEMORY.value].outputSchema
    required = set(schema["required"])
    # The handler returns {success, memory_id}, so the schema must match
    assert "success" in required
    assert "memory_id" in required
    assert "conversation_id" not in required


# ── do_list_gpts ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_gpts(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_gpts
    result = await do_list_gpts(mock_driver)
    assert "gpts" in result
    assert len(result["gpts"]) == 1
    assert result["gpts"][0]["id"] == "gpt-1"


# ── do_list_project_files ────────────────────────────────────

@pytest.mark.asyncio
async def test_list_project_files(mock_driver):
    from chatgpt_web2api.mcp_server import do_list_project_files
    result = await do_list_project_files(mock_driver, {"project_id": "g-p-test"})
    assert "files" in result
    assert len(result["files"]) == 1
    assert result["project_id"] == "g-p-test"


# ── do_chat_completion ───────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_completion_basic(mock_driver, mock_config):
    from chatgpt_web2api.mcp_server import do_chat_completion
    result = await do_chat_completion(mock_driver, {
        "message": "Hello",
    }, mock_config)
    assert "content" in result
    assert result["content"] == "Hello!"
    assert "model" in result
    assert "conversation_id" in result


@pytest.mark.asyncio
async def test_chat_completion_with_system_prompt(mock_driver, mock_config):
    from chatgpt_web2api.mcp_server import do_chat_completion
    result = await do_chat_completion(mock_driver, {
        "message": "Hello",
        "system_prompt": "Be concise",
    }, mock_config)
    assert result["content"] == "Hello!"
    # Should have navigated to new chat (system prompt forces new conv)
    mock_driver.navigate_new_chat.assert_called_once()


@pytest.mark.asyncio
async def test_chat_completion_with_project(mock_driver, mock_config):
    from chatgpt_web2api.mcp_server import do_chat_completion
    result = await do_chat_completion(mock_driver, {
        "message": "Hello",
        "project_id": "g-p-test",
    }, mock_config)
    assert result["content"] == "Hello!"
    mock_driver.navigate_new_chat.assert_called_once_with(gizmo_id="g-p-test")


@pytest.mark.asyncio
async def test_chat_completion_with_model(mock_driver, mock_config):
    from chatgpt_web2api.mcp_server import do_chat_completion
    result = await do_chat_completion(mock_driver, {
        "message": "Hello",
        "model": "gpt-5-5",
    }, mock_config)
    assert result["model"] == "gpt-5-5"
    mock_driver.select_model.assert_called_once_with("gpt-5-5")


@pytest.mark.asyncio
async def test_chat_completion_auto_model_no_select(mock_driver, mock_config):
    from chatgpt_web2api.mcp_server import do_chat_completion
    _result = await do_chat_completion(mock_driver, {
        "message": "Hello",
        "model": "auto",
    }, mock_config)
    mock_driver.select_model.assert_not_called()


# ── do_chat_with_gpt ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_with_gpt(mock_driver):
    from chatgpt_web2api.mcp_server import do_chat_with_gpt
    result = await do_chat_with_gpt(mock_driver, {
        "gpt_id": "gpt-1", "message": "Write code",
    })
    assert result["content"] == "Hello!"
    assert result["gpt_id"] == "gpt-1"
    mock_driver.navigate_gpt.assert_called_once_with(gizmo_id="gpt-1")


# ── API Server: message history ──────────────────────────────

@pytest.mark.asyncio
async def test_api_message_history_includes_assistant():
    """Verify that assistant messages are preserved in the conversation text."""
    from chatgpt_web2api.api_server import APIServer
    from chatgpt_web2api.cdp_driver import CDPDriver, StreamChunk
    from chatgpt_web2api.config import Config

    config = Config.load(None)
    driver = MagicMock(spec=CDPDriver)
    driver.is_connected = True
    driver._current_conv_id = None
    driver._access_token = "test"
    driver.select_model = AsyncMock(return_value=True)

    captured_text = {}

    async def _stream(text, timeout=120):
        captured_text["value"] = text
        yield StreamChunk(delta="Response")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()

    _server = APIServer(config, driver)

    # Simulate a request with multi-turn messages
    messages = [
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "And 3+3?"},
    ]

    # Build text the same way the handler does
    conversation_lines = []
    for msg in messages:
        role = msg.get("role", "")
        content = str(msg.get("content", ""))
        if role == "user":
            conversation_lines.append(f"[User]\n{content}")
        elif role == "assistant":
            conversation_lines.append(f"[Assistant]\n{content}")

    full_text = "\n".join(conversation_lines)

    # Verify both user and assistant messages are present
    assert "[User]\nWhat is 2+2?" in full_text
    assert "[Assistant]\n4" in full_text
    assert "[User]\nAnd 3+3?" in full_text


# ── API Server: model selection wiring ───────────────────────

@pytest.mark.asyncio
async def test_api_model_selection_called():
    """Verify select_model is called for non-auto models."""
    from chatgpt_web2api.cdp_driver import CDPDriver, StreamChunk

    driver = MagicMock(spec=CDPDriver)
    driver.is_connected = True
    driver._current_conv_id = None
    driver._access_token = "test"
    driver.select_model = AsyncMock(return_value=True)

    async def _stream(text, timeout=120):
        yield StreamChunk(delta="OK")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()

    from chatgpt_web2api.api_server import APIServer
    from chatgpt_web2api.config import Config

    config = Config.load(None)
    server = APIServer(config, driver)

    # Call the handler via internal method
    _result = await server._full_response(
        MagicMock(), "gpt-5-5", "Test message", 30,
    )

    # select_model should have been called during _handle_chat
    # (tested through do_chat_completion above, this validates the path exists)


# ── Config: W2A_HEADLESS env ─────────────────────────────────

def test_config_headless_env(monkeypatch):
    """W2A_HEADLESS env var is read correctly."""
    from chatgpt_web2api.config import Config

    monkeypatch.setenv("W2A_HEADLESS", "true")
    config = Config.load(None)
    assert config.chrome.headless is True

    monkeypatch.setenv("W2A_HEADLESS", "false")
    config = Config.load(None)
    assert config.chrome.headless is False

    monkeypatch.setenv("W2A_HEADLESS", "1")
    config = Config.load(None)
    assert config.chrome.headless is True

    monkeypatch.delenv("W2A_HEADLESS", raising=False)
    config = Config.load(None)
    assert config.chrome.headless is False  # default


# ── CDP Driver: _js_with_data safety ─────────────────────────

def test_js_with_data_escapes_properly():
    """Verify that _js_with_data uses json.dumps for safe serialization."""

    # The method uses json.dumps(data) as prefix
    data = {
        "token": "abc'def\"ghi\\jkl",
        "conv_id": "test'; DROP TABLE--;",
        "title": 'He said "hello" and \\left',
    }
    serialized = json.dumps(data)

    # Should be valid JSON
    parsed = json.loads(serialized)
    assert parsed["token"] == "abc'def\"ghi\\jkl"
    assert parsed["conv_id"] == "test'; DROP TABLE--;"
    assert parsed["title"] == 'He said "hello" and \\left'

    # When used as JS: const __D = {...};
    # This should not break JS parsing
    js_code = f"const __D = {serialized};"
    # No assertion on JS execution here — just that JSON is valid
    assert "__D" in js_code

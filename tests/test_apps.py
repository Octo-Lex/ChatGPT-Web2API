"""App contract and send regressions; no live ChatGPT account required."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from chatgpt_web2api.api_server import APIServer
from chatgpt_web2api.cdp_driver import CDPDriver, RateLimitError, SendReadinessError, StreamChunk
from chatgpt_web2api.config import Config
from chatgpt_web2api.identity_listener import IdentityListener, hash_sent_text
from chatgpt_web2api.mcp_server import ChatCompletionInput, do_chat_completion
from chatgpt_web2api.resilience import retry_on_rate_limit


@pytest.fixture(autouse=True)
def isolated_lock_files(monkeypatch, tmp_path):
    monkeypatch.setattr("chatgpt_web2api.cross_process_lock.Path.home", lambda: tmp_path)


def send_driver():
    driver = CDPDriver()
    driver._read_assistant_count_baseline = AsyncMock(return_value=0)
    driver._verify_send_acknowledged = AsyncMock(return_value=True)
    driver._js_strict = AsyncMock(return_value="https://chatgpt.com/c/test")
    driver._fetch_text_for_turn = AsyncMock(
        return_value=SimpleNamespace(status="matched", text="OK", diagnostic={})
    )
    driver.type_message = AsyncMock()
    driver.click_send = AsyncMock()
    driver._capture_selector_diagnostic = AsyncMock()
    driver._completion.last_dom_text = "OK"

    async def complete(**kwargs):
        yield StreamChunk(delta="OK")

    driver._completion.stream_until_complete = complete
    return driver


@pytest.mark.parametrize("kwargs", [{}, {"apps": None}, {"apps": []}])
async def test_no_apps_preserves_original_send_path(kwargs):
    driver = send_driver()
    driver.type_message_with_apps = AsyncMock()
    chunks = [chunk async for chunk in driver.send_and_stream("prompt", **kwargs)]
    driver.type_message.assert_awaited_once_with("prompt")
    driver.type_message_with_apps.assert_not_awaited()
    driver.click_send.assert_awaited_once()
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.parametrize("apps", [["GitHub"], ["GitHub", "Hermes Memory MCP NoAuth"]])
async def test_apps_selected_in_order_before_prompt_and_send(apps):
    driver = send_driver()
    events = []

    async def clear(text):
        events.append(("clear", text))

    async def cdp(method, params):
        events.append((method, params["text"]))

    results = iter([value for _ in apps for value in [True, "waiting", "clicked", "selected"]]
                   + [True, "https://chatgpt.com/c/test"])

    async def js(expr):
        result = next(results)
        if result == "selected":
            events.append(("chip", "confirmed"))
        return result

    async def click():
        events.append(("send", ""))

    driver.type_message = clear
    driver._cdp = cdp
    driver._js_strict = js
    driver.click_send = click
    _ = [chunk async for chunk in driver.send_and_stream("prompt", apps=apps)]
    expected = [("clear", "")]
    for app in apps:
        expected.extend([("Input.insertText", "@" + app[:3]), ("chip", "confirmed")])
    assert events == expected + [("Input.insertText", "prompt"), ("send", "")]


@pytest.mark.parametrize("clicked", [False, True], ids=["missing-suggestion", "click-without-chip"])
async def test_app_failure_prevents_prompt_and_send_and_closes_capture(monkeypatch, clicked):
    import chatgpt_web2api.chatgpt_dom as dom

    monkeypatch.setattr(dom, "APP_MENTION_MAX_WAIT_S", 0.05)
    driver = send_driver()
    driver._cdp = AsyncMock()
    driver._identity_listener = IdentityListener(driver)
    driver._identity_listener._ready = True
    values = iter([True, "clicked"] if clicked else [True])

    async def js(expr):
        return next(values, "waiting")

    driver._js_strict = js
    with pytest.raises(SendReadinessError, match="Missing App"):
        _ = [chunk async for chunk in driver.send_and_stream("prompt", apps=["Missing App"])]
    driver.click_send.assert_not_awaited()
    driver._cdp.assert_awaited_once_with("Input.insertText", {"text": "@Mis"})
    driver._capture_selector_diagnostic.assert_awaited_once()
    assert driver._identity_listener._active_scope is None


async def test_rate_limit_retry_rebuilds_apps_and_prompt(monkeypatch):
    import chatgpt_web2api.resilience as resilience

    monkeypatch.setattr(resilience.asyncio, "sleep", AsyncMock())
    driver = send_driver()
    events = []

    async def clear(text):
        events.append("clear")

    async def cdp(method, params):
        events.append(params["text"])

    driver.type_message = clear
    driver._cdp = cdp
    driver._js_strict = AsyncMock(side_effect=[True, "clicked", "selected", True] * 2
                                  + ["https://chatgpt.com/c/test"])
    driver.click_send = AsyncMock(side_effect=[RateLimitError(retry_after=1), None])
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    async def send():
        return [chunk async for chunk in driver.send_and_stream("prompt", apps=["GitHub"])]

    await retry_on_rate_limit(driver, send, max_attempts=2)
    assert events == ["clear", "@Git", "prompt"] * 2
    assert driver.click_send.await_count == 2


def transport_driver():
    driver = MagicMock(spec=CDPDriver)
    driver._current_conv_id = None
    driver.navigate_new_chat = AsyncMock()
    driver._js_strict = AsyncMock(return_value='{"text":""}')
    driver.calls = []

    async def stream(text, **kwargs):
        driver.calls.append((text, kwargs))
        yield StreamChunk(delta="OK", finish_reason="stop")

    driver.send_and_stream = stream
    return driver


APP_INPUTS = [{}, {"apps": None}, {"apps": []}, {"apps": ["GitHub", "Hermes Memory MCP NoAuth"]}]
INVALID_APPS = ["GitHub", {}, [""], [" \n"], [None], [1], [True], ["GitHub", {}]]


@pytest.mark.parametrize("fields", APP_INPUTS)
@pytest.mark.parametrize("stream", [False, True])
async def test_rest_apps_reach_driver(fields, stream):
    driver = transport_driver()
    server = APIServer(Config(), driver)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "prompt"}], "stream": stream, **fields,
        })
        assert response.status == 200
        assert "OK" in await response.text()
    assert len(driver.calls) == 1
    text, kwargs = driver.calls[0]
    assert text == "[User]\nprompt"
    if fields.get("apps"):
        assert kwargs["apps"] == fields["apps"]
    else:
        assert "apps" not in kwargs  # Preserve existing driver/test seams.


@pytest.mark.parametrize("apps", INVALID_APPS)
async def test_rest_rejects_invalid_apps_before_browser_mutation(apps):
    driver = transport_driver()
    server = APIServer(Config(), driver)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "prompt"}], "apps": apps,
        })
        assert response.status == 400
        assert (await response.json())["error"]["type"] == "invalid_request_error"
    driver.navigate_new_chat.assert_not_awaited()
    assert driver.calls == []


@pytest.mark.parametrize("fields", APP_INPUTS)
async def test_mcp_apps_schema_and_forwarding(fields):
    schema = ChatCompletionInput.model_json_schema()
    assert "apps" in schema["properties"]
    assert "apps" not in schema["required"]
    driver = transport_driver()
    result = await do_chat_completion(driver, {"message": "prompt", **fields}, Config())
    assert result["content"] == "OK"
    assert len(driver.calls) == 1
    text, kwargs = driver.calls[0]
    assert text == "prompt"
    if fields.get("apps"):
        assert kwargs["apps"] == fields["apps"]
    else:
        assert "apps" not in kwargs


@pytest.mark.parametrize("apps", INVALID_APPS)
def test_mcp_rejects_invalid_apps(apps):
    with pytest.raises(ValidationError, match="apps"):
        ChatCompletionInput(message="prompt", apps=apps)


async def test_app_metadata_does_not_change_identity_text_hash():
    """Synthetic payload contract, not evidence of ChatGPT's live serializer."""
    listener = IdentityListener(MagicMock())
    prompt = "original prompt\nwith a second line"
    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text(prompt), conversation_id=None, target_id="test",
    )
    message_id = "11111111-1111-4111-8111-111111111111"
    body = {"action": "next", "messages": [{
        "id": message_id,
        "author": {"role": "user"},
        "content": {"parts": [prompt, {"type": "app", "name": "GitHub"}]},
        "metadata": {"apps": ["GitHub"]},
    }]}
    url = "https://chatgpt.com/backend-api/f/conversation"
    try:
        await listener._process_send_post(scope, {"params": {
            "requestId": "request-1", "request": {"url": url, "postData": json.dumps(body)},
        }}, url)
        assert await listener.wait_for_captured_uuid(timeout=0.1) == message_id
    finally:
        scope.close()

"""App contract and send regressions; no live ChatGPT account required."""

import json
import shutil
import subprocess
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


APP_INPUTS = [
    {}, {"apps": None}, {"apps": []}, {"apps": ["GitHub", "Hermes Memory MCP NoAuth"]},
    {"apps": ["  GitHub  ", "Hermes Memory MCP NoAuth\t", "GitHub"]},
]
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
        assert kwargs["apps"] == [name.strip() for name in fields["apps"]]
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
        assert kwargs["apps"] == [name.strip() for name in fields["apps"]]
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


@pytest.mark.parametrize("apps,wire_text", [
    (None, "prompt"),
    (["Hermes Memory MCP NoAuth"], "@Hermes Memory MCP NoAuth prompt"),
    (["GitHub", "Hermes Memory MCP NoAuth"], "@GitHub @Hermes Memory MCP NoAuth prompt"),
])
async def test_app_wire_text_used_for_uuid_capture_and_fallback_anchor(apps, wire_text):
    """Live POST (2026-09-23): app names prefix the string part, not just metadata."""
    driver = send_driver()
    driver.type_message_with_apps = AsyncMock()
    listener = driver._identity_listener = IdentityListener(driver)
    listener._ready = True
    message_id = "11111111-1111-4111-8111-111111111111"

    async def click():
        body = {"action": "next", "messages": [{
            "id": message_id, "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [wire_text]},
        }]}
        await listener._process_send_post(listener._active_scope, {"params": {
            "request": {"postData": json.dumps(body)},
        }}, "https://chatgpt.com/backend-api/f/conversation")

    async def complete(**kwargs):
        anchor = kwargs["turn_anchor"]
        assert anchor.sent_text == wire_text
        assert anchor.captured_user_message_id == message_id
        yield StreamChunk(delta="OK")

    driver.click_send = click
    driver._completion.stream_until_complete = complete
    _ = [chunk async for chunk in driver.send_and_stream("prompt", apps=apps)]
    assert listener.capture_success_count == 1
    assert listener._active_scope is None
    driver._verify_send_acknowledged.assert_not_awaited()


async def test_app_without_captured_uuid_reconciles_wire_text():
    from chatgpt_web2api.turn_anchor import select_text_for_turn

    driver = send_driver()
    driver._current_conv_id = "test"
    driver.type_message_with_apps = AsyncMock()
    listener = driver._identity_listener = IdentityListener(driver)
    listener._ready = True
    listener.wait_for_captured_uuid = AsyncMock(return_value=None)
    prompt = "[User]\nRufe get_poc_memory auf."
    wire_text = "@Hermes Memory MCP NoAuth " + prompt
    marker = "POC0_MEMORY_MARKER_7F2A"

    def turn(suffix, timestamp, answer):
        user, assistant = "u-" + suffix, "a-" + suffix
        return {
            user: {"id": user, "children": [assistant], "role": "user",
                   "create_time": timestamp, "content_type": "text", "text": wire_text},
            assistant: {"id": assistant, "parent": user, "children": [], "role": "assistant",
                        "end_turn": True, "create_time": timestamp + 1,
                        "content_type": "text", "text": answer},
        }

    old_nodes = turn("old", 100, "STALE")
    projection = {"nodes": {**old_nodes, **turn("new", 200, marker)}}
    driver._backend_client._fetch_recent_conversation_projection = AsyncMock(
        return_value={"nodes": old_nodes}
    )

    async def fetch(conv_id, anchor):
        assert conv_id == "test"
        assert anchor.captured_user_message_id is None
        assert anchor.mode == "existing_conversation"
        assert anchor.sent_text == wire_text
        result = select_text_for_turn(projection, anchor)
        assert result.status == "matched", result.diagnostic
        assert result.text == marker
        return result

    async def complete(**kwargs):
        driver._completion.last_dom_text = ""
        if False:
            yield

    driver._fetch_text_for_turn = fetch
    driver._completion.stream_until_complete = complete
    chunks = [c async for c in driver.send_and_stream(prompt, apps=["Hermes Memory MCP NoAuth"])]
    assert "".join(c.delta for c in chunks) == marker
    driver.type_message_with_apps.assert_awaited_once_with(prompt, ["Hermes Memory MCP NoAuth"])
    driver.click_send.assert_awaited_once()
    listener.wait_for_captured_uuid.assert_awaited_once()
    assert listener._active_scope is None


@pytest.mark.parametrize("popup_attributes,suggestion_html,app_name,click_selector", [
    ('role="listbox"', 'Hermes Memory MCP NoAuth', 'Hermes Memory MCP NoAuth', '#suggestion'),
    ('class="popover"', '<div class="__menu-item"><span>Hermes Memory MCP NoAuth</span>'
     '<span>Hermes Memory MCP NoAuth</span></div>', 'Hermes Memory MCP NoAuth', '.__menu-item'),
    ('class="suggestionMenu-XYZ composer-home-top-menu"',
     '<button><span>GitHub</span><span>Triage PRs, issues, CI, and publish flows</span></button>',
     'GitHub', 'button'),
])
async def test_plain_div_app_suggestion_in_real_dom(
    tmp_path, popup_attributes, suggestion_html, app_name, click_selector,
):
    """Run the generated selector in local Chrome; no ChatGPT session or network."""
    chrome = shutil.which(Config().chrome.chrome_path)
    if not chrome:
        pytest.skip("Chrome is required for the local DOM regression")
    driver = send_driver()
    driver._cdp = AsyncMock()
    scripts = []
    responses = iter([True, "clicked", "selected", True])

    async def capture_script(expr):
        scripts.append(expr)
        return next(responses)

    driver._js_strict = capture_script
    await driver.type_message_with_apps("prompt", [app_name])
    assert driver._cdp.await_args_list[0].args == ("Input.insertText", {"text": "@" + app_name[:3]})
    fixture = tmp_path / "app-suggestion.html"
    fixture.write_text("""<!doctype html><html><body>
        <div id="prompt-textarea" role="textbox" contenteditable="true"
             style="position:fixed;left:300px;top:400px;width:450px;height:90px"></div>
        <div """ + popup_attributes + """ style="position:fixed;left:300px;top:280px;width:400px;height:110px">
            <div id="suggestion">""" + suggestion_html + """</div>
        </div>
        <aside><div role="listbox"><div>""" + app_name + """</div></div></aside>
        <script>
        const scripts = """ + json.dumps(scripts) + """;
        const name = """ + json.dumps(app_name) + """;
        const clickTarget = document.querySelector(""" + json.dumps(click_selector) + """);
        const suggestion = document.getElementById('suggestion');
        let clicks = 0;
        suggestion.onclick = event => {
            if (event.target !== clickTarget) return;
            clicks++;
            const chip = document.createElement('span');
            chip.contentEditable = 'false';
            chip.textContent = name;
            document.getElementById('prompt-textarea').append(chip);
        };
        const result = [eval(scripts[0]), eval(scripts[1]), eval(scripts[2]), clicks];
        suggestion.style.display = 'none';
        result.push(eval(scripts[1]));
        const output = document.createElement('pre');
        output.id = 'result'; output.textContent = JSON.stringify(result);
        document.body.append(output);
        </script></body></html>""", encoding="utf-8")
    result = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--disable-background-networking",
         "--no-first-run", "--no-default-browser-check", "--window-size=1280,900",
         f"--user-data-dir={tmp_path / 'chrome-profile'}", "--dump-dom", fixture.as_uri()],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert '<pre id="result">[true,"clicked","selected",1,"waiting"]</pre>' in result.stdout, (
        result.stdout + result.stderr
    )

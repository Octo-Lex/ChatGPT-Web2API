"""Fail-closed fresh-chat navigation and mutable Send/Stop regression tests.

These are deterministic CDP mocks; no live ChatGPT behavior is certified.
The separate non-replay mutation contract belongs to issue #51 / PR #52.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, SendReadinessError
from chatgpt_web2api.mcp_server import do_chat_completion


def _driver():
    d = CDPDriver(cdp_port=9222)
    d._cdp = AsyncMock(return_value={})
    d._breakers = None
    return d


def _probe(
    url="https://chatgpt.com/?model=auto",
    *,
    composer=True,
    stop=False,
    ready_state="complete",
):
    return json.dumps({
        "url": url,
        "ready_state": ready_state,
        "composer": composer,
        "stop_present": stop,
    })


@pytest.fixture(autouse=True)
def _fast_navigation(monkeypatch):
    """No live sleep for the fixed-budget 30-poll navigation loop."""
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())


@pytest.mark.parametrize("url,expected", [
    ("https://chatgpt.com/", True),
    ("https://chatgpt.com/?model=auto", True),
    ("https://chatgpt.com/c/foreign-turn", False),
    ("https://chatgpt.com/g/different/project", False),
    ("https://chatgpt.com.evil.example/?model=auto", False),
    ("https://fake.example/?next=https://chatgpt.com/", False),
    ("http://chatgpt.com/", False),
    ("https://chatgpt.com:8080/", False),
    ("https://chatgpt.com@evil.example/", False),
    ("not a url chatgpt.com", False),
])
def test_fresh_route_exact_origin_and_path(url, expected):
    assert CDPDriver._is_url_at_new_chat(url) is expected


@pytest.mark.parametrize("url,expected", [
    ("https://chatgpt.com/g/g-123/project", True),
    ("https://chatgpt.com/g/g-123/project/", True),
    ("https://chatgpt.com/g/g-other/project", False),
    ("https://chatgpt.com/g/g-123/c/other", False),
    ("https://chatgpt.com/?model=auto", False),
])
def test_project_fresh_route_exact_identity(url, expected):
    assert CDPDriver._is_url_at_new_chat(url, "g-123") is expected


@pytest.mark.asyncio
async def test_fresh_navigation_requires_two_readiness_samples():
    d = _driver()
    d._current_conv_id = "old-123"
    d._js = AsyncMock(return_value=_probe())

    await d.navigate_new_chat()

    d._cdp.assert_awaited_once_with(
        "Page.navigate", {"url": "https://chatgpt.com/?model=auto"}
    )
    assert d._current_conv_id is None
    assert d._js.await_count == 2
    js = d._js.call_args.args[0]
    assert "getClientRects" in js
    assert "stop_present" in js
    assert "COMPOSER_SELECTOR" not in js  # rendered selector, not literal Python name


@pytest.mark.asyncio
async def test_project_navigation_requires_matching_project():
    d = _driver()
    d._js = AsyncMock(return_value=_probe("https://chatgpt.com/g/g-123/project"))

    await d.navigate_new_chat(gizmo_id="g-123")

    d._cdp.assert_awaited_once_with(
        "Page.navigate", {"url": "https://chatgpt.com/g/g-123/project"}
    )


@pytest.mark.asyncio
async def test_missing_composer_fails_closed_and_clears_stale_identity():
    d = _driver()
    d._current_conv_id = "old-123"
    d._js = AsyncMock(return_value=_probe(composer=False))

    with pytest.raises(SendReadinessError, match="usable composer absent"):
        await d.navigate_new_chat()

    assert d._current_conv_id is None
    assert d._js.await_count == 30


@pytest.mark.asyncio
async def test_wrong_chat_route_fails_even_with_usable_editor():
    d = _driver()
    d._current_conv_id = "old-123"
    d._js = AsyncMock(return_value=_probe("https://chatgpt.com/c/foreign"))

    with pytest.raises(SendReadinessError, match="wrong fresh-chat route"):
        await d.navigate_new_chat()

    assert d._current_conv_id is None


@pytest.mark.asyncio
async def test_wrong_project_route_fails_even_with_usable_editor():
    d = _driver()
    d._js = AsyncMock(return_value=_probe("https://chatgpt.com/g/foreign/project"))

    with pytest.raises(SendReadinessError, match="wrong fresh-chat route"):
        await d.navigate_new_chat(gizmo_id="g-123")


@pytest.mark.asyncio
async def test_stop_visible_fails_closed_before_any_submission():
    d = _driver()
    d._js = AsyncMock(return_value=_probe(stop=True))

    with pytest.raises(SendReadinessError, match="generation active"):
        await d.navigate_new_chat()

    assert d._current_conv_id is None


@pytest.mark.asyncio
async def test_loading_document_cannot_be_counted_as_ready():
    d = _driver()
    d._js = AsyncMock(return_value=_probe(ready_state="loading"))

    with pytest.raises(SendReadinessError, match="document not ready"):
        await d.navigate_new_chat()


@pytest.mark.asyncio
async def test_displacement_between_two_samples_cannot_succeed():
    d = _driver()
    d._js = AsyncMock(side_effect=[
        _probe(),  # first stage appears ready
        *([_probe("https://chatgpt.com/c/foreign")] * 30),
    ])

    with pytest.raises(SendReadinessError, match="wrong fresh-chat route"):
        await d.navigate_new_chat()

    assert d._current_conv_id is None
    assert d._js.await_count == 31


@pytest.mark.asyncio
async def test_unreadable_probes_fail_closed():
    d = _driver()
    d._js = AsyncMock(return_value="")

    with pytest.raises(SendReadinessError, match="no readable readiness probes"):
        await d.navigate_new_chat()


@pytest.mark.asyncio
async def test_page_navigate_error_fails_before_probe():
    d = _driver()
    d._current_conv_id = "old-123"
    d._cdp = AsyncMock(return_value={"errorText": "net::ERR_ABORTED"})
    d._js = AsyncMock()

    with pytest.raises(SendReadinessError, match="navigation rejected"):
        await d.navigate_new_chat()

    assert d._current_conv_id is None
    d._js.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_new_chat_failure_does_not_enter_send_and_stream():
    """Verify the actual MCP application seam stops before typing/sending."""
    d = MagicMock()
    d._current_conv_id = None
    d.navigate_new_chat = AsyncMock(
        side_effect=SendReadinessError("fresh chat navigation failed")
    )

    with pytest.raises(SendReadinessError, match="navigation failed"):
        await do_chat_completion(d, {"message": "never send this"}, None)

    d.navigate_new_chat.assert_awaited_once()
    d.send_and_stream.assert_not_called()


@pytest.mark.asyncio
async def test_stop_state_change_at_click_is_rejected():
    """A Stop appearing after the ready poll must fail at mutation time."""
    d = _driver()
    captured = []

    async def evaluate(expr, timeout=15):
        captured.append(expr)
        return "stop button present" if "btn.dispatchEvent" in expr else "yes"

    d._js = evaluate
    d._capture_selector_diagnostic = AsyncMock()

    with pytest.raises(SendReadinessError, match="stop button present"):
        await d.click_send()

    assert len(captured) == 2
    click_js = captured[-1]
    assert "form.querySelector" in click_js
    assert "stopLike" in click_js
    assert 'button[type="submit"]' not in click_js
    assert click_js.index("stop button present") < click_js.index("btn.dispatchEvent")
    d._capture_selector_diagnostic.assert_awaited_once()

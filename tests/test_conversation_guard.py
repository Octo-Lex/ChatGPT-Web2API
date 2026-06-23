"""Tests for the auto-continue conversation guard (PR #9 Finding 1).

The bug: REST/MCP auto-continue trusted the in-memory ``_current_conv_id``
without reconciling against the live browser tab. If another process sharing
the Chrome tab navigated it, a follow-up message could be typed into the wrong
conversation. The fix adds ``ensure_current_conversation`` (exact path-segment
URL match, fail-closed) and tightens ``navigate_conversation`` so it only sets
``_current_conv_id`` after a verified landing.

These tests pin:
  - the static URL matcher (exact path match, query tolerated, no false positives)
  - ensure_current_conversation (no-op when live, navigates when stale, raises
    fail-closed when navigation can't verify)
  - navigate_conversation's verified-landing invariant (no admission of an
    unverified conversation; clears stale id on failure)
  - the REST + MCP auto-continue call sites actually invoke the guard
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from chatgpt_web2api.cdp_driver import CDPDriver


# ── 1. Static URL matcher ─────────────────────────────────────────────

def test_url_match_exact_conversation_path():
    d = CDPDriver(cdp_port=9222)
    cid = "6a3a80c8-64bc-83eb-8967-66452f3d93b1"
    assert d._is_url_at_conversation(
        f"https://chatgpt.com/c/{cid}", cid
    ) is True


def test_url_match_tolerates_query_string():
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    assert d._is_url_at_conversation(
        f"https://chatgpt.com/c/{cid}?model=auto&foo=bar", cid
    ) is True


def test_url_match_tolerates_trailing_slash():
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    assert d._is_url_at_conversation(
        f"https://chatgpt.com/c/{cid}/", cid
    ) is True


def test_url_match_rejects_different_conversation():
    """A different conversation id must NOT match — the original bug was
    substring matching that could admit the wrong conversation."""
    d = CDPDriver(cdp_port=9222)
    assert d._is_url_at_conversation(
        "https://chatgpt.com/c/different-id", "abc-123"
    ) is False


def test_url_match_rejects_subpath_of_other_conversation():
    """Trailing path segments under a different conversation must not match."""
    d = CDPDriver(cdp_port=9222)
    assert d._is_url_at_conversation(
        "https://chatgpt.com/c/other-id/something", "abc-123"
    ) is False


def test_url_match_rejects_non_conversation_url():
    d = CDPDriver(cdp_port=9222)
    assert d._is_url_at_conversation(
        "https://chatgpt.com/", "abc-123"
    ) is False
    assert d._is_url_at_conversation(
        "https://chatgpt.com/g/some-gpt", "abc-123"
    ) is False


def test_url_match_rejects_wrong_host():
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    assert d._is_url_at_conversation(
        f"https://evil.com/c/{cid}", cid
    ) is False


def test_url_match_rejects_empty_inputs():
    d = CDPDriver(cdp_port=9222)
    assert d._is_url_at_conversation("", "abc-123") is False
    assert d._is_url_at_conversation("https://chatgpt.com/c/x", "") is False
    assert d._is_url_at_conversation("", "") is False


def test_url_match_rejects_malformed_url():
    """urllib.parse handles malformed input; the helper returns False, not raises."""
    d = CDPDriver(cdp_port=9222)
    # A value that urlparse can handle but isn't a chatgpt conversation
    assert d._is_url_at_conversation("not a url at all", "abc-123") is False


# ── 2. _is_live_conversation_url ──────────────────────────────────────

@pytest.mark.asyncio
async def test_live_url_true_when_href_matches():
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    d._js_strict = AsyncMock(return_value=f"https://chatgpt.com/c/{cid}")
    assert await d._is_live_conversation_url(cid) is True


@pytest.mark.asyncio
async def test_live_url_false_on_cdp_read_failure():
    """An unreadable location.href must return False (fail-closed at the
    ensure_current_conversation layer, not here)."""
    from chatgpt_web2api.cdp_driver import CDPJSError
    d = CDPDriver(cdp_port=9222)
    d._js_strict = AsyncMock(side_effect=CDPJSError("context destroyed"))
    assert await d._is_live_conversation_url("abc-123") is False


# ── 3. ensure_current_conversation ────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_current_no_op_when_live_url_matches():
    """If the tab is already at the conversation, no navigation happens."""
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    d._js_strict = AsyncMock(return_value=f"https://chatgpt.com/c/{cid}")
    d.navigate_conversation = AsyncMock()
    await d.ensure_current_conversation(cid)
    d.navigate_conversation.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_current_navigates_when_stale_then_succeeds():
    """Live URL mismatch → navigate → post-navigation check passes → ok.

    The live URL reads 'stale' first (triggering navigation) then 'correct'
    after navigation (the post-navigation belt-and-braces check).
    """
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    # First read: wrong. Second read (after navigate): correct.
    d._js_strict = AsyncMock(
        side_effect=[
            "https://chatgpt.com/c/some-other-conv",
            f"https://chatgpt.com/c/{cid}",
        ]
    )
    d.navigate_conversation = AsyncMock()
    await d.ensure_current_conversation(cid)
    d.navigate_conversation.assert_awaited_once_with(cid)


@pytest.mark.asyncio
async def test_ensure_current_raises_when_post_navigation_still_wrong():
    """Fail-closed: if navigation still doesn't land on the right URL, raise
    and clear _current_conv_id — never proceed into an unknown tab state."""
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    d._current_conv_id = cid  # simulate stale local state
    # Both reads return the wrong URL.
    d._js_strict = AsyncMock(
        return_value="https://chatgpt.com/c/wrong-conv"
    )
    d.navigate_conversation = AsyncMock()  # navigate succeeds (no raise)...
    # ...but the post-nav live check still says wrong, so ensure_current must
    # catch the discrepancy and raise.

    with pytest.raises(RuntimeError, match="Failed to restore conversation"):
        await d.ensure_current_conversation(cid)
    # Stale local id cleared so a later auto-continue can't reuse it.
    assert d._current_conv_id is None


@pytest.mark.asyncio
async def test_ensure_current_raises_when_navigation_raises():
    """If navigate_conversation itself raises, the error propagates."""
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    d._js_strict = AsyncMock(
        return_value="https://chatgpt.com/c/wrong-conv"
    )
    d.navigate_conversation = AsyncMock(
        side_effect=RuntimeError("did not reach a ready composer")
    )
    with pytest.raises(RuntimeError, match="did not reach a ready composer"):
        await d.ensure_current_conversation(cid)


# ── 4. navigate_conversation verified-landing invariant ───────────────

@pytest.mark.asyncio
async def test_navigate_conversation_sets_id_only_on_verified_landing():
    """Happy path: composer ready AND url matches → _current_conv_id set."""
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    d._cdp = AsyncMock()  # Page.navigate
    # _js returns a ready state at the right URL on first poll.
    d._js = AsyncMock(return_value=json.dumps({
        "ready": True,
        "url": f"https://chatgpt.com/c/{cid}",
    }))
    await d.navigate_conversation(cid)
    assert d._current_conv_id == cid


@pytest.mark.asyncio
async def test_navigate_conversation_raises_and_clears_when_never_ready(monkeypatch):
    """If the composer never becomes ready at the right URL within the poll
    loop, _current_conv_id must NOT be admitted — and any stale id matching
    the request must be cleared. The old code fell through and set it."""
    d = CDPDriver(cdp_port=9222)
    cid = "abc-123"
    d._current_conv_id = cid  # pre-existing (possibly stale) state
    d._cdp = AsyncMock()
    # Composer never ready / never at the right URL.
    d._js = AsyncMock(return_value=json.dumps({"ready": False, "url": ""}))
    # Collapse the sleeps so the 30-iteration loop runs fast.
    async def _fast(_s):
        return None
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", _fast)

    with pytest.raises(RuntimeError, match="did not reach a ready composer"):
        await d.navigate_conversation(cid)
    assert d._current_conv_id is None  # stale id cleared, not admitted


# ── 5. REST auto-continue calls the guard ─────────────────────────────

@pytest.mark.asyncio
async def test_rest_auto_continue_invokes_ensure_current(monkeypatch):
    """The REST continue branch must call ensure_current_conversation instead
    of sleeping and trusting the local _current_conv_id.

    Drives the real _handle_chat with a fake request whose body triggers the
    continue branch (matching _last_conv_id/_current_conv_id, no system prompt).
    _full_response is stubbed to raise so we can prove the guard ran before
    the response path without coupling to the streaming internals.
    """
    import chatgpt_web2api.api_server as srv

    server = srv.APIServer.__new__(srv.APIServer)  # bypass __init__
    server._last_conv_id = "conv-rest-1"
    server._last_project_id = None
    server._request_count = 0
    server._cdp_port = 9222
    server._config = srv.Config.load(None)
    driver = MagicMock()
    driver._current_conv_id = "conv-rest-1"
    driver._current_model = None
    driver.select_model = AsyncMock(return_value=True)
    driver.ensure_current_conversation = AsyncMock()
    server._driver = driver

    # Sentinel: _full_response records that we reached past the guard and
    # returns a dummy response. The handler catches exceptions, so we can't
    # rely on propagation; instead assert the guard ran AND we got this far.
    reached = {"past_guard": False}
    async def _stub_response(*a, **kw):
        reached["past_guard"] = True
        return MagicMock()
    server._full_response = _stub_response
    server._stream_response = _stub_response

    # Fake request: a user message, no conversation_id (→ continue branch),
    # matching the server's _last_conv_id.
    request = MagicMock()
    request.headers = {}
    request.json = AsyncMock(return_value={
        "messages": [{"role": "user", "content": "hello"}],
        "model": "auto",
    })

    # Bypass the cross-process file lock so the test runs without it.
    from chatgpt_web2api.cross_process_lock import CrossProcessLock
    class _NullLock:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(srv, "CrossProcessLock", _NullLock)

    await server._handle_chat(request)

    # The guard ran (proving the continue branch was taken), and execution
    # reached _full_response (proving we proceeded past the guard correctly).
    driver.ensure_current_conversation.assert_awaited_once_with("conv-rest-1")
    assert reached["past_guard"] is True


# ── 6. MCP auto-continue calls the guard ──────────────────────────────

@pytest.mark.asyncio
async def test_mcp_auto_continue_invokes_ensure_current():
    """The MCP continue branch must call ensure_current_conversation instead
    of only logging and trusting _current_conv_id.

    Drives do_chat_completion directly with a driver whose _current_conv_id
    is set and no system_prompt/project_id → continue branch. send_and_stream
    raises to prove we reached past the guard.
    """
    from chatgpt_web2api import mcp_server as mod
    from chatgpt_web2api.config import Config

    driver = MagicMock()
    driver._current_conv_id = "conv-mcp-1"
    driver.ensure_current_conversation = AsyncMock()
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()

    async def _boom(text, timeout=120):
        raise AssertionError("reached past the guard")
        yield  # pragma: no cover (generator signature)
    driver.send_and_stream = _boom

    cfg = Config.load(None)
    with pytest.raises(AssertionError, match="reached past the guard"):
        await mod.do_chat_completion(
            driver, {"message": "hello"}, cfg,
            on_progress=None,
        )

    driver.ensure_current_conversation.assert_awaited_once_with("conv-mcp-1")

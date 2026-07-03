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
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    server._parallel_tabs = False  # PR4: mirror __init__'s cache for __new__ bypass
    server._config = srv.Config.load(None)
    server._breakers = srv.BreakerRegistry()  # Phase 4 PR2: preflight reads this
    server._last_error = None
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
    class _NullLock:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(srv, "MutationLock", _NullLock)

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


# ── 7. Connect-time send-readiness invariant ──────────────────────────
#
# Live-found bug (not caught by 292 mocked tests): connect() may attach to a
# chatgpt.com/ home/landing tab. The home page is auth-valid but lacks the
# composer (its textarea is unnamed and matches neither selector), so the next
# type_message raises "No composer found" and surfaces as an opaque
# "no close frame received or sent" 500 through REST. _ensure_send_ready
# normalizes the tab into a chat page before connect() returns: a connected
# driver is a send-capable driver.

@pytest.mark.asyncio
async def test_ensure_send_ready_noop_when_composer_present():
    """If the tab already has a composer, _ensure_send_ready is a no-op:
    no navigation. The common case (driver attaches to a real chat tab)."""
    d = CDPDriver(cdp_port=9222)
    # First _wait_for_composer (the pre-navigation poll) finds a composer.
    d._wait_for_composer = AsyncMock(side_effect=[True])
    d.navigate_new_chat = AsyncMock()

    await d._ensure_send_ready()

    d.navigate_new_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_send_ready_navigates_when_home_tab_has_no_composer():
    """The bug case: attached tab is a home/landing page (no composer on the
    first poll). _ensure_send_ready must navigate to a new chat (?model=auto,
    which renders the real composer) and then succeed on the re-poll."""
    d = CDPDriver(cdp_port=9222)
    # First _wait_for_composer (no composer) → navigate_new_chat → composer now.
    d._wait_for_composer = AsyncMock(side_effect=[False, True])
    d.navigate_new_chat = AsyncMock()
    d._capture_selector_diagnostic = AsyncMock()

    await d._ensure_send_ready()  # must not raise

    d.navigate_new_chat.assert_awaited_once()
    d._capture_selector_diagnostic.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_send_ready_fail_closed_when_composer_still_absent():
    """Fail-closed: if navigating to a new chat still yields no composer, raise
    (and capture a diagnostic so the drift is diagnosable). Never silently
    admit an unsendable tab as send-ready."""
    d = CDPDriver(cdp_port=9222)
    # Both polls fail — composer absent before AND after navigation.
    d._wait_for_composer = AsyncMock(return_value=False)
    d.navigate_new_chat = AsyncMock()
    d._capture_selector_diagnostic = AsyncMock()

    with pytest.raises(RuntimeError, match="No composer found after navigating"):
        await d._ensure_send_ready()

    d.navigate_new_chat.assert_awaited_once()
    d._capture_selector_diagnostic.assert_awaited_once_with(
        "composer (connect send-ready)"
    )


def test_has_composer_parses_ready_flag_from_js():
    """_has_composer reads a {ready: bool} JSON payload from _js and returns
    a real bool (the home page, which returns ready:false, must be False)."""
    d = CDPDriver(cdp_port=9222)
    d._js = AsyncMock(return_value=json.dumps({"ready": True}))
    import asyncio
    assert asyncio.get_event_loop().run_until_complete(d._has_composer()) is True

    d._js = AsyncMock(return_value=json.dumps({"ready": False}))
    assert asyncio.get_event_loop().run_until_complete(d._has_composer()) is False


@pytest.mark.asyncio
async def test_connect_calls_ensure_send_ready_after_auth(monkeypatch):
    """connect() must invoke _ensure_send_ready AFTER _refresh_token (so we
    never navigate on an unauthenticated page). Auth first, send-readiness
    second. Pins the ordering invariant and that connect establishes it."""
    d = CDPDriver(cdp_port=9222)
    order = []

    async def _noop_ws(*a, **kw):
        return MagicMock()
    # Stub the CDP plumbing so connect runs its body without a real Chrome.
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.websockets.connect", _noop_ws)
    d._find_page_ws = lambda: "ws://fake"
    d._find_owned_tab_ws = lambda: None
    d._adopt_existing_chatgpt_tab = lambda: None
    d._create_owned_tab = AsyncMock(return_value="ws://fake")
    d._reader_loop = AsyncMock()
    d._live_target_ids = AsyncMock(return_value=[])
    d._wait_for_chatgpt_ready = AsyncMock()
    d._refresh_token = AsyncMock(
        side_effect=lambda: order.append("auth")
    )
    d._has_composer = AsyncMock(return_value=True)
    d._ensure_send_ready = AsyncMock(
        side_effect=lambda: order.append("send_ready")
    )
    d._start_heartbeat = lambda: order.append("heartbeat")

    await d.connect()

    assert order == ["auth", "send_ready", "heartbeat"], (
        "send-readiness must run AFTER auth, before heartbeat"
    )
    d._ensure_send_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_survives_send_readiness_failure(monkeypatch):
    """If _ensure_send_ready raises, connect() must NOT abort — reads
    (list_models etc.) work without a composer, and the failure is logged.
    A broken composer shouldn't take down server startup."""
    d = CDPDriver(cdp_port=9222)

    async def _noop_ws(*a, **kw):
        return MagicMock()
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.websockets.connect", _noop_ws)
    d._find_page_ws = lambda: "ws://fake"
    d._find_owned_tab_ws = lambda: None
    d._adopt_existing_chatgpt_tab = lambda: None
    d._create_owned_tab = AsyncMock(return_value="ws://fake")
    d._reader_loop = AsyncMock()
    d._live_target_ids = AsyncMock(return_value=[])
    d._wait_for_chatgpt_ready = AsyncMock()
    d._refresh_token = AsyncMock()
    d._ensure_send_ready = AsyncMock(
        side_effect=RuntimeError("No composer found after navigating to a new chat")
    )
    d._start_heartbeat = lambda: None

    await d.connect()  # must not raise
    d._ensure_send_ready.assert_awaited_once()


# ── 8. CDP auto-reconnect on dead socket ──────────────────────────────
#
# Found while restoring a bricked live bridge this session: reconnect() exists
# (cdp_driver.py:491) but had ZERO callers — it was dead code. So a single
# mid-session WebSocket drop (the "no close frame" case) permanently bricked a
# long-running bridge: every subsequent _cdp call re-raised the dead-socket
# error forever, nothing triggered recovery. _cdp() now reconnects-once on a
# dead socket and retries the call (guarded against recursion by _retry).

def test_should_reconnect_recognizes_dead_socket_errors():
    """_should_reconnect returns True ONLY for socket-death signatures, never
    for timeouts or application errors (those must surface, not reconnect)."""
    assert CDPDriver._should_reconnect(Exception("no close frame received or sent")) is True
    assert CDPDriver._should_reconnect(Exception("Connection closed")) is True
    # Name-based check (websockets.ConnectionClosedError) without importing it
    class FakeConnectionClosedError(Exception):
        pass
    FakeConnectionClosedError.__name__ = "ConnectionClosedError"
    assert CDPDriver._should_reconnect(FakeConnectionClosedError("x")) is True
    # NOT reconnect triggers:
    assert CDPDriver._should_reconnect(TimeoutError("CDP timeout")) is False
    assert CDPDriver._should_reconnect(ValueError("bad param")) is False


@pytest.mark.asyncio
async def test_cdp_reconnects_and_retries_on_dead_socket():
    """When the WebSocket dies mid-call, _cdp must reconnect once and retry,
    succeeding on the second attempt. This is the exact path that bricked the
    live bridge before the fix (reconnect() existed but was never called)."""
    d = CDPDriver(cdp_port=9222)
    dead_ws = MagicMock()
    # First send raises the dead-socket signature; the retry (post-reconnect)
    # uses a fresh ws whose reader resolves the pending future.
    fresh_ws = MagicMock()

    call_count = {"send": 0}

    async def fake_send(payload):
        call_count["send"] += 1
        if call_count["send"] == 1:
            raise Exception("no close frame received or sent")
        # Second send on fresh_ws: simulate the reader resolving the future.
        import json as _json
        msg = _json.loads(payload)
        mid = msg["id"]
        # Resolve the pending future for this id with a canned response.
        fut = d._pending.get(mid)
        if fut and not fut.done():
            fut.set_result({"result": {"ok": True}})

    dead_ws.send = fake_send
    fresh_ws.send = fake_send

    d._ws = dead_ws
    reconnect_calls = {"n": 0}

    async def fake_reconnect():
        reconnect_calls["n"] += 1
        d._ws = fresh_ws  # reconnect swaps to the fresh socket

    d.reconnect = fake_reconnect

    result = await d._cdp("Runtime.evaluate", {"expression": "1+1"})
    assert reconnect_calls["n"] == 1, "must reconnect exactly once"
    assert result == {"result": {"ok": True}}, "must return the retried result"
    assert call_count["send"] == 2, "must have sent twice (first died, retry ok)"


@pytest.mark.asyncio
async def test_cdp_does_not_reconnect_on_non_socket_errors():
    """A non-dead-socket error (e.g. ValueError) must propagate immediately —
    never trigger a reconnect. Reconnect is ONLY for socket death."""
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()

    async def bad_send(payload):
        raise ValueError("not a socket error")

    d._ws.send = bad_send
    d.reconnect = AsyncMock()

    with pytest.raises(ValueError, match="not a socket error"):
        await d._cdp("Runtime.evaluate")

    d.reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cdp_does_not_loop_if_reconnect_also_fails():
    """If reconnect succeeds but the retry STILL hits a dead socket, the call
    must propagate — not reconnect again (infinite loop). The _retry=False
    guard on the recursive call enforces one-and-done."""
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    reconnect_calls = {"n": 0}

    async def always_dead(payload):
        raise Exception("no close frame received or sent")

    d._ws.send = always_dead

    async def fake_reconnect():
        reconnect_calls["n"] += 1
        # reconnect "succeeds" but the socket is still dead on retry

    d.reconnect = fake_reconnect

    with pytest.raises(Exception, match="no close frame"):
        await d._cdp("Runtime.evaluate")

    assert reconnect_calls["n"] == 1, "must reconnect at most ONCE, never loop"


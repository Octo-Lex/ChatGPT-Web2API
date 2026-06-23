"""Tests for tab isolation — per-process owned Chrome tabs.

Verifies that connect() creates a dedicated tab via Target.createTarget,
close() cleans it up via Target.closeTarget, fallback works when
createTarget fails, and reconnect re-finds or re-creates the owned tab.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver

# Reuse the richer fake from test_cdp_foundation for tests that drive the real
# _refresh_token body — it scripts Runtime.evaluate responses by CDP id.
from tests.test_cdp_foundation import FakeWebSocket


class _FakeWS:
    """Minimal fake websocket for connect/close tests."""
    def __init__(self):
        self.state = MagicMock()
        self.state.name = "OPEN"
        self._closed = False
    async def close(self):
        self._closed = True
        self.state.name = "CLOSED"
    async def recv(self):
        # Block until cancelled — simulates an idle socket. The reader task
        # gets cancelled on close(), which raises CancelledError here.
        await asyncio.Event().wait()
    async def send(self, data):
        pass


def _mock_ws_connect(fake_ws=None):
    """Return an AsyncMock for websockets.connect.

    The driver uses `self._ws = await websockets.connect(...)` — not as a
    context manager. So the mock must be awaitable and return the WS directly.
    """
    ws = fake_ws or _FakeWS()
    mock = AsyncMock(return_value=ws)
    return mock, ws


def _make_driver():
    d = CDPDriver(cdp_port=9222)
    d._access_token = "tok"
    d._token_fetched_at = time.time()
    return d


# ── 1. connect creates an owned tab via Target.createTarget ───────────

@pytest.mark.asyncio
async def test_connect_creates_owned_tab():
    """connect() calls _browser_cdp('Target.createTarget') and stores the
    targetId, then connects to that tab's page WS."""
    d = _make_driver()

    # Mock _browser_cdp to return a fake targetId
    async def fake_browser_cdp(method, params=None, timeout=10):
        if method == "Target.createTarget":
            return {"id": 1, "result": {"targetId": "test-tab-id-123"}}
        return {"id": 1, "result": {}}
    d._browser_cdp = fake_browser_cdp

    # Mock _create_owned_tab's /json/list lookup
    fake_targets = [{"id": "test-tab-id-123", "webSocketDebuggerUrl": "ws://fake/tab123"}]
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_targets).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # Mock _refresh_token and the WS connect. Also stub
        # _wait_for_chatgpt_ready (called before _refresh_token in connect) —
        # without this the real helper would fire CDP frames into _FakeWS's
        # no-op socket. See test_connect_waits_for_chatgpt_ready for the spy form.
        d._wait_for_chatgpt_ready = AsyncMock()
        d._refresh_token = AsyncMock()
        mock_connect, fake_ws = _mock_ws_connect()
        with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
            await d.connect()

    assert d._target_id == "test-tab-id-123"


# ── 2. close calls Target.closeTarget on owned tab ────────────────────

@pytest.mark.asyncio
async def test_close_closes_owned_tab():
    """close() calls _browser_cdp('Target.closeTarget') with the targetId —
    but only when _owns_target is True (we created the tab). An adopted tab
    is left open."""
    d = _make_driver()
    d._target_id = "test-tab-id-456"
    d._owns_target = True  # we created it → close() should tear it down
    d._ws = MagicMock()
    d._ws.close = AsyncMock()

    close_calls = []
    async def fake_browser_cdp(method, params=None, timeout=10):
        close_calls.append((method, params))
        return {"id": 1, "result": {}}
    d._browser_cdp = fake_browser_cdp

    await d.close()

    assert d._target_id is None  # cleared after close
    assert len(close_calls) == 1
    assert close_calls[0] == ("Target.closeTarget", {"targetId": "test-tab-id-456"})


# ── 3. Fallback to shared tab when createTarget fails ─────────────────

@pytest.mark.asyncio
async def test_connect_falls_back_to_shared_tab():
    """When _browser_cdp raises, connect() falls back to _find_page_ws()."""
    d = _make_driver()

    # Mock _browser_cdp to fail
    async def failing_browser_cdp(method, params=None, timeout=10):
        raise ConnectionError("browser WS unavailable")
    d._browser_cdp = failing_browser_cdp

    # Mock _find_page_ws to succeed
    async def fake_find_page_ws():
        return "ws://fake/shared-tab"
    d._find_page_ws = fake_find_page_ws
    # No tab to adopt — forces the createTarget path, which fails and falls
    # back to shared-tab mode.
    d._adopt_existing_chatgpt_tab = lambda: None
    d._wait_for_chatgpt_ready = AsyncMock()  # see test_connect_creates_owned_tab
    d._refresh_token = AsyncMock()
    mock_connect, fake_ws = _mock_ws_connect()
    with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
        await d.connect()

    assert d._target_id is None  # no owned tab — using shared
    assert d._owns_target is False
    assert d._ws is not None


# ── 4. close does NOT call closeTarget without an owned tab ───────────

@pytest.mark.asyncio
async def test_close_no_closetarget_without_owned_tab():
    """When _target_id is None (shared tab mode), close() skips closeTarget."""
    d = _make_driver()
    d._target_id = None  # shared mode
    d._owns_target = False
    d._ws = MagicMock()
    d._ws.close = AsyncMock()

    browser_calls = []
    async def spy_browser_cdp(method, params=None, timeout=10):
        browser_calls.append(method)
        return {}
    d._browser_cdp = spy_browser_cdp

    await d.close()
    assert len(browser_calls) == 0  # no closeTarget call


# ── 5. Reconnect re-finds owned tab if it still exists ────────────────

@pytest.mark.asyncio
async def test_reconnect_refinds_owned_tab():
    """When reconnect() runs and the owned tab still exists in /json/list,
    it reconnects to that tab (not creating a new one)."""
    d = _make_driver()
    d._target_id = "owned-tab-789"

    # Mock _find_owned_tab_ws to find the tab
    def fake_find_owned():
        return "ws://fake/owned-789"
    d._find_owned_tab_ws = fake_find_owned
    d._wait_for_chatgpt_ready = AsyncMock()  # see test_connect_creates_owned_tab
    d._refresh_token = AsyncMock()

    mock_connect, fake_ws = _mock_ws_connect()
    with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
        await d.reconnect()

    assert d._target_id == "owned-tab-789"  # unchanged


# ── 6. Reconnect re-creates tab if owned tab is gone ──────────────────

@pytest.mark.asyncio
async def test_reconnect_recreates_if_tab_gone():
    """When reconnect() runs and the owned tab is gone (and nothing is
    adoptable), it creates a new one."""
    d = _make_driver()
    d._target_id = "old-tab-gone"

    # _find_owned_tab_ws returns None (tab gone)
    d._find_owned_tab_ws = lambda: None
    # _adopt_existing_chatgpt_tab also returns None (no chatgpt.com tab to
    # adopt) so reconnect falls through to createTarget.
    d._adopt_existing_chatgpt_tab = lambda: None

    # Mock _create_owned_tab to succeed with a new id
    async def fake_create():
        d._target_id = "new-tab-999"
        d._owns_target = True
        return "ws://fake/new-999"
    d._create_owned_tab = fake_create
    d._wait_for_chatgpt_ready = AsyncMock()  # see test_connect_creates_owned_tab
    d._refresh_token = AsyncMock()

    mock_connect, fake_ws = _mock_ws_connect()
    with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
        await d.reconnect()

    assert d._target_id == "new-tab-999"  # re-created


# ── 7. connect() waits for the owned tab to load before refreshing token ─
#
# Regression guard for the startup race: _refresh_token used to fire on a
# freshly-created tab that hadn't navigated to chatgpt.com yet, so the relative
# fetch('/api/auth/session') resolved against the wrong origin and returned an
# empty accessToken, killing the MCP process before `initialize`. connect() now
# calls _wait_for_chatgpt_ready first.

@pytest.mark.asyncio
async def test_connect_waits_for_chatgpt_ready():
    """connect() awaits _wait_for_chatgpt_ready before _refresh_token."""
    d = _make_driver()

    async def fake_browser_cdp(method, params=None, timeout=10):
        if method == "Target.createTarget":
            return {"id": 1, "result": {"targetId": "tab-wait-ready"}}
        return {"id": 1, "result": {}}
    d._browser_cdp = fake_browser_cdp

    fake_targets = [{"id": "tab-wait-ready", "webSocketDebuggerUrl": "ws://fake/wait"}]
    # Order matters: spy on both readiness and token refresh, then assert the
    # call order so the race can't silently regress (e.g. someone reordering).
    call_order = []
    ready_spy = AsyncMock(side_effect=lambda: call_order.append("ready"))
    token_spy = AsyncMock(side_effect=lambda: call_order.append("token"))
    d._wait_for_chatgpt_ready = ready_spy
    d._refresh_token = token_spy

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_targets).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        mock_connect, _ = _mock_ws_connect()
        with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
            await d.connect()

    ready_spy.assert_awaited_once()
    token_spy.assert_awaited_once()
    assert call_order == ["ready", "token"], "readiness must precede token fetch"


# ── 8. _refresh_token retries and recovers on a transient empty token ────
#
# Drives the REAL _refresh_token body (the first test in the suite to do so —
# every other test stubs it as AsyncMock). Scripts the fake WS so the first
# Runtime.evaluate returns an empty token (the race condition) and the second
# returns a valid one, then asserts the driver self-heals.

@pytest.mark.asyncio
async def test_refresh_token_retries_on_empty():
    """_refresh_token retries when the first fetch returns an empty token.

    Uses real (short) sleeps for the 0.5s backoff — NOT monkeypatched. Patching
    asyncio.sleep globally (cdp_driver.asyncio IS the singleton module) would
    break FakeWebSocket.recv's 0.05s poll yield, deadlocking _reader_loop.
    ~0.5s of real waiting per test is the correct trade here.
    """
    d = CDPDriver(cdp_port=9222)
    ws = FakeWebSocket()
    # _refresh_token sends Runtime.evaluate; _js reads result.result.value.
    # Attempt 1 → empty token (the race). Attempt 2 → real token.
    ws.enqueue(1, {"result": {"value": '{"token": "", "user": ""}'}})
    ws.enqueue(2, {"result": {"value": '{"token": "real-token-xyz", "user": "tester"}'}})
    d._ws = ws
    d._reader_task = asyncio.create_task(d._reader_loop())

    try:
        await d._refresh_token()
    finally:
        d._reader_task.cancel()
        try:
            await d._reader_task
        except asyncio.CancelledError:
            pass

    assert d._access_token == "real-token-xyz"
    assert d._user_name == "tester"


# ── 9. _refresh_token gives up after max retries with the same error ─────

@pytest.mark.asyncio
async def test_refresh_token_fails_after_max_retries():
    """After 3 empty-token attempts, _refresh_token raises RuntimeError.

    Real sleeps (see test_refresh_token_retries_on_empty for why not patched).
    """
    d = CDPDriver(cdp_port=9222)
    ws = FakeWebSocket()
    # All three attempts return empty → the auth gate never clears.
    for mid in (1, 2, 3):
        ws.enqueue(mid, {"result": {"value": '{"token": "", "user": ""}'}})
    d._ws = ws
    d._reader_task = asyncio.create_task(d._reader_loop())

    try:
        with pytest.raises(RuntimeError, match="No access token"):
            await d._refresh_token()
    finally:
        d._reader_task.cancel()
        try:
            await d._reader_task
        except asyncio.CancelledError:
            pass

    assert d._access_token == ""  # never populated


# ── 10. connect() adopts an existing chatgpt.com tab instead of creating one ─
#
# Regression guard for the multi-tab startup bug: when Chrome is already on
# chatgpt.com (its launch URL) or a prior run left a chatgpt.com tab behind,
# connect() must reuse that tab rather than firing Target.createTarget and
# piling up redundant tabs. The original code always called createTarget, so
# each restart added another tab; adoption keeps the count stable.

@pytest.mark.asyncio
async def test_connect_adopts_existing_chatgpt_tab_in_adopt_mode():
    """In tab_mode='adopt', connect() reuses an existing chatgpt.com tab and
    does NOT call Target.createTarget — no new tab is opened.

    Adoption is opt-in (the pre-multi-session behavior): the default mode is
    'owned', which always creates a dedicated tab. This test pins that the
    adopt path still works for single-process compatibility."""
    d = _make_driver()
    d.tab_mode = "adopt"

    # /json/list shows an existing chatgpt.com page tab (e.g. Chrome's launch
    # tab, or a leftover from a previous service run).
    fake_targets = [{
        "id": "existing-tab-1",
        "type": "page",
        "url": "https://chatgpt.com/",
        "title": "ChatGPT",
        "webSocketDebuggerUrl": "ws://fake/existing",
    }]
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_targets).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # Spy on _browser_cdp — it must NOT be called for createTarget.
        create_called = []
        async def spy_browser_cdp(method, params=None, timeout=10):
            create_called.append(method)
            return {"result": {"targetId": "should-not-be-used"}}
        d._browser_cdp = spy_browser_cdp
        d._wait_for_chatgpt_ready = AsyncMock()
        d._refresh_token = AsyncMock()

        mock_connect, _ = _mock_ws_connect()
        with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
            await d.connect()

    assert d._target_id == "existing-tab-1"
    assert d._owns_target is False  # adopted → not ours to close
    assert create_called == [], "connect() must not call createTarget when a tab is adoptable"


# ── 11. close() leaves an adopted tab open ──────────────────────────────
#
# Companion to the adoption test: an adopted tab is not ours, so close()
# must NOT close it (the user, or Chrome's own lifecycle, owns it). Only
# tabs we created via Target.createTarget get torn down.

@pytest.mark.asyncio
async def test_close_leaves_adopted_tab_open():
    """close() does NOT call Target.closeTarget on an adopted tab."""
    d = _make_driver()
    d._target_id = "adopted-tab-abc"
    d._owns_target = False  # adopted, not created
    d._ws = MagicMock()
    d._ws.close = AsyncMock()

    browser_calls = []
    async def spy_browser_cdp(method, params=None, timeout=10):
        browser_calls.append(method)
        return {}
    d._browser_cdp = spy_browser_cdp

    await d.close()

    assert d._target_id is None  # cleared
    assert d._owns_target is False
    assert browser_calls == [], "close() must not closeTarget an adopted tab"


# ── 12. _owns_target defaults to False on a fresh driver ────────────────

def test_fresh_driver_does_not_own_target():
    """A newly-constructed CDPDriver has no target and owns nothing."""
    d = CDPDriver(cdp_port=9222)
    assert d._target_id is None
    assert d._owns_target is False


# ── 13. owned mode (default) creates a tab even when one exists ─────────
#
# This is the multi-session isolation guarantee: the DEFAULT behavior is to
# create a dedicated tab per driver, NOT reuse an existing chatgpt.com tab.
# Reusing would let two simultaneous drivers contend on the same DOM.

@pytest.mark.asyncio
async def test_default_owned_mode_creates_tab_even_when_chatgpt_tab_exists():
    """In the default tab_mode='owned', connect() must call Target.createTarget
    even if a chatgpt.com page tab already exists. This is what makes the
    bridge safe for multiple simultaneous sessions: each driver gets its own
    DOM instead of fighting over a shared tab."""
    d = _make_driver()  # default tab_mode='owned'
    assert d.tab_mode == "owned"

    # /json/list shows an existing chatgpt.com page tab — the OLD default
    # would have adopted it. The new default must ignore it and create.
    fake_targets = [{
        "id": "existing-tab-to-ignore",
        "type": "page",
        "url": "https://chatgpt.com/",
        "title": "ChatGPT",
        "webSocketDebuggerUrl": "ws://fake/existing",
    }]
    create_called = []

    async def fake_browser_cdp(method, params=None, timeout=10):
        if method == "Target.createTarget":
            create_called.append(method)
            return {"result": {"targetId": "new-owned-tab"}}
        return {"result": {}}
    d._browser_cdp = fake_browser_cdp

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        # _create_owned_tab polls /json/list for the new tab's WS URL.
        new_targets = fake_targets + [{
            "id": "new-owned-tab", "webSocketDebuggerUrl": "ws://fake/new"
        }]
        mock_resp.read.return_value = json.dumps(new_targets).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        d._wait_for_chatgpt_ready = AsyncMock()
        d._refresh_token = AsyncMock()
        mock_connect, _ = _mock_ws_connect()
        with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
            await d.connect()

    assert create_called == ["Target.createTarget"], \
        "owned mode must create a tab, not adopt the existing one"
    assert d._target_id == "new-owned-tab"
    assert d._owns_target is True  # we created it → close() will tear it down


# ── 14. two drivers in owned mode get distinct target ids ───────────────
#
# The concrete multi-session payoff: two CDPDriver instances (e.g. the REST
# process and the MCP process) each create their own tab, so neither can
# navigate the other's DOM. This is the regression guard for the interference
# bug — if someone reverts to adoption-by-default, this fails.

@pytest.mark.asyncio
async def test_two_owned_drivers_get_distinct_target_ids():
    """Two drivers in default owned mode create distinct tabs — the DOM
    isolation that prevents cross-session conversation corruption."""
    # Each driver's _browser_cdp hands out a different targetId, simulating
    # Chrome creating two real tabs.
    def make_driver_with_create(target_id):
        d = _make_driver()
        async def fake_browser_cdp(method, params=None, timeout=10):
            if method == "Target.createTarget":
                return {"result": {"targetId": target_id}}
            return {"result": {}}
        d._browser_cdp = fake_browser_cdp
        return d

    d1 = make_driver_with_create("tab-for-driver-1")
    d2 = make_driver_with_create("tab-for-driver-2")

    def fake_urlopen(targets):
        m = MagicMock()
        m.read.return_value = json.dumps(targets).encode()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        return m

    for d, tid in [(d1, "tab-for-driver-1"), (d2, "tab-for-driver-2")]:
        with patch("urllib.request.urlopen",
                   return_value=fake_urlopen(
                       [{"id": tid, "webSocketDebuggerUrl": f"ws://fake/{tid}"}]
                   )):
            d._wait_for_chatgpt_ready = AsyncMock()
            d._refresh_token = AsyncMock()
            mock_connect, _ = _mock_ws_connect()
            with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
                await d.connect()

    assert d1._target_id != d2._target_id, \
        "two owned drivers must hold distinct tabs — shared id means shared DOM"
    assert d1._owns_target and d2._owns_target


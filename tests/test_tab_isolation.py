"""Tests for tab isolation — per-process owned Chrome tabs.

Verifies that connect() creates a dedicated tab via Target.createTarget,
close() cleans it up via Target.closeTarget, fallback works when
createTarget fails, and reconnect re-finds or re-creates the owned tab.
"""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from chatgpt_web2api.cdp_driver import CDPDriver


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

        # Mock _refresh_token and the WS connect
        d._refresh_token = AsyncMock()
        mock_connect, fake_ws = _mock_ws_connect()
        with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
            await d.connect()

    assert d._target_id == "test-tab-id-123"


# ── 2. close calls Target.closeTarget on owned tab ────────────────────

@pytest.mark.asyncio
async def test_close_closes_owned_tab():
    """close() calls _browser_cdp('Target.closeTarget') with the targetId."""
    d = _make_driver()
    d._target_id = "test-tab-id-456"
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
    d._refresh_token = AsyncMock()
    mock_connect, fake_ws = _mock_ws_connect()
    with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
        await d.connect()

    assert d._target_id is None  # no owned tab — using shared
    assert d._ws is not None


# ── 4. close does NOT call closeTarget without an owned tab ───────────

@pytest.mark.asyncio
async def test_close_no_closetarget_without_owned_tab():
    """When _target_id is None (shared tab mode), close() skips closeTarget."""
    d = _make_driver()
    d._target_id = None  # shared mode
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
    d._refresh_token = AsyncMock()

    mock_connect, fake_ws = _mock_ws_connect()
    with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
        await d.reconnect()

    assert d._target_id == "owned-tab-789"  # unchanged


# ── 6. Reconnect re-creates tab if owned tab is gone ──────────────────

@pytest.mark.asyncio
async def test_reconnect_recreates_if_tab_gone():
    """When reconnect() runs and the owned tab is gone, it creates a new one."""
    d = _make_driver()
    d._target_id = "old-tab-gone"

    # _find_owned_tab_ws returns None (tab gone)
    d._find_owned_tab_ws = lambda: None

    # Mock _create_owned_tab to succeed with a new id
    async def fake_create():
        d._target_id = "new-tab-999"
        return "ws://fake/new-999"
    d._create_owned_tab = fake_create
    d._refresh_token = AsyncMock()

    mock_connect, fake_ws = _mock_ws_connect()
    with patch("chatgpt_web2api.cdp_driver.websockets.connect", mock_connect):
        await d.reconnect()

    assert d._target_id == "new-tab-999"  # re-created

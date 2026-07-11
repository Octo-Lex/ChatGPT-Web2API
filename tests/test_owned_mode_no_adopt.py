"""Tests: owned-mode never adopts an arbitrary tab.

The adopt fallback (_find_page_ws / _adopt_existing_chatgpt_tab) picks ANY
chatgpt.com tab from /json/list without checking ownership. In a multi-process
setup (REST bridge + SSE server), this can steal another process's tab,
causing two drivers to race on the same DOM.

These tests verify that in owned mode:
  1. connect() does NOT fall back to _find_page_ws when tab creation fails
  2. _reconnect() does NOT fall back to _find_page_ws when it can't find/create a tab
  3. adopt mode still falls back (the legacy behavior is preserved)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver


def _make_driver(tab_mode="owned", parallel_tabs=False):
    """Build a CDPDriver with mocked transport for testing."""
    driver = CDPDriver(cdp_port=9222, tab_mode=tab_mode, parallel_tabs=parallel_tabs)
    driver._browser_cdp = AsyncMock()
    driver._find_page_ws = AsyncMock(return_value="ws://stolen-tab")
    driver._find_owned_tab_ws = MagicMock(return_value=None)
    driver._adopt_existing_chatgpt_tab = MagicMock(return_value=None)
    driver._tab_registry = None  # skip registry reclaim
    driver._wait_for_chatgpt_ready = AsyncMock(return_value=True)
    driver._refresh_token = AsyncMock()
    driver._attach_identity_listener = AsyncMock()
    driver._breakers = None
    return driver


# ── 1. connect() does not adopt in owned mode ─────────────────────────────


@pytest.mark.asyncio
async def test_connect_owned_mode_does_not_fall_back_to_find_page_ws():
    """When tab creation fails in owned mode, connect must NOT call _find_page_ws.
    It should raise instead — never steal another process's tab."""
    driver = _make_driver(tab_mode="owned")
    # Make _create_owned_tab fail
    driver._create_owned_tab = AsyncMock(side_effect=RuntimeError("Chrome refused"))

    with pytest.raises(Exception, match="shared-tab fallback is disabled in owned mode"):
        await driver.connect()

    # _find_page_ws must NOT have been called
    driver._find_page_ws.assert_not_called()


# ── 2. connect() still falls back in adopt mode ───────────────────────────


@pytest.mark.asyncio
async def test_connect_adopt_mode_still_falls_back():
    """Adopt mode preserves the legacy fallback to _find_page_ws."""
    driver = _make_driver(tab_mode="adopt")
    driver._create_owned_tab = AsyncMock(side_effect=RuntimeError("Chrome refused"))

    # Should fall back to _find_page_ws, not raise
    # Need to mock the websocket connect too
    with patch("chatgpt_web2api.cdp_driver.websockets.connect") as mock_ws:
        mock_ws.return_value = MagicMock()
        try:
            await driver.connect()
        except Exception:
            pass  # may fail later in connect, but the key assertion is below

    # _find_page_ws WAS called (the legacy fallback)
    driver._find_page_ws.assert_called()


# ── 3. reconnect does not adopt in owned mode ─────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_owned_mode_does_not_fall_back_to_find_page_ws():
    """When reconnect can't find or create a tab in owned mode, it must NOT
    call _find_page_ws. It should raise instead."""
    driver = _make_driver(tab_mode="owned")
    driver._target_id = "OLD-TAB-ID"  # had a tab, now gone
    driver._ws = None  # no dead socket to close
    driver._reader_task = None  # no reader to cancel
    driver._pending = {}  # no pending messages
    # _find_owned_tab_ws returns None (tab gone) — already set in _make_driver
    # _create_owned_tab fails
    driver._create_owned_tab = AsyncMock(side_effect=RuntimeError("Chrome refused"))

    with pytest.raises(Exception, match="shared-tab fallback is disabled in owned mode"):
        await driver.reconnect()

    driver._find_page_ws.assert_not_called()


# ── 4. reconnect creates new tab when owned tab is gone ──────────────────


@pytest.mark.asyncio
async def test_reconnect_owned_mode_creates_new_tab_when_old_gone():
    """When the old owned tab is gone, reconnect should create a NEW tab,
    not adopt an existing one."""
    driver = _make_driver(tab_mode="owned")
    driver._target_id = "OLD-TAB-ID"
    driver._ws = None
    driver._reader_task = None
    driver._pending = {}
    # _find_owned_tab_ws returns None (tab gone)
    driver._create_owned_tab = AsyncMock(return_value="ws://new-tab")

    with patch("chatgpt_web2api.cdp_driver.websockets.connect") as mock_ws:
        mock_ws.return_value = MagicMock()
        try:
            await driver.reconnect()
        except Exception:
            pass

    # _create_owned_tab was called (created a new tab, not adopted)
    driver._create_owned_tab.assert_called()
    # _find_page_ws was NOT called
    driver._find_page_ws.assert_not_called()

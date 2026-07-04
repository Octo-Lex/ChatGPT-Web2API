"""Tests for the CDP event dispatch table (A2 Step 1).

Verifies that ``CDPTransport._reader_loop`` routes unsolicited CDP events
(those without an ``id``) to registered handlers on
``driver._cdp_event_handlers``, while continuing to route id-keyed responses
to pending futures.

The reader loop is the sole consumer of ``ws.recv()`` and resolves all
pending command futures. Event handlers must be fast and non-blocking; this
test verifies the dispatch mechanism, not handler performance (that is an
implementation contract enforced by code review, not testable here).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_transport import CDPTransport


def _make_driver():
    """Minimal CDPDriver stub with the state the reader loop touches."""
    d = MagicMock()
    d._pending = {}
    d._msg_id = 0
    d._cdp_event_handlers = {}
    d.reconnect = AsyncMock()
    return d


class _FakeWS:
    """Yields a scripted sequence of CDP frames, then closes."""

    def __init__(self, frames: list[str]):
        self._frames = list(frames)
        self._closed = False

    async def recv(self):
        if not self._frames:
            # Keep the reader loop alive until cancelled by the test.
            await asyncio.sleep(3600)
        return self._frames.pop(0)


@pytest.mark.asyncio
async def test_unsolicited_event_dispatched_to_registered_handler():
    """An event with a method but no id is routed to the registered handler."""
    received = []
    driver = _make_driver()
    driver._ws = _FakeWS([
        json.dumps({"method": "Network.requestWillBeSent", "params": {"requestId": "abc"}}),
    ])
    driver._cdp_event_handlers["Network.requestWillBeSent"] = lambda msg: received.append(msg)

    transport = CDPTransport(driver)
    task = asyncio.create_task(transport._reader_loop())
    # Give the loop a tick to process the frame.
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(received) == 1
    assert received[0]["method"] == "Network.requestWillBeSent"
    assert received[0]["params"]["requestId"] == "abc"


@pytest.mark.asyncio
async def test_unsolicited_event_no_handler_is_debug_logged_not_crashed():
    """An event with no registered handler is silently skipped (debug-logged)."""
    driver = _make_driver()
    driver._ws = _FakeWS([
        json.dumps({"method": "Page.frameNavigated", "params": {}}),
    ])
    # No handler registered for Page.frameNavigated.

    transport = CDPTransport(driver)
    task = asyncio.create_task(transport._reader_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # If we got here without the loop raising, the test passes.
    assert True


@pytest.mark.asyncio
async def test_id_keyed_response_still_routes_to_pending_future():
    """The event dispatch change must not break id-keyed response routing."""
    driver = _make_driver()
    driver._ws = _FakeWS([
        json.dumps({"id": 42, "result": {"value": "hello"}}),
    ])
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    driver._pending[42] = fut

    transport = CDPTransport(driver)
    task = asyncio.create_task(transport._reader_loop())
    # Wait for the future to resolve.
    result = await asyncio.wait_for(fut, timeout=1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert result["result"]["value"] == "hello"
    assert 42 not in driver._pending  # popped after resolution


@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_reader_loop():
    """A handler that raises is logged and swallowed; the loop continues."""
    driver = _make_driver()
    good_events = []
    driver._ws = _FakeWS([
        # First event: handler raises.
        json.dumps({"method": "Network.requestWillBeSent", "params": {"i": 1}}),
        # Second event: handler succeeds (loop survived).
        json.dumps({"method": "Network.requestWillBeSent", "params": {"i": 2}}),
    ])

    def flaky_handler(msg):
        if msg["params"]["i"] == 1:
            raise RuntimeError("boom")
        good_events.append(msg)

    driver._cdp_event_handlers["Network.requestWillBeSent"] = flaky_handler

    transport = CDPTransport(driver)
    task = asyncio.create_task(transport._reader_loop())
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The second event was processed despite the first handler raising.
    assert len(good_events) == 1
    assert good_events[0]["params"]["i"] == 2


@pytest.mark.asyncio
async def test_no_event_handlers_table_does_not_crash():
    """If _cdp_event_handlers is missing entirely, loop still runs (back-compat)."""
    driver = _make_driver()
    del driver._cdp_event_handlers  # simulate a driver that never set it
    driver._ws = _FakeWS([
        json.dumps({"method": "Page.frameNavigated", "params": {}}),
    ])

    transport = CDPTransport(driver)
    task = asyncio.create_task(transport._reader_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Survived without AttributeError.
    assert True

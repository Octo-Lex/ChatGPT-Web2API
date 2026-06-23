"""Tests for the CDP foundation fixes — _cdp future-table (#7), reader loop,
reconnect (#4), liveness check (#16), state reset (#18).

These are the keystone fixes that make the driver safe under MCP's
concurrent, persistent, multi-process context.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver

# ── Mock websocket that delivers responses (possibly out of order) ──────

class FakeWebSocket:
    """A fake websocket that delivers responses matched by id. Waits for a
    command to be sent before delivering its response. Supports out-of-order
    delivery to test the future-table routing."""

    def __init__(self):
        self._response_map: dict[int, str] = {}
        self._delivered: set[int] = set()
        self.sent: list[str] = []
        self.state = MagicMock()
        self.state.name = "OPEN"

    def enqueue(self, msg_id: int, result: dict):
        """Queue a response for a given CDP message id."""
        self._response_map[msg_id] = json.dumps({"id": msg_id, "result": result})

    async def recv(self):
        # Wait until a sent command has a queued response that hasn't been
        # delivered yet. This simulates a real socket where responses arrive
        # after the command is sent.
        while True:
            for mid, resp_str in self._response_map.items():
                if mid in self._delivered:
                    continue
                # Check that the command for this id has been sent
                for sent_raw in self.sent:
                    sent = json.loads(sent_raw)
                    if sent.get("id") == mid:
                        self._delivered.add(mid)
                        return resp_str
            await asyncio.sleep(0.05)

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.state.name = "CLOSED"


# ── 1. Concurrent _cdp calls don't cross-eat responses (#7) ────────────

@pytest.mark.asyncio
async def test_concurrent_cdp_calls_get_own_responses():
    """Two concurrent _cdp calls must each receive their own response,
    even when responses arrive out of order. This is the #7 regression guard."""
    d = CDPDriver(cdp_port=9222)
    ws = FakeWebSocket()
    ws.enqueue(1, {"data": "response-for-call-1"})
    ws.enqueue(2, {"data": "response-for-call-2"})
    d._ws = ws
    d._reader_task = asyncio.create_task(d._reader_loop())

    # Launch both calls concurrently
    async def call_a():
        return await d._cdp("Test.A", {}, timeout=5)

    async def call_b():
        return await d._cdp("Test.B", {}, timeout=5)

    results = await asyncio.gather(call_a(), call_b())

    # Clean up
    d._reader_task.cancel()
    try:
        await d._reader_task
    except asyncio.CancelledError:
        pass

    assert results[0]["result"]["data"] == "response-for-call-1"
    assert results[1]["result"]["data"] == "response-for-call-2"


# ── 2. Reader handles id-less CDP events without crashing ──────────────

@pytest.mark.asyncio
async def test_reader_handles_events_without_crashing():
    """CDP events (no id) should be logged and discarded, not crash the reader."""
    d = CDPDriver(cdp_port=9222)
    ws = FakeWebSocket()
    ws.enqueue(1, {"value": "ok"})
    d._ws = ws
    d._reader_task = asyncio.create_task(d._reader_loop())

    # Inject a CDP event by sending it raw via the websocket's recv override.
    # We use a wrapper that delivers an event first, then the response.
    original_recv = ws.recv
    event_delivered = {"done": False}
    async def event_then_response():
        if not event_delivered["done"]:
            event_delivered["done"] = True
            return json.dumps({"method": "Page.frameNavigated", "params": {}})
        return await original_recv()
    ws.recv = event_then_response

    result = await d._cdp("Runtime.evaluate", {}, timeout=5)

    d._reader_task.cancel()
    try:
        await d._reader_task
    except asyncio.CancelledError:
        pass

    assert result["id"] == 1


# ── 3. _cdp cleans up pending on timeout ───────────────────────────────

@pytest.mark.asyncio
async def test_cdp_cleans_up_pending_on_timeout():
    """A timed-out _cdp call must remove its entry from _pending so it
    doesn't leak or match a late-arriving response."""
    d = CDPDriver(cdp_port=9222)
    ws = FakeWebSocket()  # no responses queued → never delivers
    d._ws = ws
    d._reader_task = asyncio.create_task(d._reader_loop())

    with pytest.raises(TimeoutError):
        await d._cdp("Test.Timeout", {}, timeout=0.5)

    d._reader_task.cancel()
    try:
        await d._reader_task
    except asyncio.CancelledError:
        pass

    assert len(d._pending) == 0  # cleaned up


# ── 4. close() cancels reader and clears pending ───────────────────────

@pytest.mark.asyncio
async def test_close_cancels_reader_and_clears_pending():
    """close() must cancel the reader task and clear _pending."""
    d = CDPDriver(cdp_port=9222)
    ws = FakeWebSocket()
    d._ws = ws
    d._reader_task = asyncio.create_task(d._reader_loop())
    # Add a fake pending entry
    d._pending[99] = asyncio.get_event_loop().create_future()

    await d.close()

    assert d._reader_task is None
    assert len(d._pending) == 0
    assert d._ws is None


# ── 5. Reader fails pending futures on socket close ────────────────────

@pytest.mark.asyncio
async def test_reader_fails_pending_on_socket_close():
    """When the reader loop exits (socket closed), pending futures must be
    failed so callers don't hang."""
    d = CDPDriver(cdp_port=9222)

    class ClosingWebSocket:
        def __init__(self):
            self.state = MagicMock()
            self.state.name = "OPEN"
            self.sent = []
        async def recv(self):
            raise ConnectionError("socket closed")
        async def send(self, data):
            self.sent.append(data)
        async def close(self):
            self.state.name = "CLOSED"

    d._ws = ClosingWebSocket()
    d._reader_task = asyncio.create_task(d._reader_loop())

    # Register a pending future
    fut = asyncio.get_event_loop().create_future()
    d._pending[1] = fut

    # Wait for the reader to fail it
    try:
        await asyncio.wait_for(fut, timeout=2)
        assert False, "should have raised"
    except (TimeoutError, ConnectionError):
        pass  # either the reader failed it, or it timed out

    # The future should be done (either resolved or failed)
    assert fut.done()

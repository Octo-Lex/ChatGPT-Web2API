"""Wiring tests for CDPTransport (Phase 5 PR2 extraction).

These verify the extraction moved the 7 wire-primitive methods onto
CDPTransport and that the transport reaches the driver's socket + id table
through the correct seam. They are NOT behavioral tests — the behavior of
each method is already covered exhaustively by test_cdp_foundation
(concurrent _cdp, pending cleanup, reader-on-socket-close) and the broad
suite, which stub the driver's transport methods and confirm the delegators
preserve the surface. This file guards the wiring: no method is dropped, the
transport talks to the driver (not to itself) for socket/id-table state, and
the driver wires the transport in __init__.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_transport import CDPTransport


class FakeWebSocket:
    """Fake websocket that delivers id-matched responses once their command is
    sent. Mirrors test_cdp_foundation's helper so _cdp + a reader task resolve
    through the real future-table wiring."""

    def __init__(self):
        self._response_map: dict[int, str] = {}
        self._delivered: set[int] = set()
        self.sent: list[str] = []
        self.state = MagicMock()
        self.state.name = "OPEN"

    def enqueue(self, msg_id: int, result: dict):
        self._response_map[msg_id] = json.dumps({"id": msg_id, "result": result})

    async def recv(self):
        while True:
            for mid, resp_str in self._response_map.items():
                if mid in self._delivered:
                    continue
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


def _make_transport():
    """A CDPTransport backed by a mock driver with the attributes/seam the
    transport reaches through. The driver's socket/id-table are minimal mocks
    so individual tests override only what they assert on."""
    driver = MagicMock()
    driver._ws = MagicMock()
    driver._ws.send = AsyncMock()
    driver._ws.recv = AsyncMock()
    driver._msg_id = 0
    driver._pending = {}
    driver.reconnect = AsyncMock()
    return CDPTransport(driver), driver


# ── 1. Every moved method exists on CDPTransport ──────────────────────


def test_transport_has_all_moved_methods():
    """The extraction must not drop a method. Each of these is delegated by
    CDPDriver and must live on CDPTransport."""
    transport, _ = _make_transport()
    expected = [
        "_reader_loop",
        "_cdp",
        "_should_reconnect",
        "_js",
        "_js_strict",
        "_js_with_data",
        "_js_with_data_strict",
    ]
    missing = [name for name in expected if not callable(getattr(transport, name, None))]
    assert missing == [], f"CDPTransport is missing moved methods: {missing}"


def test_should_reconnect_is_staticmethod():
    """_should_reconnect must remain callable without an instance (the driver
    delegates to it as CDPTransport._should_reconnect(exc))."""
    assert CDPTransport._should_reconnect(Exception("connection closed")) is True
    assert CDPTransport._should_reconnect(TimeoutError()) is False


# ── 2. State seam: transport reaches driver._ws / _msg_id / _pending ──


@pytest.mark.asyncio
async def test_cdp_reaches_driver_ws_msg_id_pending():
    """_cdp must send on the driver's socket and register its future in the
    driver's pending table keyed by the driver's msg id. The reader task
    (sole recv consumer) resolves the future — mirroring the real wiring."""
    transport, driver = _make_transport()
    ws = FakeWebSocket()
    ws.enqueue(1, {"ok": True})
    driver._ws = ws
    driver._reader_task = asyncio.create_task(transport._reader_loop())

    result = await transport._cdp("Test.Method", {"a": 1}, timeout=5)

    driver._reader_task.cancel()
    try:
        await driver._reader_task
    except asyncio.CancelledError:
        pass

    assert result["result"] == {"ok": True}
    # Sent on the driver's socket, with the driver's id.
    assert len(ws.sent) == 1
    sent = ws.sent[0]
    assert '"Test.Method"' in sent
    # msg_id advanced on the driver (not a transport-local counter).
    assert driver._msg_id == 1


@pytest.mark.asyncio
async def test_reader_loop_consumes_driver_ws_recv_and_routes_to_pending():
    """_reader_loop must be the sole recv() consumer and route responses into
    the driver's _pending table by id."""
    transport, driver = _make_transport()

    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    driver._pending[42] = fut

    frames = iter([
        '{"id": 42, "result": {"value": "hello"}}',
        # An event (no id) — must be discarded, not routed.
        '{"method": "Page.frameNavigated"}',
    ])

    async def _fake_recv():
        try:
            return next(frames)
        except StopIteration:
            raise asyncio.CancelledError()

    driver._ws.recv = _fake_recv
    with pytest.raises(asyncio.CancelledError):
        await transport._reader_loop()
    assert fut.result() == {"id": 42, "result": {"value": "hello"}}


@pytest.mark.asyncio
async def test_cdp_calls_driver_reconnect_on_socket_death_then_retries():
    """On a socket-death send failure, _cdp must call back into
    driver.reconnect() exactly once and retry once (the _retry guard). The
    reader task resolves the retry attempt's future."""
    transport, driver = _make_transport()

    send_calls = {"n": 0}

    async def _flaky_send(payload):
        send_calls["n"] += 1
        ws.sent.append(payload)
        if send_calls["n"] == 1:
            raise ConnectionError("connection closed")

    ws = FakeWebSocket()
    ws.enqueue(2, {"ok": True})  # response for the retried (second) attempt
    ws.send = _flaky_send  # first send raises, second succeeds
    driver._ws = ws
    driver._reader_task = asyncio.create_task(transport._reader_loop())

    await transport._cdp("Test.A", {}, timeout=5, _retry=True)

    driver._reader_task.cancel()
    try:
        await driver._reader_task
    except asyncio.CancelledError:
        pass

    driver.reconnect.assert_awaited_once()
    assert send_calls["n"] == 2  # failed once, retried once after reconnect


# ── 3. JS wrappers reach the driver-facing _cdp / _js seam ────────────
#
# The transport's _js → _cdp and _js_with_data → _js internal routing goes
# through self._driver (the driver-facing seam), NOT self. This preserves
# monkeypatch interception: tests patch driver._cdp / driver._js and expect
# them to intercept — the same seam BackendClient relies on for its driver
# transport calls.


@pytest.mark.asyncio
async def test_js_reaches_driver_cdp_and_unwraps_value():
    transport, driver = _make_transport()

    async def _fake_cdp(method, params=None, timeout=15, _retry=True):
        return {"result": {"result": {"value": "v"}}}

    driver._cdp = _fake_cdp
    assert await transport._js("expr") == "v"


@pytest.mark.asyncio
async def test_js_with_data_injects_data_as___D():
    """_js_with_data wraps the template into an IIFE passing __D as a JSON
    argument (never string-concatenated), then delegates to driver._js."""
    transport, driver = _make_transport()
    captured = {}

    async def _fake_js(expr, timeout=15):
        captured["expr"] = expr
        return "wrapped-seen"

    driver._js = _fake_js
    out = await transport._js_with_data("doThing(__D.x)", {"x": 'esc"aped'})
    assert out == "wrapped-seen"
    # The data must be JSON-serialized, not concatenated raw.
    assert '__D) => (doThing(__D.x))' in captured["expr"]
    assert '{"x": "esc\\"aped"}' in captured["expr"]


@pytest.mark.asyncio
async def test_js_strict_raises_cdp_error_on_error_blob():
    from chatgpt_web2api.cdp_driver import CDPJSError

    transport, driver = _make_transport()

    async def _fake_cdp(method, params=None, timeout=15, _retry=True):
        return {"error": {"message": "execution context destroyed"}}

    driver._cdp = _fake_cdp
    with pytest.raises(CDPJSError):
        await transport._js_strict("expr")


@pytest.mark.asyncio
async def test_js_with_data_strict_delegates_to_driver_js_strict():
    transport, driver = _make_transport()
    seen = {}

    async def _fake_js_strict(expr, timeout=15):
        seen["expr"] = expr
        return "strict-seen"

    driver._js_strict = _fake_js_strict
    out = await transport._js_with_data_strict("t(__D.n)", {"n": 1})
    assert out == "strict-seen"
    assert "(__D) => (t(__D.n))" in seen["expr"]


# ── 4. State stays on the driver (not migrated into the transport) ────


def test_transport_holds_no_socket_state():
    """The transport must NOT own _ws / _msg_id / _pending / _reader_task —
    those stay on the driver so connect/reconnect/close and test stubs that
    poke driver._ws / driver._pending keep working."""
    transport, _ = _make_transport()
    for attr in ("_ws", "_msg_id", "_pending", "_reader_task"):
        assert not hasattr(transport, attr), (
            f"CDPTransport must not own driver state '{attr}' (Phase 5 PR2 contract)"
        )


# ── 5. Integration: CDPDriver wires CDPTransport in __init__ ──────────


def test_driver_wires_cdp_transport():
    """CDPDriver.__init__ constructs a CDPTransport and delegates through it."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    assert isinstance(d._transport, CDPTransport)
    assert d._transport._driver is d


def test_driver_delegators_preserve_signatures():
    """All 7 moved methods remain callable on the driver (delegators), so
    internal call sites and test stubs that patch driver._js / driver._cdp
    etc. keep working unchanged."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    for name in (
        "_reader_loop",
        "_cdp",
        "_js",
        "_js_strict",
        "_js_with_data",
        "_js_with_data_strict",
    ):
        assert callable(getattr(d, name, None)), f"CDPDriver lost delegator {name}"
    assert callable(CDPDriver._should_reconnect)

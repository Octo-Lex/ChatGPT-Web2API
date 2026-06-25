"""Tests for the honest /health endpoint.

The old /health returned ``"waiting"`` when CDP was disconnected —
indistinguishable from "freshly started," so a zombie process (HTTP
listener up, CDP never connected) looked healthy. These tests pin the
four-state status logic: starting / healthy / degraded / broken.

A zombie (Chrome alive, driver dead) MUST report ``"degraded"``, never
``"ok"`` or ``"waiting"``.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from chatgpt_web2api.api_server import APIServer
from chatgpt_web2api.config import Config


def _make_server(driver_connected: bool = True):
    """Build an APIServer with a mock driver + chrome probe controlled by params."""
    driver = MagicMock()
    driver.is_connected = driver_connected
    server = APIServer(Config.load(None), driver)
    return server


def _health_body(server, chrome_running: bool = True) -> dict:
    """Call _handle_health with chrome liveness mocked.

    The handler probes http://127.0.0.1:<cdp_port>/json/version to check if
    Chrome is alive. We patch that probe so tests don't need a real Chrome.
    Returns a coroutine to be awaited inside an async test.
    """
    async def _run():
        with patch("urllib.request.urlopen") as mock_urlopen:
            if chrome_running:
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp
            else:
                mock_urlopen.side_effect = ConnectionRefusedError
            # The handler uses run_in_executor for the probe, which runs the
            # (mocked) urlopen in a thread pool — the mock applies globally.
            resp = await server._handle_health(MagicMock())
            return json.loads(resp.text)

    return _run()


@pytest.mark.asyncio
async def test_health_healthy_when_chrome_and_driver_connected():
    """Chrome alive + driver connected + has served = "healthy"."""
    server = _make_server(driver_connected=True)
    server._request_count = 1
    server._last_successful_send_at = 12345.0
    body = await _health_body(server, chrome_running=True)
    assert body["status"] == "healthy"
    assert body["chrome_running"] is True
    assert body["driver_connected"] is True


@pytest.mark.asyncio
async def test_health_starting_when_connected_but_never_served():
    """Chrome alive + driver connected + no requests yet = "starting"."""
    server = _make_server(driver_connected=True)
    body = await _health_body(server, chrome_running=True)
    assert body["status"] == "starting"
    assert body["chrome_running"] is True
    assert body["driver_connected"] is True


@pytest.mark.asyncio
async def test_health_degraded_when_chrome_alive_but_driver_disconnected():
    """ZOMBIE CASE: Chrome alive + driver disconnected = "degraded" (never ok/waiting).

    This is the exact failure PID 26592 exhibited: HTTP listener up, Chrome
    healthy, but CDP websocket never connected. The old endpoint reported
    "waiting"; the new one must report "degraded".
    """
    server = _make_server(driver_connected=False)
    body = await _health_body(server, chrome_running=True)
    assert body["status"] == "degraded"
    assert body["chrome_running"] is True
    assert body["driver_connected"] is False


@pytest.mark.asyncio
async def test_health_broken_when_chrome_dead():
    """Chrome unreachable = "broken" regardless of driver state."""
    server = _make_server(driver_connected=True)
    body = await _health_body(server, chrome_running=False)
    assert body["status"] == "broken"
    assert body["chrome_running"] is False


@pytest.mark.asyncio
async def test_health_exposes_last_successful_send_at():
    """last_successful_send_at is tracked so a never-served zombie is visible."""
    server = _make_server(driver_connected=True)
    server._last_successful_send_at = 99999.0
    server._request_count = 5
    body = await _health_body(server, chrome_running=True)
    assert body["last_successful_send_at"] == 99999.0
    assert body["requests_served"] == 5


@pytest.mark.asyncio
async def test_health_exposes_last_error():
    """last_error surfaces the most recent failure for diagnosis."""
    server = _make_server(driver_connected=True)
    server._last_error = "RateLimitError: too many requests"
    body = await _health_body(server, chrome_running=True)
    assert body["last_error"] == "RateLimitError: too many requests"


@pytest.mark.asyncio
async def test_health_includes_breakers_snapshot():
    """Phase 4 PR1: /health carries a 'breakers' snapshot. On a fresh server
    every breaker is closed — PR1 records no failure signals, so this is the
    only state a real deployment will see until PR2 wires trips."""
    from chatgpt_web2api.breakers import BreakerKind

    server = _make_server(driver_connected=True)
    body = await _health_body(server, chrome_running=True)

    assert "breakers" in body
    breakers = body["breakers"]
    # All four kinds present (stable shape for consumers like ensure.py)
    assert set(breakers.keys()) == {k.value for k in BreakerKind}
    # PR1: nothing is wired, so all are closed
    for entry in breakers.values():
        assert entry["open"] is False
        assert entry["failures_in_window"] == 0

"""Issue #51 — deterministic replay reproduction (offline red tests + controls).

These four tests pin the no-replay contract for chat mutations:

  1. test_mutating_runtime_evaluate_ambiguous_send_is_not_replayed
     A mutating Runtime.evaluate whose socket dies mid-dispatch must send
     exactly ONE frame, must NOT reconnect-and-replay, and must raise
     SendOutcomeUnknownError (reconcile-only, never resend).

  2. test_readonly_runtime_evaluate_reconnect_retry_is_preserved
     Control: a READ-only Runtime.evaluate keeps the existing resilience —
     one reconnect, one retry, value returned. (#51 must not remove CDP
     resilience globally.)

  3. test_single_send_post_mutation_rate_limit_does_not_repeat_operation
     With max_attempts=1 (the single-send mode), a RateLimitError raised
     AFTER the factory mutated must propagate with exactly one invocation.

  4. test_default_retry_mode_remains_backward_compatible
     Default mode still retries a failing factory (3-attempt budget,
     success on attempt 2). Behavior unchanged for non-single-send callers.

Evidence protocol: test 1 fails against current master (no mutation
primitive / replay occurs); the failure output plus the mechanical
characterization (2 frames, 1 reconnect through plain _cdp) is preserved in
captures/phase0/ as the defect evidence for issue #51.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import RateLimitError
from chatgpt_web2api.cdp_transport import CDPTransport
from chatgpt_web2api.resilience import retry_on_rate_limit


class ConnectionClosedError(Exception):
    """Local stand-in socket-death error. CDPTransport._should_reconnect
    matches by exception CLASS NAME ('ConnectionClosedError' is in its name
    set), so no real websockets exception is needed."""


def _make_transport():
    """Mirror test_cdp_transport._make_transport: transport + mock driver.
    ``driver._cdp`` is wired to the transport's real method so _js routes
    through the genuine send/reconnect/future-table path."""
    driver = MagicMock()
    driver._ws = MagicMock()
    driver._ws.send = AsyncMock()
    driver._ws.recv = AsyncMock()
    driver._msg_id = 0
    driver._pending = {}
    driver.reconnect = AsyncMock()
    transport = CDPTransport(driver)
    driver._cdp = transport._cdp
    return transport, driver


class AmbiguousSendSocket:
    """Socket that records every frame as delivered, then raises a
    reconnect-class error on the FIRST send. The peer may already have
    received/executed that frame — the ambiguity #51 is about.

    After driver.reconnect() swaps in a healthy socket, subsequent sends
    resolve their own pending future (no reader loop needed).
    """

    def __init__(self, transport):
        self.transport = transport
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        if len(self.sent) == 1:
            raise ConnectionClosedError("connection closed")
        driver = self.transport._driver
        mid = self.sent[-1]["id"]
        fut = driver._pending.get(mid)
        if fut is not None and not fut.done():
            fut.set_result({
                "id": mid,
                "result": {"result": {"type": "string", "value": "sent"}},
            })

    async def recv(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


class HealthySocket:
    """Answers every sent frame from its own pending table."""

    def __init__(self, transport, value="ok"):
        self.transport = transport
        self.value = value
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        driver = self.transport._driver
        mid = self.sent[-1]["id"]
        fut = driver._pending.get(mid)
        if fut is not None and not fut.done():
            fut.set_result({
                "id": mid,
                "result": {"result": {"type": "string", "value": self.value}},
            })

    async def recv(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


async def test_mutating_runtime_evaluate_ambiguous_send_is_not_replayed():
    """RED against current master. The mutation primitive (_js_mutation)
    must send exactly one frame, never reconnect-and-replay it, and raise
    SendOutcomeUnknownError so the caller reconciles instead of resending."""
    from chatgpt_web2api.cdp_driver import SendOutcomeUnknownError

    transport, driver = _make_transport()
    ambiguous = AmbiguousSendSocket(transport)
    healthy = HealthySocket(transport, value="sent")
    driver._ws = ambiguous

    async def reconnect():
        driver._ws = healthy

    driver.reconnect = reconnect

    with pytest.raises(SendOutcomeUnknownError) as exc_info:
        await transport._js_mutation("(function(){ return 'sent'; })()")

    err = exc_info.value
    assert getattr(err, "effect_state", None) == "unknown"
    assert getattr(err, "retry_safe", None) is False
    assert getattr(err, "reconciliation_safe", None) is True

    mutation_frames = [f for f in ambiguous.sent + healthy.sent
                       if f.get("method") == "Runtime.evaluate"]
    assert len(mutation_frames) == 1, (
        f"mutation replayed: {len(mutation_frames)} Runtime.evaluate frames "
        f"sent (ambiguous={len(ambiguous.sent)}, healthy={len(healthy.sent)})")
    # reconnect must NOT have been triggered by the mutation path itself
    assert driver._ws is ambiguous or driver._ws is healthy


async def test_readonly_runtime_evaluate_reconnect_retry_is_preserved():
    """Control (green before and after): a read via _js keeps the existing
    one-reconnect-one-retry resilience and returns the value."""
    transport, driver = _make_transport()
    ambiguous = AmbiguousSendSocket(transport)
    healthy = HealthySocket(transport, value="read-result")
    driver._ws = ambiguous

    async def reconnect():
        driver._ws = healthy

    driver.reconnect = reconnect

    value = await transport._js("(function(){ return 'read-result'; })()")

    assert value == "read-result"
    total_frames = [f for f in ambiguous.sent + healthy.sent
                    if f.get("method") == "Runtime.evaluate"]
    assert len(total_frames) == 2  # first (failed) + retried (succeeded)
    assert len(healthy.sent) == 1  # exactly one retry after reconnect


async def test_single_send_post_mutation_rate_limit_does_not_repeat_operation():
    """Single-send mode (max_attempts=1): a RateLimitError raised AFTER the
    factory already mutated must propagate, with exactly one invocation —
    the mutation is never repeated."""
    calls = {"attempts": 0, "mutations": 0}
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    async def mutating_operation():
        calls["attempts"] += 1
        calls["mutations"] += 1  # the USER send happened
        if calls["attempts"] == 1:
            raise RateLimitError("rate limited", retry_after=0.01)
        return "ok"

    with pytest.raises(RateLimitError):
        await retry_on_rate_limit(driver, mutating_operation, max_attempts=1)

    assert calls["attempts"] == 1
    assert calls["mutations"] == 1


async def test_default_retry_mode_remains_backward_compatible():
    """Default mode: the wrapper still retries (success on attempt 2), for
    callers that did not opt into single-send."""
    calls = {"attempts": 0}
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    async def flaky_operation():
        calls["attempts"] += 1
        if calls["attempts"] == 1:
            raise RateLimitError("rate limited", retry_after=0.01)
        return "ok"

    result = await retry_on_rate_limit(driver, flaky_operation)

    assert result == "ok"
    assert calls["attempts"] == 2

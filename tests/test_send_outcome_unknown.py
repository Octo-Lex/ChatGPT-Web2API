"""#51 evidence-preservation and boundary tests.

Three groups pinning the bridge between the transport fix and the future
UNKNOWN→CONFIRMED reconciliation path, plus the breaker's new success
boundary and the single_send scoping:

  A. An ambiguous click (SendOutcomeUnknownError) preserves an in-flight
     captured UUID on the error before the capture scope closes.
  B. COMPOSER_SEND_READINESS success records exactly at submission evidence
     (captured UUID, or DOM ack True) and NOT on an unknown outcome.
  C. single_send is honored by chat_completion / chat_with_gpt only;
     create_memory keeps the default retry budget even if the argument
     appears.

These tests drive the REAL send_and_stream orchestration with mocked
collaborators (constructed like test_composer_selectors._make_driver).
The transport-level frame guarantees (one mutation frame, no replay) are
covered in test_no_replay_send.py and are not duplicated here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from chatgpt_web2api.breakers import BreakerKind, BreakerRegistry
from chatgpt_web2api.cdp_driver import (
    AMBIGUOUS_CAPTURE_WINDOW_S,
    CDPDriver,
    SendOutcomeUnknownError,
)


class _StopStream(Exception):
    """Marker raised by the mocked completion stream to end send_and_stream
    right after the breaker-success point — everything past it (URL wait,
    reconciliation) is out of scope for these tests."""


def _make_send_driver(*, captured_uuid, click_raises=None, dom_ack=None):
    """A CDPDriver whose send_and_stream collaborators are mocked so the
    orchestration up to (and including) the breaker-success point runs
    genuinely; the completion stream raises _StopStream immediately after."""
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    reg = BreakerRegistry()
    d._breakers = reg
    successes: list[BreakerKind] = []
    _orig = reg.record_success
    reg.record_success = lambda kind: (successes.append(kind), _orig(kind))[1]  # type: ignore[method-assign]

    d._assert_owned_tab_required = AsyncMock()
    d._read_assistant_count_baseline = AsyncMock(return_value=0)
    d._capture_pre_send_fallback_anchor = AsyncMock(return_value=MagicMock())

    listener = MagicMock()
    listener.reenable_if_stale = AsyncMock()
    listener.is_alive.return_value = True
    scope = MagicMock()
    listener.arm_capture_scope.return_value = scope
    listener.wait_for_captured_uuid = AsyncMock(return_value=captured_uuid)
    d._identity_listener = listener

    d.type_message = AsyncMock()
    if click_raises is not None:
        d.click_send = AsyncMock(side_effect=click_raises)
    else:
        d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=dom_ack)

    async def _raising_stream(**kwargs):
        raise _StopStream()
        yield  # pragma: no cover — makes this an async generator

    d._completion = MagicMock()
    d._completion.stream_until_complete = _raising_stream
    return d, listener, scope, successes


async def _driven_send(driver):
    """Consume send_and_stream; return the exception it terminated with."""
    try:
        async for _ in driver.send_and_stream("sentinel-text"):
            pass
    except Exception as e:  # _StopStream or the error under test
        return e
    return None


# ── A. UUID preservation on the ambiguous path ──────────────────────

async def test_ambiguous_click_preserves_captured_uuid_before_scope_close():
    """The click frame may have executed remotely; the listener may hold or
    be resolving the outgoing UUID. send_and_stream must attach it to the
    SendOutcomeUnknownError before the finally closes the capture scope."""
    err = SendOutcomeUnknownError("ambiguous dispatch")
    d, listener, scope, successes = _make_send_driver(
        captured_uuid="uuid-causal-1", click_raises=err)

    raised = await _driven_send(d)

    assert raised is err
    assert err.captured_user_id == "uuid-causal-1"
    # The preservation wait used the short bounded window, not the normal 5s
    listener.wait_for_captured_uuid.assert_awaited_once()
    assert listener.wait_for_captured_uuid.await_args.kwargs.get(
        "timeout") == AMBIGUOUS_CAPTURE_WINDOW_S
    # The capture scope DID close (finally) — evidence was preserved first.
    scope.close.assert_called_once()
    # Unknown outcome is never submission evidence → no breaker success.
    assert successes == []


async def test_ambiguous_click_without_capture_leaves_field_none():
    """No UUID resolved in the window → captured_user_id stays None and the
    error still propagates (reconcile-only, evidence-less UNKNOWN)."""
    err = SendOutcomeUnknownError("ambiguous dispatch")
    d, listener, scope, successes = _make_send_driver(
        captured_uuid=None, click_raises=err)

    raised = await _driven_send(d)

    assert raised is err
    assert err.captured_user_id is None
    assert successes == []


# ── B. Breaker success at the new boundary ──────────────────────────

async def test_breaker_success_on_captured_uuid():
    """Captured UUID (primary submission evidence) → exactly one
    COMPOSER_SEND_READINESS record_success; the DOM ack probe is skipped."""
    d, listener, scope, successes = _make_send_driver(captured_uuid="uuid-1")

    raised = await _driven_send(d)

    assert isinstance(raised, _StopStream)  # got past the boundary
    assert successes == [BreakerKind.COMPOSER_SEND_READINESS]
    d._verify_send_acknowledged.assert_not_awaited()


async def test_breaker_success_on_dom_ack_fallback():
    """No UUID, DOM acknowledgment True (fallback evidence) → one
    record_success."""
    d, listener, scope, successes = _make_send_driver(
        captured_uuid=None, dom_ack=True)

    raised = await _driven_send(d)

    assert isinstance(raised, _StopStream)
    assert successes == [BreakerKind.COMPOSER_SEND_READINESS]
    d._verify_send_acknowledged.assert_awaited_once()


async def test_breaker_no_success_on_unknown_outcome():
    """Covered by group A assertions; explicit here for the boundary trio:
    an ambiguous send records no success (outcome unproven)."""
    d, listener, scope, successes = _make_send_driver(
        captured_uuid="uuid-2",
        click_raises=SendOutcomeUnknownError("ambiguous dispatch"))

    raised = await _driven_send(d)

    assert isinstance(raised, SendOutcomeUnknownError)
    assert successes == []


# ── C. single_send scoping ───────────────────────────────────────────

def test_single_send_scoped_to_chat_send_tools():
    """chat_completion / chat_with_gpt honor single_send; create_memory
    keeps the default budget even when the argument is present."""
    from chatgpt_web2api.mcp_server import _chat_tool_attempts

    assert _chat_tool_attempts(
        "chat_completion", {"single_send": True}) == 1
    assert _chat_tool_attempts(
        "chat_with_gpt", {"single_send": True}) == 1
    # Non-send chat tool: scope isolation — the flag is ignored.
    assert _chat_tool_attempts(
        "create_memory", {"single_send": True}) == 3
    # Defaults unchanged when the flag is absent.
    assert _chat_tool_attempts("chat_completion", {}) == 3
    assert _chat_tool_attempts("create_memory", {}) == 3

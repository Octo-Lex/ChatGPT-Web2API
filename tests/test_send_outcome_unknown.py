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

import asyncio
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


def _make_send_driver(*, captured_uuid, click_raises=None, dom_ack=None,
                      stream="raise"):
    """A CDPDriver whose send_and_stream collaborators are mocked so the
    orchestration up to (and including) the breaker-success point runs
    genuinely. stream="raise" ends at the boundary point via _StopStream;
    stream="complete" runs a full successful turn through reconciliation."""
    from types import SimpleNamespace

    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    reg = BreakerRegistry()
    d._breakers = reg
    successes: list[BreakerKind] = []
    _orig = reg.record_success
    reg.record_success = lambda kind: (successes.append(kind), _orig(kind))[1]  # type: ignore[method-assign]

    d._assert_owned_tab_required = MagicMock()  # sync seam — must not be AsyncMock
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

    if stream == "raise":
        async def _raising_stream(**kwargs):
            raise _StopStream()
            yield  # pragma: no cover — makes this an async generator

        d._completion.stream_until_complete = _raising_stream
    else:
        async def _completing_stream(**kwargs):
            yield MagicMock()

        d._completion.stream_until_complete = _completing_stream
        d._completion.last_dom_text = ""
        d._completion.had_non_text_content = False
        d._js_strict = AsyncMock(
            return_value="https://chatgpt.com/c/conv-p2")
        d._fetch_text_for_turn = AsyncMock(return_value=SimpleNamespace(
            status="matched", text="hello world", diagnostic=None))
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


# ── C2. Breaker downstream-success fallback (Codex P2 review) ────────

async def test_inconclusive_ack_completed_turn_clears_breaker_failures():
    """No UUID + an inconclusive DOM probe (acknowledged=None) + a turn that
    reaches FULL completion → the fallback records COMPOSER_SEND_READINESS
    success exactly once, so successful sends clear prior failure history."""
    from chatgpt_web2api.breakers import BreakerKind

    d, listener, scope, successes = _make_send_driver(
        captured_uuid=None, dom_ack=None, stream="complete")
    d.last_send_outcome_unknown = {"stale": True}  # cleared at send start

    raised = await _driven_send(d)

    assert raised is None  # full successful turn
    assert successes == [BreakerKind.COMPOSER_SEND_READINESS]
    assert d.last_send_outcome_unknown is None  # side channel cleared at start


async def test_uuid_evidence_completed_turn_records_success_once():
    """UUID already recorded the success at submission evidence; the
    downstream fallback must not double-record."""
    from chatgpt_web2api.breakers import BreakerKind

    d, listener, scope, successes = _make_send_driver(
        captured_uuid="uuid-1", stream="complete")

    raised = await _driven_send(d)

    assert raised is None
    assert successes == [BreakerKind.COMPOSER_SEND_READINESS]


async def test_inconclusive_ack_failed_stream_records_no_success():
    """An inconclusive probe plus a stream that RAISES is not evidence —
    no breaker success (fallback unreachable on failure)."""
    d, listener, scope, successes = _make_send_driver(
        captured_uuid=None, dom_ack=None, stream="raise")

    raised = await _driven_send(d)

    assert isinstance(raised, _StopStream)
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


# ── D. Surface mappings preserve the UNKNOWN distinction ────────────

def test_rest_error_response_maps_unknown_to_409_with_evidence():
    """#51: an ambiguous outcome must not surface as a bare 500 (the status
    class SDKs blindly retry). It maps to 409 with code send_outcome_unknown,
    retry_safe=false, and the preserved captured_user_id when present. The
    x-should-retry:false header is load-bearing: the official OpenAI SDKs
    check it BEFORE their status rules and auto-retry 409 otherwise."""
    import json as _json

    from chatgpt_web2api.api_server import APIServer

    server = object.__new__(APIServer)  # _error_response is pure (no self use)

    exc = SendOutcomeUnknownError("ambiguous dispatch")
    exc.captured_user_id = "uuid-causal-9"
    resp = server._error_response(exc)
    assert resp.status == 409
    assert resp.headers["x-should-retry"] == "false"
    assert "Retry-After" not in resp.headers
    body = _json.loads(resp.text)
    assert body["error"]["code"] == "send_outcome_unknown"
    assert body["error"]["retry_safe"] is False
    assert body["error"]["captured_user_id"] == "uuid-causal-9"


def test_rest_error_response_unknown_without_evidence_omits_field():
    import json as _json

    from chatgpt_web2api.api_server import APIServer

    server = object.__new__(APIServer)
    resp = server._error_response(SendOutcomeUnknownError("ambiguous dispatch"))
    assert resp.status == 409
    assert resp.headers["x-should-retry"] == "false"
    assert "Retry-After" not in resp.headers
    body = _json.loads(resp.text)
    assert body["error"]["code"] == "send_outcome_unknown"
    assert "captured_user_id" not in body["error"]


def test_mcp_maps_unknown_to_structured_do_not_resend_error():
    from chatgpt_web2api.mcp_server import _map_tool_exception

    exc = SendOutcomeUnknownError("ambiguous dispatch")
    exc.captured_user_id = "uuid-causal-7"
    result = _map_tool_exception(exc)
    assert result is not None and result.isError
    text = result.content[0].text
    assert "send_outcome_unknown" in text
    assert "uuid-causal-7" in text
    assert "NOT send it again" in text


# ── E. Cancellation during the ambiguity window ─────────────────────

async def test_cancellation_during_ambiguity_window_still_closes_scope():
    """Cancelling while the preservation wait is in flight must propagate
    CancelledError (never swallowed by the evidence wait) and still reach
    the finally that closes the capture scope."""
    err = SendOutcomeUnknownError("ambiguous dispatch")
    d, listener, scope, successes = _make_send_driver(
        captured_uuid=None, click_raises=err)

    async def slow_capture(**kwargs):
        await asyncio.sleep(5)
        return "too-late-uuid"

    listener.wait_for_captured_uuid = slow_capture

    task = asyncio.get_running_loop().create_task(_driven_send(d))
    await asyncio.sleep(0.3)  # inside the 1s window wait now
    task.cancel()
    try:
        await task
        raised = None
    except asyncio.CancelledError:
        raised = "cancelled"

    assert raised == "cancelled"  # the wait did not convert cancellation
    scope.close.assert_called_once()  # finally still ran
    assert successes == []

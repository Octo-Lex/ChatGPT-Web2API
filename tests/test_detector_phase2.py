"""Behavioral tests for P1 phase-2 two-state detector machine.

These verify the core co-designed behavior (conversation 6a4ebc1e):

  1. Reasoning first-content wait does NOT fail at the old 90s default — the
     longer reasoning budget keeps the detector alive during the silent
     thinking phase.
  2. Stream-idle (text appeared then stopped) fails with its OWN budget,
     shorter than first-content.
  3. Hard cap wins over an active DOM liveness signal — no infinite waits.
  4. Final reconciliation: if the turn completed in the backend, the detector
     returns normally instead of raising (generation actually finished).
  5. Structured error: phase-2 stalls carry stall_kind, model_class, elapsed
     time, and liveness-signal fields.

Timing is controlled via a fake clock to keep tests deterministic and fast.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.completion_detector import (
    CompletionDetector,
    DetectorBudgets,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnEndResult


def _make_detector(budgets=None, conv_id="conv-1"):
    """Detector backed by a mock driver. budgets defaults to reasoning (the
    case the P1 fix targets)."""
    driver = MagicMock()
    driver._current_conv_id = conv_id
    driver._js_strict = AsyncMock(return_value="")
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="not_ready"))
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value=conv_id or "")
    detector = CompletionDetector(driver)
    if budgets is not None:
        detector._budgets_override = budgets
    return detector, driver


def _phase2_poll_payload(*, text="", md_text="", is_thinking=False,
                         has_action=False, html_len=0, child_count=0):
    """Build the JSON the phase-2 poll JS returns."""
    return json.dumps({
        "text": text, "md_text": md_text, "html_len": html_len,
        "child_count": child_count, "has_action": has_action,
        "is_thinking": is_thinking,
    })


class _ScriptedPoll:
    """A scripted _js_strict that returns pre-set payloads per phase.

    - body scan → always empty (no rate limit)
    - assistant count → always "1" (exceeds initial_count=0, exits phase-1)
    - phase-2 poll → call next() from a user-provided sequence
    """

    def __init__(self, poll_sequence):
        self.scan = '{"text": ""}'
        self.polls = list(poll_sequence)
        self._idx = 0

    async def __call__(self, expr):
        if "getBoundingClientRect" in expr:
            if self._idx < len(self.polls):
                return self.polls[self._idx]
            return self.polls[-1] if self.polls else _phase2_poll_payload()
        if "innerText" in expr:
            return self.scan
        return "1"


# ── 1. Reasoning first-content does NOT fail at 90s ─────────────────────


@pytest.mark.asyncio
async def test_reasoning_first_content_survives_past_90s(monkeypatch):
    """The core P1 fix: a reasoning model thinking silently for >90s must NOT
    be falsely aborted. The reasoning first-content budget (300s default) keeps
    the detector alive. With the old uniform 90s, this would have raised."""
    budgets = DetectorBudgets.reasoning()  # first_content=300, stream_idle=120
    detector, driver = _make_detector(budgets=budgets)

    # Simulate: thinking indicator present, no text, for 100 polls (50s of
    # "thinking" — well past the old 90s ceiling IF the clock were real, but
    # we control the clock). We just need to confirm no stall raises at the
    # old threshold. Use a fast-forward clock.
    thinking_payload = _phase2_poll_payload(is_thinking=True)
    script = _ScriptedPoll([thinking_payload])
    driver._js_strict = script

    # Fast-forward clock: control time.monotonic so 100s of "thinking" passes
    # in real-time milliseconds. This proves the 90s boundary is gone for
    # reasoning first-content.
    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d  # each 0.5s poll advances simulated time
        await original_sleep(0)  # don't actually wait

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    # timeout is the overall wall-clock — set high enough that it doesn't fire
    # before the first-content budget.
    async def drain():
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
        ):
            pass

    # Run for a bounded number of events then cancel — we just need to confirm
    # it does NOT raise within the first 100s of simulated thinking. Each poll
    # advances simulated time by 0.5s; let it run ~200 polls (100s simulated)
    # via real-time yielding (no actual sleeping — fast_sleep does original_sleep(0)).
    task = asyncio.create_task(drain())
    # Let the task run enough event-loop ticks to advance simulated time past 90s.
    # Each poll = one sleep(0) yield, so we need ~200 polls = ~200 iterations.
    for _ in range(300):
        await original_sleep(0)
        if t[0] > 100:
            break
    # If the detector raised, task.exception() will hold it.
    exc = task.exception() if task.done() else None
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    if exc and "GenerationStuckError" in type(exc).__name__:
        pytest.fail(f"Reasoning model falsely aborted during thinking phase: {exc}")

    # Confirm simulated time advanced past the old 90s ceiling without abort
    assert t[0] > 90, f"Expected simulated time >90s, got {t[0]}"


# ── 2. Stream-idle fails with its own (shorter) budget ──────────────────


@pytest.mark.asyncio
async def test_stream_idle_uses_shorter_budget_after_first_content(monkeypatch):
    """Once text has appeared and then stopped progressing, the stream-idle
    budget applies — which is shorter than first-content for reasoning models.
    Verify the stall_kind is 'stream_idle_timeout', not 'first_content_timeout'."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=300,  # long
        stream_idle_timeout_seconds=5,       # short — will fire fast
        hard_timeout_seconds=900,
    )
    detector, driver = _make_detector(budgets=budgets)

    # First poll: text appears (exits first-content state). Then: text stops.
    polls = [
        _phase2_poll_payload(text="Hello", md_text="Hello"),
        _phase2_poll_payload(text="Hello", md_text="Hello"),  # no progress
    ] * 50  # repeat to keep polling
    script = _ScriptedPoll(polls)
    driver._js_strict = script
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
        ):
            pass

    # The stall should be classified as stream_idle (text appeared then stopped)
    assert exc_info.value.phase == "phase_2_stream"
    assert getattr(exc_info.value, "stall_kind", None) == "stream_idle_timeout", (
        f"Expected stall_kind='stream_idle_timeout', got {getattr(exc_info.value, 'stall_kind', None)}"
    )


# ── 3. Hard cap wins over active DOM signal ─────────────────────────────


@pytest.mark.asyncio
async def test_hard_cap_wins_over_dom_liveness(monkeypatch):
    """Even if the DOM shows a thinking/generating indicator forever, the hard
    cap must eventually fire. No infinite waits."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=300,
        stream_idle_timeout_seconds=300,
        hard_timeout_seconds=10,  # very short hard cap
    )
    detector, driver = _make_detector(budgets=budgets)

    # DOM shows thinking indicator forever, never produces text
    script = _ScriptedPoll([_phase2_poll_payload(is_thinking=True)])
    driver._js_strict = script

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError):
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
        ):
            pass

    # The hard cap (10s simulated) should have fired, not the first-content (300s)
    assert t[0] >= 10, f"Hard cap should have fired around 10s, got t={t[0]}"
    assert t[0] < 300, "first-content budget should NOT have been the one to fire"


# ── 4. Final reconciliation success returns normally ────────────────────


@pytest.mark.asyncio
async def test_final_reconciliation_success_returns_normally(monkeypatch):
    """When the phase-2 stall fires but final reconciliation finds the turn DID
    complete in the backend, the detector returns normally instead of raising.
    This is the field-verified case: 'generation completed after detector gave up.'"""
    budgets = DetectorBudgets(
        first_content_timeout_seconds=5,  # short to trigger stall fast
        stream_idle_timeout_seconds=5,
        hard_timeout_seconds=900,
    )
    detector, driver = _make_detector(budgets=budgets)

    # Phase-2: no text, no thinking indicator → stall fires quickly.
    script = _ScriptedPoll([_phase2_poll_payload()])
    driver._js_strict = script

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    # Final reconciliation: backend says the turn completed
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="matched")
    )

    chunks = []
    # This should NOT raise — reconciliation finds the completed turn.
    async for chunk in detector.stream_until_complete(
        initial_count=0, timeout=500,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
        budgets=budgets,
    ):
        chunks.append(chunk)

    # No exception — the detector returned normally after reconciliation.
    # (If we got here, the test passes. If reconciliation had failed, a
    # GenerationStuckError would have been raised.)


# ── 5. Structured error fields ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_stall_error_has_diagnostic_fields(monkeypatch):
    """Phase-2 stall errors carry structured fields for observability:
    stall_kind, model_class, elapsed_seconds, generation_active_signal."""
    from chatgpt_web2api.cdp_driver import GenerationStuckError

    budgets = DetectorBudgets(
        first_content_timeout_seconds=5,
        stream_idle_timeout_seconds=5,
        hard_timeout_seconds=900,
    )
    detector, driver = _make_detector(budgets=budgets)

    # No text, no thinking, no progress → first_content_timeout stall
    script = _ScriptedPoll([_phase2_poll_payload()])
    driver._js_strict = script
    driver._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )

    t = [0.0]
    original_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await original_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(GenerationStuckError) as exc_info:
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=500,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
            budgets=budgets,
            model="gpt-5-5-thinking",
        ):
            pass

    err = exc_info.value
    assert err.phase == "phase_2_stream"
    assert getattr(err, "stall_kind", None) == "first_content_timeout"
    assert getattr(err, "model_class", None) == "reasoning"
    assert hasattr(err, "elapsed_seconds"), "error must carry elapsed_seconds"
    assert hasattr(err, "generation_active_signal"), "error must carry generation_active_signal"

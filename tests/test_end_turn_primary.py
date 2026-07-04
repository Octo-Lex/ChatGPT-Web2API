"""Regression tests for issue #12: end_turn is now the primary completion signal.

The DOM action-button selector (has_action) drifted structurally — ancestor
depth limit too shallow AND geometry gate rejects short-answer buttons (see
the #12 DOM investigation). Backend end_turn has proven stable across all
three drifts, so it is now checked FIRST when conv_id is available;
has_action is a fallback for the pre-conv_id window.

These tests verify the Python-side completion POLICY (which signal wins),
using the same mocking pattern as test_sse_end_turn_new_chat.py:
monkeypatch time.monotonic + asyncio.sleep, fake _js_strict returning
phase-appropriate JSON, AsyncMock for _fetch_end_turn_for_turn /
type_message / click_send / _fetch_text_for_turn.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.turn_anchor import TurnEndResult, TurnTextResult


def _make_driver():
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    d._access_token = "tok"
    d._token_fetched_at = time.time()
    return d


def _install_virtual_clock(monkeypatch, start=0.0):
    t = [start]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    return t


def _phase1_then_phase2_js(
    state, *, phase1_turn_after=2, phase2_factory=None, url="https://chatgpt.com/c/resolved-conv-id"
):
    """Fake _js_strict distinguishing Phase-1 (appear) from Phase-2 (poll)."""

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            state["phase2"] += 1
            return json.dumps(phase2_factory(state["phase2"]))
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1
            return "1" if state["phase1"] >= phase1_turn_after else "0"
        if "location.href" in expr:
            return url
        return ""

    return _fake_js


# ── 1. end_turn=True wins even when has_action=False ───────────────────


@pytest.mark.asyncio
async def test_end_turn_wins_over_no_has_action(monkeypatch):
    """When end_turn=True and there's usable text, completion fires even if
    has_action never becomes true (the #12 drift scenario). end_turn is PRIMARY."""
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        text = "The answer." if n > 2 else ""
        return {
            "text": text,
            "md_text": text,
            "html_len": 50,
            "child_count": 1,
            "has_action": False,
            "is_thinking": False,
        }

    d._js_strict = _phase1_then_phase2_js(state, phase2_factory=phase2)
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: anchored final-text fetch. The DOM already streamed "The answer."
    # (last_dom_text); return matched with the same text so the reconciliation
    # loop breaks cleanly (len == last_dom_text → no spurious delta).
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="The answer.")
    )
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    deltas = [c.delta for c in chunks if c.delta]
    assert any("The answer" in c for c in deltas), f"deltas: {deltas}"
    assert chunks[-1].finish_reason == "stop"
    assert d._fetch_end_turn_for_turn.await_count >= 1


# ── 2. end_turn=True wins even when is_thinking=True ───────────────────


@pytest.mark.asyncio
async def test_end_turn_wins_over_is_thinking(monkeypatch):
    """The stale-thinking deadlock: is_thinking stays True (lingering
    .result-thinking), has_action never fires. end_turn must still complete."""
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        text = "Done." if n > 2 else ""
        return {
            "text": text,
            "md_text": text,
            "html_len": 253,
            "child_count": 1,
            "has_action": False,
            "is_thinking": True,
        }

    d._js_strict = _phase1_then_phase2_js(state, phase2_factory=phase2)
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: DOM streamed "Done." (last_dom_text); return matched with same text.
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="Done.")
    )
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    deltas = [c.delta for c in chunks if c.delta]
    assert any("Done" in c for c in deltas), f"deltas: {deltas}"
    assert chunks[-1].finish_reason == "stop"


# ── 3. has_action=True completes when no conv_id is available ──────────


@pytest.mark.asyncio
async def test_has_action_fallback_without_conv_id(monkeypatch):
    """When conv_id is unavailable (URL never resolves to /c/), has_action
    is the fallback completion signal and must still work."""
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        text = "Fallback answer." if n > 2 else ""
        return {
            "text": text,
            "md_text": text,
            "html_len": 60,
            "child_count": 1,
            "has_action": n > 4,
            "is_thinking": False,
        }

    # URL never becomes a /c/ URL — conv_id stays empty
    d._js_strict = _phase1_then_phase2_js(
        state, phase2_factory=phase2, url="https://chatgpt.com/?model=auto"
    )
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: conv_id never resolves (URL stays ?model=auto) so the reconciliation
    # loop is skipped — this mock is never called. Mapped to not_ready for
    # semantic fidelity with the old return_value="".
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="not_ready")
    )
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))  # should NOT be called

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    # Completed via has_action (the fallback), NOT end_turn (conv_id was empty)
    deltas = [c.delta for c in chunks if c.delta]
    assert any("Fallback answer" in c for c in deltas), f"deltas: {deltas}"
    assert chunks[-1].finish_reason == "stop"
    # end_turn was never consulted because conv_id_for_check was never set
    assert d._fetch_end_turn_for_turn.await_count == 0


# ── 4. JS selector walks to ancestor depth 8 (structural check) ────────


def test_has_action_js_walks_depth_8():
    """The JS has_action selector must walk up to depth 8 (was 4) so it
    reaches the action row at ancestor depth 6. This is a structural check
    on the JS source — the depth limit is what the #12 investigation found
    was too shallow.

    Phase 5 PR4: the Phase-2 completion-detection JS moved from
    CDPDriver.send_and_stream into CompletionDetector.stream_until_complete;
    inspect the detector (the JS's new canonical home)."""
    import inspect

    from chatgpt_web2api.completion_detector import CompletionDetector

    src = inspect.getsource(CompletionDetector.stream_until_complete)
    # The walk loop bound must be 8, not the old 4
    assert "d <= 8" in src, "has_action JS must walk d <= 8 ancestors (was 4)"
    assert "d <= 4" not in src, "old d <= 4 depth limit must be gone"


# ── 5. JS geometry window accepts buttons above the message ───────────


def test_has_action_js_geometry_accepts_above_message():
    """Short answers place the action button ABOVE the message node. The
    geometry gate must accept top >= lastRect.top - 180 (was -8). Structural
    check on the JS source.

    Phase 5 PR4: JS moved to CompletionDetector.stream_until_complete."""
    import inspect

    from chatgpt_web2api.completion_detector import CompletionDetector

    src = inspect.getsource(CompletionDetector.stream_until_complete)
    # The geometry window must be widened to top-180 (the #12 short-answer case)
    assert "lastRect.top - 180" in src, (
        "geometry gate must accept buttons 180px above message (was top - 8)"
    )


# ── 6. has_action must NOT override a live backend that says "not done" ─


@pytest.mark.asyncio
async def test_has_action_does_not_complete_when_backend_says_not_done(monkeypatch):
    """Regression for the review finding on PR #14: with conv_id available and
    backend end_turn=False, a widened has_action match (e.g. a prior turn's
    action row caught by depth-8 / top-180) must NOT complete early. The
    backend is authoritative when available; DOM is fallback only.

    Setup: conv_id resolves after ~1s (the real new-chat timing), text is
    present (usable content), has_action goes True on poll 3+ (simulating the
    widened false-match appearing once the DOM settles), but end_turn stays
    False. The loop must keep polling until the stall guard raises
    GenerationStuckError — proving it did NOT break on the stale has_action.
    """
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        text = "Streaming answer."  # usable content present from the start
        # has_action False early (conv_id window), True from poll 3+ —
        # simulates a prior turn's button caught by the widened selector once
        # the DOM settles. By poll 3 the virtual clock has advanced past 1s so
        # conv_id has resolved and the backend is authoritative.
        return {
            "text": text,
            "md_text": text,
            "html_len": 60,
            "child_count": 1,
            "has_action": n >= 3,
            "is_thinking": False,
        }

    d._js_strict = _phase1_then_phase2_js(state, phase2_factory=phase2)
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: loop never reaches reconciliation (GenerationStuckError fires in
    # Phase-2 first). Mapped to not_ready (faithful to old "").
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="not_ready")
    )
    # Backend is reachable but says NOT done — authoritative
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="not_ready"))

    from chatgpt_web2api.cdp_driver import GenerationStuckError

    chunks = []
    with pytest.raises(GenerationStuckError):
        async for chunk in d.send_and_stream("hello", timeout=10000):
            chunks.append(chunk)

    # The loop polled the backend (conv_id resolved after ~1s) and did NOT
    # break on has_action — it ran until the stall guard fired. Proves the DOM
    # signal cannot override a live backend.
    assert d._fetch_end_turn_for_turn.await_count >= 1, "backend must be consulted once conv_id is available"

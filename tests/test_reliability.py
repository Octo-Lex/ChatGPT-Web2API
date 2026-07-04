"""Reliability tests — auth-expiry fix, stall detectors, cap removal.

Covers bugs #3 (silent auth expiry) and #1 (60s appear-cap + stuck
generation) per the implementation plan. All tests are unit-level with
mocked CDP — no live Chrome needed.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import (
    TOKEN_TTL_SECONDS,
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
)
from chatgpt_web2api.turn_anchor import TurnEndResult, TurnTextResult

# ── Helpers ────────────────────────────────────────────────────


def _make_driver():
    """A CDPDriver with a mocked websocket (no real connect)."""
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()  # truthy; is_connected will treat as open
    d._access_token = "fresh-token"
    d._token_fetched_at = time.time()
    return d


def _mock_js_with_payload(d, payload_map):
    """Make d._js_with_data return values based on a lookup of (conv_id/token)
    → response string. For backend conversation fetches the payload carries
    conv_id+token."""

    async def _fake(js_template, data, timeout=15):
        return payload_map.get(data.get("conv_id"), "")

    d._js_with_data_strict = _fake
    return d


# ── 1. Auth TTL ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_token_refreshes_when_empty():
    d = _make_driver()
    d._access_token = ""
    d._refresh_token = AsyncMock()
    await d.ensure_token()
    d._refresh_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_token_refreshes_when_stale():
    d = _make_driver()
    d._token_fetched_at = time.time() - (TOKEN_TTL_SECONDS + 10)  # older than TTL
    d._refresh_token = AsyncMock()
    await d.ensure_token()
    d._refresh_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_token_no_refresh_when_fresh():
    d = _make_driver()
    d._token_fetched_at = time.time()  # fresh
    d._refresh_token = AsyncMock()
    await d.ensure_token()
    d._refresh_token.assert_not_awaited()


# ── 2. 15-site guard ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_methods_call_ensure_token_first():
    """A representative read method (get_models) calls ensure_token before its
    fetch. Verify via mock call-order."""
    d = _make_driver()
    d._refresh_token = AsyncMock()

    # _js_with_data returns a models JSON string
    async def _fake(js_template, data, timeout=15):
        return json.dumps({"title": "Models", "models": [{"slug": "auto", "title": "Auto"}]})

    d._js_with_data_strict = _fake
    # Patch ensure_token to record the call distinctly
    call_order = []

    async def _record_token():
        call_order.append("ensure_token")
        return d._access_token

    d.ensure_token = _record_token

    async def _rec_js(template, data, timeout=15):
        call_order.append("fetch")
        return json.dumps({"title": "M", "models": [{"slug": "auto", "title": "A"}]})

    d._js_with_data_strict = _rec_js
    await d.get_models()
    assert call_order == ["ensure_token", "fetch"], f"order: {call_order}"


# ── 3. (removed) legacy _fetch_text status-decode tests ─────────
#
# The uncorrelated ``_fetch_text`` / ``_fetch_end_turn`` methods were deleted
# in the A2 Step 9/10 cleanup; the same status-decode + breaker behavior now
# lives in ``_fetch_recent_conversation_projection`` and is exercised via the
# A2 anchored fetchers (``_fetch_text_for_turn`` / ``_fetch_end_turn_for_turn``).


# ── 4. Phase-1 stall (node count never changes) ────────────────


@pytest.mark.asyncio
async def test_phase1_stall_raises_generation_stuck(monkeypatch):
    """If the assistant node count never changes for >PHASE_STALL_SECONDS,
    raise GenerationStuckError('phase_1_appear', ...) rather than waiting
    the full timeout."""
    d = _make_driver()
    # Mock time to accelerate the test: advance monotonic fast.
    t = [0.0]

    def fake_monotonic():
        return t[0]

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", fake_monotonic)

    async def fast_sleep(s):
        t[0] += s  # each sleep advances "time" by the sleep amount

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    # _js returns: rate-limit scan = harmless text; assistant count = constant 1
    call = {"n": 0}

    async def _fake_js(expr, timeout=15):
        call["n"] += 1
        # Count poll is the bare .length expression (no JSON.stringify)
        if "JSON.stringify" not in expr and ".length" in expr:
            return "1"  # constant count, never > initial_count
        return '{"text":"normal page text"}'

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()

    with pytest.raises(GenerationStuckError) as ei:
        async for _ in d.send_and_stream("hi", timeout=10000):
            pass
    assert ei.value.phase == "phase_1_appear"


# ── 5. Phase-2 stall (text never changes, Stop present) ────────


@pytest.mark.asyncio
async def test_phase2_stall_raises_generation_stuck(monkeypatch):
    """Phase 2: text unchanging + Stop button present for >stall window → raise."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    # Track call sequence rather than expression-string matching (more robust).
    # Distinguish polls by unique substrings: the count poll is a bare expression
    # ending in `.length` (no JSON.stringify); the Phase-2 poll contains
    # `JSON.stringify`; the rate-limit scan contains `body.innerText`.
    state = {"count_polls": 0, "in_phase2": False}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            # Phase-2 poll: the action button never appears (has_action=False)
            # and the text/html/child signals never change, so last_change_time
            # never resets and the stall detector fires.
            state["in_phase2"] = True
            return json.dumps(
                {"text": "partial", "html_len": 10, "child_count": 1, "has_action": False}
            )
        if "body.innerText" in expr:
            # Rate-limit scan (Phase 1)
            return json.dumps({"text": "normal page"})
        # Count poll (bare .length expression)
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(return_value=TurnTextResult(status="not_ready"))

    with pytest.raises(GenerationStuckError) as ei:
        async for _ in d.send_and_stream("hi", timeout=10000):
            pass
    assert ei.value.phase == "phase_2_stream"


# ── 6. Cap removal: slow appear succeeds ───────────────────────


@pytest.mark.asyncio
async def test_slow_appear_succeeds_without_cap(monkeypatch):
    """A response whose assistant node appears at t=70s succeeds, PROVIDED there
    is progress beforehand (count changes) so the stall clock keeps resetting.
    Before the cap removal this raised RuntimeError at t=60s regardless. The
    stall detector correctly fires only on NO progress — a slow render that
    shows intermittent node changes (e.g. loading placeholders) is allowed."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    # Count wobbles 0→1→0→1... every ~10 polls so the stall clock keeps
    # resetting, then settles at 2 (>initial) at poll 150 (~75s) to break Phase 1.
    count_polls = {"n": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            return json.dumps({"text": "done", "has_action": True})
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        count_polls["n"] += 1
        n = count_polls["n"]
        if n > 150:
            return "2"  # appear + break at ~75s
        return "1" if (n // 10) % 2 == 0 else "0"  # wobble 0/1 — progress signal

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="done")
    )

    chunks = []
    async for chunk in d.send_and_stream("hi", timeout=10000):
        chunks.append(chunk)
    assert any(c.delta for c in chunks)
    assert chunks[-1].finish_reason == "stop"


# ── 7. Progressing generation does NOT raise ───────────────────


@pytest.mark.asyncio
async def test_progressing_generation_does_not_raise(monkeypatch):
    """A generation that keeps making progress (text changing) does NOT raise
    GenerationStuckError even after the stall window would have fired. Locks
    in progress-sensitivity, not wall-clock-sensitivity."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1_polls": 0, "phase2_polls": 0, "text": ""}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            state["phase2_polls"] += 1
            state["text"] += "x"
            done = state["phase2_polls"] > 200  # ~100s of progress
            return json.dumps({"text": state["text"], "has_action": done})
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        state["phase1_polls"] += 1
        return "1" if state["phase1_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text=state["text"])
    )

    chunks = []
    async for chunk in d.send_and_stream("hi", timeout=100000):
        chunks.append(chunk)
    assert chunks[-1].finish_reason == "stop"


# ── 7b. Thinking-model streaming regression ────────────────────


@pytest.mark.asyncio
async def test_thinking_model_streams_during_answer_phase(monkeypatch):
    """Regression: when is_thinking is true but the answer is actively
    streaming, deltas MUST still be emitted.

    The old `if is_thinking / elif text-changed` structure meant
    is_thinking=true suppressed ALL delta emission — freezing
    last_dom_text="" and producing an empty response whenever the
    backend final-text fetch lagged (the common case for thinking
    models, whose conversation-API text commits late).

    Simulates the post-reasoning gap: is_thinking=true throughout
    (.result-thinking lingers after reasoning ends), answer text grows
    each poll, has_action fires at the end. With _fetch_text_for_turn
    mocked not_ready (fallback lag), the answer MUST still arrive via
    deltas."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1": 0, "phase2": 0, "text": ""}

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            state["phase2"] += 1
            state["text"] += "answer chunk. "
            return json.dumps(
                {
                    "text": "Thinking... " + state["text"],  # innerText w/ reasoning label
                    "md_text": state["text"],  # clean answer from .markdown
                    "html_len": len(state["text"]) + 20,
                    "child_count": 1,
                    "has_action": state["phase2"] > 5,
                    "is_thinking": True,  # .result-thinking lingers post-reasoning
                }
            )
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1
            return "1" if state["phase1"] > 1 else "0"
        return ""

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="not_ready")
    )  # fallback lag → empty

    chunks = []
    async for chunk in d.send_and_stream("think then answer", timeout=10000):
        chunks.append(chunk)

    full = "".join(c.delta for c in chunks if c.delta)
    assert "answer chunk" in full, f"is_thinking suppressed streaming — no answer deltas: {chunks}"
    assert "Thinking" not in full, f"reasoning UI label leaked as a delta: {full}"
    assert chunks[-1].finish_reason == "stop"


# ── 8. R4: backend end_turn fallback for completion ────────────


@pytest.mark.asyncio
async def test_phase2_end_turn_fallback_completes_when_dom_action_missing(monkeypatch):
    """R4: when the DOM action-button selector drifts (has_action stays false),
    the throttled backend end_turn check rescues the loop — the answer is fully
    present, end_turn===true, so we complete instead of stalling for 90s. This
    is the defense-in-depth that would have caught the Phase-2 bug instantly."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    # Pretend we know the conversation id (set during Phase-1 in real flow).
    d._current_conv_id = "conv-fallback-test"
    d._access_token = "tok"

    end_turn_calls = {"n": 0}
    state = {"count_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            # DOM action button NEVER appears (simulates selector drift).
            return json.dumps(
                {"text": "the full answer", "html_len": 10, "child_count": 1, "has_action": False}
            )
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="the full answer")
    )
    # Backend says end_turn is true on first check → completes.
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    end_turn_calls["n"] = 0

    async def _counting_end_turn(cid, anchor, *, had_non_text_content=False):
        end_turn_calls["n"] += 1
        return TurnEndResult(status="matched")

    d._fetch_end_turn_for_turn = _counting_end_turn

    chunks = []
    async for chunk in d.send_and_stream("hi", timeout=10000):
        chunks.append(chunk)
    # The fallback fired and broke the loop (no GenerationStuckError).
    assert end_turn_calls["n"] >= 1, "end_turn fallback must be consulted"


@pytest.mark.asyncio
async def test_phase2_end_turn_fallback_ignored_on_fetch_failure(monkeypatch):
    """R4: if the backend fetch raises, the fallback is ignored — the DOM poll
    and stall detector still govern. The loop must NOT crash on a backend error."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    d._current_conv_id = "conv-x"
    d._access_token = "tok"

    state = {"count_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            return json.dumps(
                {"text": "partial", "html_len": 10, "child_count": 1, "has_action": False}
            )
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(return_value=TurnTextResult(status="not_ready"))

    # Backend fetch raises — must be swallowed, not propagated.
    async def _raising_end_turn(cid, anchor, *, had_non_text_content=False):
        raise RuntimeError("backend blew up")

    d._fetch_end_turn_for_turn = _raising_end_turn

    # Should still raise GenerationStuckError (stall), NOT the backend error.
    with pytest.raises(GenerationStuckError) as ei:
        async for _ in d.send_and_stream("hi", timeout=10000):
            pass
    assert ei.value.phase == "phase_2_stream"


@pytest.mark.asyncio
async def test_phase2_end_turn_fallback_skipped_when_no_text(monkeypatch):
    """R4: don't complete on a bare end_turn if no answer text has streamed yet.
    The guard `last_dom_text` must be non-empty before consulting the backend,
    so an empty terminal node can't finish the loop prematurely."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    d._current_conv_id = "conv-empty"
    d._access_token = "tok"

    end_turn_calls = {"n": 0}
    state = {"count_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            # No text streamed (empty answer).
            return json.dumps({"text": "", "html_len": 0, "child_count": 0, "has_action": False})
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(return_value=TurnTextResult(status="not_ready"))

    async def _counting_end_turn(cid, anchor, *, had_non_text_content=False):
        end_turn_calls["n"] += 1
        return TurnEndResult(status="matched")

    d._fetch_end_turn_for_turn = _counting_end_turn

    with pytest.raises(GenerationStuckError):
        async for _ in d.send_and_stream("hi", timeout=10000):
            pass
    # Fallback was NEVER consulted because last_dom_text stayed empty.
    assert end_turn_calls["n"] == 0


@pytest.mark.asyncio
async def test_phase2_backend_end_turn_is_primary_over_dom(monkeypatch):
    """R4 (updated for #12): backend end_turn is the PRIMARY signal. When
    conv_id is available, the backend is consulted first; has_action is a
    fallback only when conv_id is unavailable or the backend fetch failed.
    With conv_id set and end_turn=True, completion fires via the backend
    even if has_action is also true."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    d._current_conv_id = "conv-dom"
    d._access_token = "tok"

    end_turn_calls = {"n": 0}
    state = {"count_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            return json.dumps(
                {
                    "text": "answer",
                    "md_text": "answer",
                    "html_len": 10,
                    "child_count": 1,
                    "has_action": True,
                    "is_thinking": False,
                }
            )
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="answer")
    )

    async def _counting_end_turn(cid, anchor, *, had_non_text_content=False):
        end_turn_calls["n"] += 1
        return TurnEndResult(status="matched")

    d._fetch_end_turn_for_turn = _counting_end_turn

    async for _ in d.send_and_stream("hi", timeout=10000):
        pass
    # Backend is PRIMARY now — it was consulted (conv_id was available).
    assert end_turn_calls["n"] >= 1


# ── 9. Thinking-stall fix: is_thinking as progress + fallback unlock ──


@pytest.mark.asyncio
async def test_thinking_placeholder_does_not_stall_past_90s(monkeypatch):
    """A static 'Thinking...' placeholder for >90s of simulated polls must NOT
    raise GenerationStuckError. is_thinking resets the stall clock — reasoning
    is active generation, not a stall. This is the root fix for the bug that
    broke long reasoning responses.

    Updated for #12: with conv_id available, backend end_turn is primary. The
    test models a reasoning phase followed by backend confirmation (end_turn
    flips True after the thinking settles), which is how completion now works."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    d._current_conv_id = "conv-think"
    d._access_token = "tok"

    state = {"count_polls": 0, "phase2_polls": 0, "end_turn_calls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            state["phase2_polls"] += 1
            if state["phase2_polls"] > 10:
                return json.dumps(
                    {
                        "text": "the answer",
                        "md_text": "the answer",
                        "html_len": 10,
                        "child_count": 1,
                        "has_action": True,
                        "is_thinking": False,
                    }
                )
            return json.dumps(
                {
                    "text": "",
                    "md_text": "",
                    "html_len": 0,
                    "child_count": 0,
                    "has_action": False,
                    "is_thinking": True,
                }
            )
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="the answer")
    )

    # Backend says not-done during thinking, then done once the answer appears.
    async def _end_turn(cid, anchor, *, had_non_text_content=False):
        state["end_turn_calls"] += 1
        return TurnEndResult(
            status="matched" if state["phase2_polls"] > 10 else "not_ready"
        )

    d._fetch_end_turn_for_turn = _end_turn

    chunks = []
    async for chunk in d.send_and_stream("think hard", timeout=10000):
        chunks.append(chunk)
    assert any(c.delta for c in chunks)


@pytest.mark.asyncio
async def test_saw_thinking_unlocks_fallback_but_empty_end_turn_does_not_finish(monkeypatch):
    """saw_thinking=True, backend end_turn=true BUT empty content → must NOT
    finish. Completion stays strict even when the fallback is unlocked."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    d._current_conv_id = "conv-empty-think"
    d._access_token = "tok"

    state = {"count_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            return json.dumps(
                {
                    "text": "",
                    "md_text": "",
                    "html_len": 0,
                    "child_count": 0,
                    "has_action": False,
                    "is_thinking": True,
                }
            )
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(return_value=TurnTextResult(status="not_ready"))
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    # is_thinking keeps resetting the stall clock, and the strict content
    # guard prevents completing on empty. The loop runs to the deadline and
    # returns WITHOUT emitting any delta — proving the fallback didn't
    # prematurely complete on an empty end_turn.
    chunks = []
    async for chunk in d.send_and_stream("think", timeout=5):
        chunks.append(chunk)
    assert not any(c.delta for c in chunks), (
        "fallback must not complete an empty answer even with end_turn=true"
    )


@pytest.mark.asyncio
async def test_saw_thinking_with_end_turn_and_content_finishes(monkeypatch):
    """saw_thinking=True, backend end_turn=true WITH usable text → finishes.
    The rescue path for long reasoning: DOM action slow, backend confirms done."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    d._current_conv_id = "conv-rescue"
    d._access_token = "tok"

    state = {"count_polls": 0}

    async def _fake_js(expr, timeout=15):
        if "has_action" in expr:
            return json.dumps(
                {
                    "text": "the full answer",
                    "md_text": "the full answer",
                    "html_len": 10,
                    "child_count": 1,
                    "has_action": False,
                    "is_thinking": False,
                }
            )
        if "body.innerText" in expr:
            return json.dumps({"text": "normal page"})
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"

    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="the full answer")
    )
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    chunks = []
    async for chunk in d.send_and_stream("think then answer", timeout=10000):
        chunks.append(chunk)
    assert any(c.delta for c in chunks)


# ── 10. MCP mapping ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_auth_expired_returns_error_result():
    """do_chat_completion raises AuthExpiredError when send_and_stream raises it.
    The call_tool handler catches it → CallToolResult(isError=True); we verify
    the do_ function propagates the exception (the catch lives in call_tool's
    closure, tested via the exception class contract)."""
    from chatgpt_web2api import mcp_server

    drv = MagicMock(spec=CDPDriver)
    drv._current_conv_id = None
    drv._current_model = None
    drv.is_connected = True
    drv.select_model = AsyncMock(return_value=True)
    drv.navigate_new_chat = AsyncMock()

    # send_and_stream must be an async GENERATOR that raises on iteration.
    async def _raising_stream(text, timeout=120):
        raise AuthExpiredError()
        yield  # unreachable, makes this a generator

    drv.send_and_stream = _raising_stream

    with pytest.raises(AuthExpiredError):
        await mcp_server.do_chat_completion(drv, {"message": "hi"}, None)


# ── 9. HTTP mapping ────────────────────────────────────────────


def test_http_error_response_auth_expired_is_401():
    from unittest.mock import MagicMock

    from chatgpt_web2api.api_server import APIServer

    srv = APIServer.__new__(APIServer)  # bypass __init__
    srv._driver = MagicMock()
    resp = srv._error_response(AuthExpiredError())
    assert resp.status == 401


def test_http_error_response_generation_stuck_is_504():
    from unittest.mock import MagicMock

    from chatgpt_web2api.api_server import APIServer

    srv = APIServer.__new__(APIServer)
    srv._driver = MagicMock()
    resp = srv._error_response(GenerationStuckError("phase_2_stream", 47.3))
    assert resp.status == 504

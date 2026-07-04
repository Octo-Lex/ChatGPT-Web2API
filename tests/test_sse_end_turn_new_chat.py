"""Regression tests for issue #10: SSE chat_completion completion deadlock.

Root cause: on a NEW chat (SSE/MCP path, or first send on a fresh REST
session) ``_current_conv_id`` is None, so ``conv_id_for_check`` starts as ""
inside the completion poll loop. That disabled the backend ``end_turn``
fallback (its guard is ``conv_id_for_check and ...``), leaving only the
brittle DOM action-button as a completion signal. When that selector
drifted, the loop deadlocked until the 120s deadline.

The fix resolves ``conv_id_for_check`` mid-loop from the live URL, and stops
letting a stale ``is_thinking`` state freeze the stall detector forever once
a backend completion signal is available.

These tests use the same mocking pattern as ``test_non_text_response.py``:
monkeypatch ``time.monotonic`` + ``asyncio.sleep`` to advance a virtual
clock, and a fake ``_js_strict`` that returns phase-appropriate JSON.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.turn_anchor import TurnEndResult, TurnTextResult


def _make_driver():
    """A driver with no live CDP connection, ready for mocked polling tests.

    ``_current_conv_id`` is deliberately None — this is the NEW-chat state
    that reproduces issue #10 (REST keeps it populated across requests;
    SSE/MCP starts each fresh send without it).
    """
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    d._access_token = "tok"
    d._token_fetched_at = time.time()
    return d


def _install_virtual_clock(monkeypatch, start=0.0):
    """Return a controllable virtual clock + a sleep that advances it."""
    t = [start]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)
    return t


def _phase1_then_phase2_js(
    state, *, phase1_turn_after=2, phase2_factory=None, url="https://chatgpt.com/c/resolved-conv-id"
):
    """Build a fake _js_strict distinguishing Phase-1 (appear) from Phase-2
    (completion poll) calls by expression content.

    phase2_factory(n) -> dict  produces the Phase-2 JSON for poll n.
    """

    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:  # rate-limit scan
            return json.dumps({"text": "normal"})
        if "has_action" in expr:  # Phase-2 completion poll
            state["phase2"] += 1
            return json.dumps(phase2_factory(state["phase2"]))
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1  # Phase-1 node-count poll
            return "1" if state["phase1"] >= phase1_turn_after else "0"
        if "location.href" in expr:  # URL probe for conv_id
            return url
        return ""

    return _fake_js


# ── 1. New chat starts with conv_id_for_check == "" ─────────────────────


@pytest.mark.asyncio
async def test_new_chat_starts_with_empty_conv_id_for_check():
    """A fresh driver (no _current_conv_id) must begin polling with
    conv_id_for_check == "" — this is the issue #10 precondition. The
    best-effort resolver must be what fills it, not the initial assignment.
    """
    d = _make_driver()
    assert d._current_conv_id is None
    # The best-effort resolver returns "" before any URL is known.
    d._js_strict = AsyncMock(return_value="https://chatgpt.com/?model=auto")
    got = await d._get_live_conversation_id_best_effort()
    assert got == ""


# ── 2. URL later changes to /c/<id> ─────────────────────────────────────


@pytest.mark.asyncio
async def test_conversation_id_parsed_from_url():
    """_conversation_id_from_url parses /c/{id} out of location.href and
    returns "" for non-conversation URLs."""
    d = _make_driver()

    async def _fake_js(expr, timeout=15):
        if "location.href" in expr:
            return "https://chatgpt.com/c/abc-123-DEF?model=auto"
        return ""

    d._js_strict = _fake_js
    assert await d._conversation_id_from_url() == "abc-123-DEF"

    async def _fake_js2(expr, timeout=15):
        if "location.href" in expr:
            return "https://chatgpt.com/?model=auto"  # not a /c/ URL yet
        return ""

    d._js_strict = _fake_js2
    assert await d._conversation_id_from_url() == ""


# ── 3. Loop resolves conv_id_for_check mid-loop ─────────────────────────


@pytest.mark.asyncio
async def test_loop_resolves_conv_id_mid_loop(monkeypatch):
    """The completion poll loop must resolve conv_id_for_check from the URL
    when it starts empty. We prove this by making completion depend ONLY on
    the backend end_turn fallback (has_action never becomes true), which can
    only fire once conv_id_for_check is non-empty. If the mid-loop resolution
    is missing, this test never completes within the virtual timeout."""
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        # Text streams so there's usable content; has_action NEVER true
        # (simulates selector drift). Completion must come from end_turn.
        text = "Hello world. " * (1 if n > 2 else 0)
        return {
            "text": text,
            "md_text": text,
            "html_len": len(text) + 20,
            "child_count": 1,
            "has_action": False,
            "is_thinking": False,
        }

    d._js_strict = _phase1_then_phase2_js(state, phase2_factory=phase2)
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    # A2: DOM streamed "Hello world. " (last_dom_text); return matched with the
    # same text so reconciliation breaks cleanly (len == last_dom_text → no delta).
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="Hello world. ")
    )

    # end_turn becomes matched once polled. Mocked to record the conv_id it saw.
    seen_conv_ids = []

    async def _fake_end_turn(conv_id, anchor, *, had_non_text_content=False):
        seen_conv_ids.append(conv_id)
        return TurnEndResult(status="matched")

    d._fetch_end_turn_for_turn = _fake_end_turn

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    # Backend fallback fired AND it saw the resolved conv_id (not "").
    assert seen_conv_ids, "end_turn fallback never ran — conv_id not resolved"
    assert seen_conv_ids[0] == "resolved-conv-id"
    assert chunks[-1].finish_reason == "stop"


# ── 4. Backend end_turn=True with usable text completes ────────────────


@pytest.mark.asyncio
async def test_backend_end_turn_completes_with_text(monkeypatch):
    """When has_action stays false (selector drift) but the backend reports
    end_turn=True and there is streamed text, the loop completes. This is the
    core fix: the stable backend signal must be able to complete a new-chat
    generation that the DOM signal cannot."""
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        text = "Final answer." if n > 2 else ""
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
    # A2: DOM streamed "Final answer." (last_dom_text); return matched with
    # same text so reconciliation breaks cleanly (no spurious delta).
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="Final answer.")
    )
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    deltas = [c.delta for c in chunks if c.delta]
    assert any("Final answer" in c for c in deltas), f"deltas: {deltas}"
    assert chunks[-1].finish_reason == "stop"
    assert d._fetch_end_turn_for_turn.await_count >= 1


# ── 5. is_thinking=True does not prevent backend completion ────────────


@pytest.mark.asyncio
async def test_is_thinking_does_not_block_backend_completion(monkeypatch):
    """The issue #10 deadlock: is_thinking=True froze the stall detector for
    120s while the DOM never showed progress. With conv_id resolved mid-loop,
    a stale thinking state must NOT prevent the backend end_turn signal from
    completing the generation. is_thinking stays True the whole time here;
    completion must still succeed via the backend."""
    d = _make_driver()
    _install_virtual_clock(monkeypatch)
    state = {"phase1": 0, "phase2": 0}

    def phase2(n):
        # is_thinking pinned True the entire poll (stale .result-thinking),
        # zero DOM text progress, has_action never true — the exact deadlock
        # signature from the instrumented reproduction.
        text = "Answer." if n > 2 else ""
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
    # A2: DOM streamed "Answer." (last_dom_text); return matched with same text.
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="Answer.")
    )
    d._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    # Completed despite is_thinking=True the entire time.
    deltas = [c.delta for c in chunks if c.delta]
    assert any("Answer" in c for c in deltas), f"deltas: {deltas}"
    assert chunks[-1].finish_reason == "stop"

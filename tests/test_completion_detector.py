"""Wiring tests for CompletionDetector (Phase 5 PR4 extraction).

These verify the extraction moved the Phase-1 (assistant-node appear) and
Phase-2 (DOM stream + completion-detection) loop bodies onto
CompletionDetector.stream_until_complete, and that the detector reaches the
driver's transport / backend / conversation-id through the correct seam. They
are NOT behavioral tests — completion behavior is already covered by
test_end_turn_primary / test_reliability / test_rate_limit and the broad suite,
which stub the driver and confirm the driver-facing surface is preserved. This
file guards the wiring:

  - CompletionDetector exists and exposes stream_until_complete (delta-only
    async sub-generator).
  - CDPDriver wires self._completion = CompletionDetector(self) in __init__.
  - The detector holds no long-lived config beyond _driver (the two per-call
    result attrs are transient, reset each call).
  - The detector routes transport/backend/conv-id through self._driver, NOT
    local copies — so driver-side monkeypatches still intercept.
  - is_rate_limited_text / PHASE_STALL_SECONDS stay importable from cdp_driver
    (back-compat for api_server / chatgpt_dom / tests).
  - No cdp_driver import at completion_detector module load (circular-import
    rule); error classes / StreamChunk are imported lazily inside the method.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.completion_detector import (
    PHASE_STALL_SECONDS,
    CompletionDetector,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnEndResult


def _make_detector():
    """A CompletionDetector backed by a mock driver with the seam it reaches
    through. JS/backend defaults to AsyncMocks so individual tests override
    only what they assert on."""
    driver = MagicMock()
    driver._current_conv_id = None
    driver._js_strict = AsyncMock(return_value="")
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="not_ready"))
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")
    return CompletionDetector(driver), driver


# ── 1. CompletionDetector exposes the sub-generator ───────────────────


def test_detector_has_stream_until_complete():
    """The single extracted method exists and is an async generator function
    (it yields delta chunks)."""
    detector, _ = _make_detector()
    assert callable(getattr(detector, "stream_until_complete", None)), (
        "CompletionDetector must expose stream_until_complete"
    )
    assert inspect.isasyncgenfunction(detector.stream_until_complete), (
        "stream_until_complete must be an async generator (it yields StreamChunks)"
    )


def test_stream_until_complete_is_keyword_only():
    """The driver shell calls it with initial_count= / timeout= by keyword."""
    sig = inspect.signature(CompletionDetector.stream_until_complete)
    for name in ("initial_count", "timeout"):
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must be keyword-only"
        )


# ── 2. CDPDriver wires _completion ────────────────────────────────────


def test_driver_wires_completion():
    """CDPDriver.__init__ must construct the detector with itself."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    assert isinstance(d._completion, CompletionDetector), (
        "CDPDriver must wire self._completion = CompletionDetector(self)"
    )
    assert d._completion._driver is d, "detector's _driver must be the owner"


# ── 3. Per-call results, no long-lived config across calls ────────────


def test_detector_has_only_driver_and_transient_results():
    """The detector holds _driver plus two transient per-call result attrs
    (last_dom_text / had_non_text_content). No long-lived config migrates in."""
    detector, _ = _make_detector()
    own = vars(detector)
    assert set(own) == {"_driver", "last_dom_text", "had_non_text_content"}, (
        f"unexpected instance state on CompletionDetector: {set(own)}"
    )


def test_per_call_results_reset_on_each_call():
    """last_dom_text / had_non_text_content are reset at the start of each
    stream_until_complete call (no state leaks between calls)."""
    detector, driver = _make_detector()
    # Pollute them; the first thing the method does is reset both to defaults.
    detector.last_dom_text = "stale"
    detector.had_non_text_content = True

    # The detector calls _js_strict with several distinct JS expressions:
    #   - the rate-limit body scan  -> JSON {"text": ...}
    #   - the assistant node count  -> integer-as-string
    #   - the Phase-2 poll           -> JSON {text, md_text, html_len, ...}
    # Discriminate by expression so each returns the right shape. The poll
    # carries text so the backend end_turn completion guard (which requires
    # last_dom_text OR had_non_text_content) can fire and end the loop fast.
    scan = '{"text": ""}'
    poll = '{"text":"hi","md_text":"hi","html_len":60,"child_count":1,"has_action":false,"is_thinking":false}'

    async def fake_js(expr):
        if "getBoundingClientRect" in expr:  # Phase-2 completion poll
            return poll
        if "innerText" in expr:  # Phase-1 rate-limit body scan
            return scan
        return "1"  # assistant-node count poll

    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    # Provide a conv_id so the backend end_turn primary signal is eligible
    # (guard requires conv_id_for_check); without it the loop has no
    # completion path and would run to the timeout.
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-1")

    import asyncio

    async def drain():
        async for _ in detector.stream_until_complete(
            initial_count=0, timeout=5,
            turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
        ):
            pass

    asyncio.run(drain())
    # Reset happened: the "stale" value did not survive the call boundary. With
    # the poll carrying text="hi", last_dom_text reflects THIS call ("hi").
    assert detector.last_dom_text == "hi"
    assert detector.last_dom_text != "stale"


# ── 4. Back-compat re-exports from cdp_driver ─────────────────────────


def test_is_rate_limited_text_reexported_identity():
    """is_rate_limited_text must remain importable from cdp_driver, and it must
    be the SAME object (identity) as the one in completion_detector."""
    from chatgpt_web2api.cdp_driver import is_rate_limited_text as drv_fn
    from chatgpt_web2api.completion_detector import is_rate_limited_text as det_fn

    assert drv_fn is det_fn, "is_rate_limited_text must be re-exported by identity"
    assert drv_fn("Too many requests") is True
    assert drv_fn("normal chat answer") is False


def test_phase_stall_seconds_reexported_equal():
    """PHASE_STALL_SECONDS must remain importable from cdp_driver with the same
    value (equality, not identity — ints are not guaranteed interned)."""
    from chatgpt_web2api.cdp_driver import PHASE_STALL_SECONDS as drv_val

    assert drv_val == PHASE_STALL_SECONDS == 90


# ── 5. Detector routes through self._driver (the seam) ───────────────


@pytest.mark.asyncio
async def test_detector_routes_js_through_driver():
    """The detector must call self._driver._js_strict, NOT a local copy — so
    driver-side monkeypatches (used across the test suite) still intercept."""
    detector, driver = _make_detector()
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    # Discriminate by JS expression: body scan -> JSON, count -> "1" (exceeds
    # initial_count=0 so Phase-1 breaks), poll -> completed payload WITH text so
    # the backend end_turn strict-content guard can fire and end the loop.
    scan = '{"text": ""}'
    poll = '{"text":"hi","md_text":"hi","html_len":60,"child_count":1,"has_action":false,"is_thinking":false}'
    js_calls = {"n": 0}

    async def fake_js(expr):
        js_calls["n"] += 1  # proof the detector reached transport via the driver
        if "getBoundingClientRect" in expr:  # Phase-2 poll (also contains innerText)
            return poll
        if "innerText" in expr:  # Phase-1 body scan
            return scan
        return "1"

    driver._js_strict = fake_js
    driver._fetch_end_turn_for_turn = AsyncMock(return_value=TurnEndResult(status="matched"))
    # Provide conv_id so the backend end_turn primary signal is eligible and
    # the loop completes (otherwise no completion path → runs to timeout).
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-1")
    seen = []
    async for chunk in detector.stream_until_complete(
        initial_count=0, timeout=10000,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
    ):
        seen.append(chunk)
    assert js_calls["n"] >= 1, "detector must reach transport via _driver._js_strict"
    assert driver._fetch_end_turn_for_turn.await_count >= 1, (
        "detector must reach backend via _driver._fetch_end_turn_for_turn"
    )


# ── 6. No cdp_driver import at completion_detector module load ────────


def test_no_cdp_driver_import_at_module_load():
    """Circular-import rule: completion_detector must NOT import anything from
    cdp_driver at module top level (cdp_driver top-level re-exports symbols FROM
    completion_detector). Error classes / StreamChunk are imported lazily inside
    the method body. Verify by inspecting the module's source for a top-level
    cdp_driver import."""
    src = inspect.getsource(importlib_import_module("chatgpt_web2api.completion_detector"))
    # A `from .cdp_driver import` at column 0 (module level, not inside a def)
    # would be a circular-import violation. The lazy import is indented inside
    # stream_until_complete, so it does not start at column 0.
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("from .cdp_driver import") or stripped.startswith(
            "from chatgpt_web2api.cdp_driver import"
        ):
            assert line.startswith(" "), (
                f"cdp_driver import must be lazy (indented inside a method), "
                f"not top-level: {line!r}"
            )


# importlib shim imported lazily so the test file's own imports stay clean
def importlib_import_module(name):
    import importlib

    return importlib.import_module(name)

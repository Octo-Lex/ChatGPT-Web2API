"""Response text integrity regression tests from the PR #57 live REST canary.

A finished turn is not a valid HTTP-200 response when optimistic DOM
deltas and the exact-turn backend text disagree. Earlier code used length-
only suffixes and could return a clipped answer with role UI chrome.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, StreamChunk
from chatgpt_web2api.completion_detector import CompletionDetector
from chatgpt_web2api.dom_turn_signals import ASSISTANT_TURN_COUNT_JS
from chatgpt_web2api.turn_anchor import (
    TurnAnchor,
    TurnEndResult,
    TurnReconciliationError,
    TurnTextResult,
)


def _driver_with_texts(dom_text: str, canonical_text: str) -> CDPDriver:
    d = CDPDriver(cdp_port=9222)
    d._assert_owned_tab_required = MagicMock()
    d._read_assistant_count_baseline = AsyncMock(return_value=0)
    d._identity_listener = None
    d._capture_pre_send_fallback_anchor = AsyncMock(
        return_value=TurnAnchor(sent_text="one send", mode="fresh_chat")
    )
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._verify_send_acknowledged = AsyncMock(return_value=None)
    d._js_strict = AsyncMock(return_value="https://chatgpt.com/c/conv-integrity")
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text=canonical_text)
    )

    async def fake_stream(**kwargs):
        if dom_text:
            yield StreamChunk(delta=dom_text)

    d._completion = MagicMock()
    d._completion.stream_until_complete = fake_stream
    d._completion.last_dom_text = dom_text
    d._completion.had_non_text_content = False
    return d


@pytest.mark.asyncio
@pytest.mark.parametrize("dom_text,canonical", [
    ("", "PR57-LIVE-dd2290aa"),
    ("PR57-LIVE-", "PR57-LIVE-dd2290aa"),
    ("PR57-LIVE-dd2290aa", "PR57-LIVE-dd2290aa"),
])
async def test_final_backend_reconciliation_emits_exact_prefix_suffix(
    dom_text, canonical
):
    d = _driver_with_texts(dom_text, canonical)
    chunks = [c async for c in d.send_and_stream("one send", timeout=5)]

    assert "".join(c.delta for c in chunks) == canonical
    assert chunks[-1].finish_reason == "stop"
    d.click_send.assert_awaited_once()
    d._fetch_text_for_turn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("untrusted_dom", [
    "ChatGPT said:",
    "ChatGPT said:290aa",
    "PR57-LIVE-xx",  # same-ish content but NOT an actual prefix
    "PR57-LIVE-dd2290ab",  # equal-length mismatch: old code silently 200
])
async def test_nonprefix_dom_text_fails_closed_not_http_200(untrusted_dom):
    canonical = "PR57-LIVE-dd2290aa"
    d = _driver_with_texts(untrusted_dom, canonical)

    with pytest.raises(TurnReconciliationError, match="text_prefix_mismatch") as caught:
        _ = [c async for c in d.send_and_stream("one send", timeout=5)]

    assert caught.value.diagnostic["dom_text_length"] == len(untrusted_dom)
    assert caught.value.diagnostic["backend_text_length"] == len(canonical)
    assert 0 <= caught.value.diagnostic["first_mismatch_offset"] <= len(untrusted_dom)
    assert len(caught.value.diagnostic["dom_sha256_12"]) == 12
    assert len(caught.value.diagnostic["backend_sha256_12"]) == 12
    # The observation reconciliation must NOT resend the user message.
    d.click_send.assert_awaited_once()



@pytest.mark.asyncio
async def test_phase2_ignores_nonprefix_hydration_rewrite(monkeypatch):
    """The old length-only logic would append unrelated UI text or reset its
    baseline, causing a garbled 200 even if anchored reconciliation was exact.
    Keep the actual emitted prefix intact across a DOM text shape rewrite.
    """
    clock = [10.0]

    async def fast_sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(
        "chatgpt_web2api.completion_detector.time.monotonic",
        lambda: clock[0],
    )
    monkeypatch.setattr(
        "chatgpt_web2api.completion_detector.asyncio.sleep", fast_sleep,
    )
    d = MagicMock()
    d._current_conv_id = "conv-integrity"
    d._pre_send_user_count = 0
    d._fetch_end_turn_for_turn = AsyncMock(
        side_effect=[
            TurnEndResult(status="not_ready"),
            TurnEndResult(status="matched"),
        ]
    )
    samples = [0]

    async def evaluate(script, timeout=15):
        if "body.innerText" in script:
            return '{"text": ""}'
        if script == ASSISTANT_TURN_COUNT_JS:
            return "1"  # Legacy assistant role appeared
        if "has_action" in script:
            samples[0] += 1
            text = (
                "PR57-LIVE-"
                if samples[0] == 1
                else "unrelated UI hydration snapshot with more characters"
            )
            return json.dumps({
                "text": text, "md_text": text, "html_len": 60,
                "child_count": 1, "has_action": False,
                "copy_delta": False, "stop_present": False,
                "is_thinking": False,
            })
        raise AssertionError(script)

    d._js_strict = evaluate
    detector = CompletionDetector(d)
    chunks = [
        chunk.delta
        async for chunk in detector.stream_until_complete(
            initial_count=0,
            timeout=20,
            turn_anchor=TurnAnchor(sent_text="one send", mode="fresh_chat"),
        )
    ]
    assert samples[0] >= 2
    assert chunks == ["PR57-LIVE-"]
    assert detector.last_dom_text == "PR57-LIVE-"
    assert d._fetch_end_turn_for_turn.await_count == 2



@pytest.mark.asyncio
async def test_whitespace_only_difference_stays_fail_closed_and_private(caplog):
    """Literal REST equality is mandatory: strip-equality is diagnostic only.

    Logs may include lengths, first differing offset, and short hashes,
    never the actual assistant answer (even under mismatch).
    """
    import logging

    answer = "CANARY-PRIVATE-DO-NOT-LOG"
    d = _driver_with_texts("  " + answer, answer)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(TurnReconciliationError) as raised:
            _ = [c async for c in d.send_and_stream("one send", timeout=5)]

    assert raised.value.last_status == "text_prefix_mismatch"
    assert raised.value.diagnostic["whitespace_only"] is True
    assert answer not in caplog.text
    assert "whitespace_only=True" in caplog.text
    d.click_send.assert_awaited_once()



@pytest.mark.asyncio
async def test_r1_no_conversation_id_never_finishes_http_200(monkeypatch):
    """Zero DOM/backend ID and no /c/ URL cannot be a successful empty REST."""
    d = _driver_with_texts("", "PR57-LIVE-exact")
    d._completion.last_anchored_conv_id = ""
    d._js_strict = AsyncMock(return_value="https://chatgpt.com/")

    from chatgpt_web2api import cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    with pytest.raises(
        TurnReconciliationError, match="conversation_id_unavailable"
    ) as caught:
        _ = [c async for c in d.send_and_stream("one send", timeout=5)]
    assert caught.value.last_status == "conversation_id_unavailable"
    d._fetch_text_for_turn.assert_not_awaited()
    d.click_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_r1_anchored_midloop_id_rescues_missing_final_url():
    """Only a matched per-turn backend end_turn can supply a fallback ID."""
    sentinel = "PR57-LIVE-exact"
    d = _driver_with_texts("", sentinel)
    d._completion.last_anchored_conv_id = "conv-integrity"
    d._js_strict = AsyncMock(return_value="https://chatgpt.com/")
    chunks = [c async for c in d.send_and_stream("one send", timeout=5)]
    assert "".join(c.delta for c in chunks) == sentinel
    assert chunks[-1].finish_reason == "stop"
    assert d._current_conv_id == "conv-integrity"
    d._fetch_text_for_turn.assert_awaited_once()
    d.click_send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_url,anchor_mode,anchor_conversation,expected_status", [
    ("https://chatgpt.com/c/other-conv", "fresh_chat", None,
     "conversation_id_mismatch"),
    ("https://chatgpt.com/", "existing_conversation", "other-conv",
     "conversation_id_anchor_mismatch"),
])
async def test_r1_trusted_id_refuses_foreign_url_or_anchor(
    visible_url, anchor_mode, anchor_conversation, expected_status,
):
    d = _driver_with_texts("", "PR57-LIVE-exact")
    d._completion.last_anchored_conv_id = "conv-integrity"
    d._capture_pre_send_fallback_anchor = AsyncMock(
        return_value=TurnAnchor(
            sent_text="one send",
            mode=anchor_mode,
            conversation_id_at_capture=anchor_conversation,
        )
    )
    d._js_strict = AsyncMock(return_value=visible_url)
    with pytest.raises(TurnReconciliationError) as caught:
        _ = [c async for c in d.send_and_stream("one send", timeout=5)]
    assert caught.value.last_status == expected_status
    d._fetch_text_for_turn.assert_not_awaited()
    d.click_send.assert_awaited_once()

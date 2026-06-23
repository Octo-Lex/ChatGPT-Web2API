"""Tests for broadened Phase-2 non-text response handling.

Verifies that image/tool-use/non-text responses don't falsely trigger the
stall detector, that text responses stream correctly (no regression), and
that the placeholder message surfaces when non-text content is detected
but _fetch_text returns empty.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import (
    CDPDriver,
)


def _make_driver():
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()
    d._access_token = "tok"
    d._token_fetched_at = time.time()
    return d


# ── 1. Image-like DOM: html_len grows, text stays empty → no stall ────

@pytest.mark.asyncio
async def test_image_response_does_not_stall(monkeypatch):
    """Image generation: html_len grows (img/canvas elements added) but text
    stays empty. The stall clock must reset on html_len change so the
    generation is allowed the full timeout, then breaks on done."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])
    async def fast_sleep(s):
        t[0] += s
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1_polls": 0, "phase2_polls": 0}
    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            # Phase 2 poll (now has html_len — distinguishes from Phase 1)
            state["phase2_polls"] += 1
            n = state["phase2_polls"]
            html_len = 10 + n * 50  # grows each poll (image rendering)
            # has_action: the per-turn action button appears only on a finished
            # message. False while the image renders, True after ~50s of
            # progress. This is the completion signal the driver now uses.
            has_action = n > 100
            return json.dumps({"text": "", "html_len": html_len, "child_count": 1, "has_action": has_action})
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            # Phase 1 count poll
            state["phase1_polls"] += 1
            return "1" if state["phase1_polls"] > 1 else "0"
        if "location.href" in expr:
            return "https://chatgpt.com/c/test-image-conv"
        return ""
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text = AsyncMock(return_value="")  # no text from API

    chunks = []
    async for chunk in d.send_and_stream("generate an image", timeout=10000):
        chunks.append(chunk)

    # Did NOT raise GenerationStuckError — image generation completed.
    # Placeholder message should be present since non-text content was detected.
    text_chunks = [c.delta for c in chunks if c.delta]
    assert any("Non-text response" in c for c in text_chunks), f"chunks: {text_chunks}"
    assert chunks[-1].finish_reason == "stop"


# ── 2. Text DOM: streams delta as before, no regression ───────────────

@pytest.mark.asyncio
async def test_text_response_streams_delta_unchanged(monkeypatch):
    """Normal text response: .markdown text grows each poll. Must stream
    deltas exactly as before (no regression from the broadened signals)."""
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
            state["text"] += "Hello world. "  # text grows
            has_action = state["phase2"] > 5
            return json.dumps({
                "text": state["text"],
                "html_len": len(state["text"]) + 20,
                "child_count": 1,
                "has_action": has_action,
            })
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1
            return "1" if state["phase1"] > 1 else "0"
        return ""
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text = AsyncMock(return_value="")

    chunks = []
    async for chunk in d.send_and_stream("hello", timeout=10000):
        chunks.append(chunk)

    # Verify deltas were streamed (not empty)
    deltas = [c.delta for c in chunks if c.delta]
    assert len(deltas) > 0, f"no deltas streamed: {chunks}"
    # No placeholder (text was captured)
    assert not any("Non-text response" in c for c in deltas)
    assert chunks[-1].finish_reason == "stop"


# ── 3. _fetch_text empty + non-text content → placeholder ────────────

@pytest.mark.asyncio
async def test_placeholder_on_empty_fetch_with_non_text_content(monkeypatch):
    """When Phase-2 detects non-text content (html_len > 50) but _fetch_text
    returns empty, the reconcile should yield a placeholder message."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])
    async def fast_sleep(s):
        t[0] += s
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1": 0, "phase2": 0}
    async def _fake_js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        if "has_action" in expr:
            state["phase2"] += 1
            has_action = state["phase2"] > 3
            return json.dumps({
                "text": "",
                "html_len": 200,  # non-text content present
                "child_count": 2,
                "has_action": has_action,
            })
        if ".length" in expr and "querySelectorAll" in expr and "JSON.stringify" not in expr:
            state["phase1"] += 1
            return "1" if state["phase1"] > 1 else "0"
        if "location.href" in expr:
            return "https://chatgpt.com/c/test-conv-123"
        return ""
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text = AsyncMock(return_value="")  # API returns no text

    chunks = []
    async for chunk in d.send_and_stream("generate image", timeout=10000):
        chunks.append(chunk)

    # Placeholder should be present
    text_chunks = [c.delta for c in chunks if c.delta]
    assert any("Non-text response" in c for c in text_chunks), f"no placeholder: {text_chunks}"

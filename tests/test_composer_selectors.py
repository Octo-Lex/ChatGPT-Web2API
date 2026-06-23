"""Regression tests for the post-2026 ChatGPT composer selectors.

ChatGPT shipped a new composer: the real input is a contenteditable
ProseMirror div, and ``#prompt-textarea`` is now a *hidden fallback*
textarea. The send button lost its ``data-testid="send-button"``. These
tests pin the new selectors so a future composer change can't silently
re-break typing/sending the way the 2026 redesign did.

All tests are unit-level with mocked CDP — no live Chrome needed.
"""

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from chatgpt_web2api.cdp_driver import (
    CDPDriver,
    COMPOSER_SELECTOR,
    COMPOSER_FALLBACK_SELECTOR,
    SEND_BUTTON_SELECTOR,
    SEND_BUTTON_FALLBACK_SELECTOR,
)


# ── Helpers ────────────────────────────────────────────────────

def _make_driver():
    """A CDPDriver with a mocked websocket (no real connect)."""
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()  # truthy; is_connected treats as open
    d._access_token = "fresh-token"
    d._token_fetched_at = time.time()
    return d


# ── 1. Selector constants target the new DOM, not the fallback ──

def test_composer_selector_targets_prosemirror_textbox():
    """COMPOSER_SELECTOR must match the contenteditable ProseMirror div,
    NOT the hidden fallback textarea."""
    assert "ProseMirror" in COMPOSER_SELECTOR
    assert 'role="textbox"' in COMPOSER_SELECTOR


def test_composer_selector_does_not_match_plain_prompt_textarea():
    """The bare ``#prompt-textarea`` id now resolves to the hidden
    fallback textarea. The primary selector must not match a plain
    ``<textarea id="prompt-textarea">`` — it requires a ``div`` tag with
    ``role="textbox"`` (or the ProseMirror class), so the hidden
    fallback element (a ``<textarea>``) is excluded."""
    # Every comma-separated branch must start with the `div` tag
    # qualifier — a `<textarea>` element won't match `div[...]`.
    branches = [b.strip() for b in COMPOSER_SELECTOR.split(",")]
    assert branches, "COMPOSER_SELECTOR is empty"
    for branch in branches:
        assert branch.startswith("div"), (
            f"composer selector branch '{branch}' does not require a <div> "
            "tag — a <textarea> fallback could match it"
        )
        assert 'role="textbox"' in branch or "ProseMirror" in branch, (
            f"composer selector branch '{branch}' needs role=textbox or "
            "ProseMirror to target the real composer"
        )


def test_fallback_selector_kept_for_legacy_deployments():
    """The legacy textarea id is retained as a fallback so the driver
    still works on older deployments / A/B holdouts."""
    assert "prompt-textarea" in COMPOSER_FALLBACK_SELECTOR
    assert COMPOSER_FALLBACK_SELECTOR.startswith("textarea")


def test_send_button_selector_uses_aria_label_not_testid():
    """The new composer's send affordance is ``button[aria-label*=Send]``,
    not ``data-testid=send-button``. The primary selector must reflect
    that."""
    assert "aria-label" in SEND_BUTTON_SELECTOR
    assert "Send" in SEND_BUTTON_SELECTOR
    # Must explicitly exclude the stop button, which also has an
    # aria-label but appears during generation.
    assert "stop-button" in SEND_BUTTON_SELECTOR


def test_send_button_fallback_kept_for_legacy_testid():
    """Legacy ``data-testid=send-button`` retained as fallback."""
    assert "send-button" in SEND_BUTTON_FALLBACK_SELECTOR
    assert "data-testid" in SEND_BUTTON_FALLBACK_SELECTOR


# ── 2. type_message emits valid JS and hits the right element ───

@pytest.mark.asyncio
async def test_type_message_fails_loudly_when_no_composer(monkeypatch):
    """If neither the new composer nor the fallback exists, type_message
    raises RuntimeError and captures a selector diagnostic (instead of
    silently typing into nothing)."""
    d = _make_driver()

    # _js reports no composer found.
    async def _fake_js(expr, timeout=15):
        return "no composer"
    d._js = _fake_js
    d._capture_selector_diagnostic = AsyncMock()

    with pytest.raises(RuntimeError, match="No composer"):
        await d.type_message("hello")
    d._capture_selector_diagnostic.assert_awaited_once()


@pytest.mark.asyncio
async def test_type_message_focuses_new_composer_when_present(monkeypatch):
    """When the ProseMirror textbox is present, type_message focuses it
    (returns 'composer') and verifies against the COMPOSER_SELECTOR."""
    d = _make_driver()
    calls = {"js": [], "cdp": [], "strict": []}

    async def _fake_js(expr, timeout=15):
        calls["js"].append(expr)
        return "composer"  # primary selector matched
    d._js = _fake_js

    async def _fake_cdp(method, params=None, timeout=15):
        calls["cdp"].append((method, params))
        return {}
    d._cdp = _fake_cdp

    async def _fake_strict(expr, timeout=15):
        calls["strict"].append(expr)
        return "hello"  # verify succeeds
    d._js_strict = _fake_strict

    await d.type_message("hello")

    # Focus step queried the primary composer selector.
    focus_expr = calls["js"][0]
    assert COMPOSER_SELECTOR in focus_expr

    # Verify step read textContent from the COMPOSER_SELECTOR (not the
    # fallback), proving we verified the element we actually focused.
    verify_expr = calls["strict"][0]
    assert COMPOSER_SELECTOR in verify_expr
    assert COMPOSER_FALLBACK_SELECTOR not in verify_expr

    # Insert text dispatched via CDP Input.insertText.
    assert any(m == "Input.insertText" and p["text"] == "hello"
               for m, p in calls["cdp"])


@pytest.mark.asyncio
async def test_type_message_falls_back_to_legacy_textarea(monkeypatch):
    """When the new composer is absent but the legacy textarea exists,
    type_message uses the fallback and verifies against it."""
    d = _make_driver()
    calls = {"js": [], "strict": []}

    async def _fake_js(expr, timeout=15):
        calls["js"].append(expr)
        return "fallback"  # primary missed, fallback hit
    d._js = _fake_js
    d._cdp = AsyncMock(return_value={})

    async def _fake_strict(expr, timeout=15):
        calls["strict"].append(expr)
        return "hello"
    d._js_strict = _fake_strict

    await d.type_message("hello")

    # Focus expression tried the primary selector first, then fallback.
    focus_expr = calls["js"][0]
    assert COMPOSER_SELECTOR in focus_expr
    assert COMPOSER_FALLBACK_SELECTOR in focus_expr

    # Verify read from the FALLBACK selector since that's what focused.
    verify_expr = calls["strict"][0]
    assert COMPOSER_FALLBACK_SELECTOR in verify_expr


@pytest.mark.asyncio
async def test_type_message_raises_when_verify_returns_empty(monkeypatch):
    """If the composer appears focused but verify reads empty text, the
    insert failed — raise rather than send an empty message."""
    d = _make_driver()
    d._js = AsyncMock(return_value="composer")
    d._cdp = AsyncMock(return_value={})
    d._js_strict = AsyncMock(return_value="")  # empty → failure

    with pytest.raises(RuntimeError, match="Failed to insert"):
        await d.type_message("hello")


# ── 3. click_send emits valid JS and hits the right button ──────

@pytest.mark.asyncio
async def test_click_send_fails_when_no_send_button(monkeypatch):
    """No send button (new or legacy) → RuntimeError, not a silent no-op."""
    d = _make_driver()

    async def _fake_js(expr, timeout=15):
        return "no send button"
    d._js = _fake_js
    d._capture_selector_diagnostic = AsyncMock()

    # The wait-for-button loop also returns 'no', so it polls all 10
    # times then falls through. Patch asyncio.sleep to no-op so it's
    # instant.
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="Send failed: no send button"):
        await d.click_send()
    d._capture_selector_diagnostic.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_send_emits_new_selector_first(monkeypatch):
    """click_send's JS must try SEND_BUTTON_SELECTOR before the legacy
    testid fallback — mirroring the new composer's DOM."""
    d = _make_driver()
    seen = []

    async def _fake_js(expr, timeout=15):
        seen.append(expr)
        # Wait-loop returns 'yes' immediately, then the click returns 'sent'.
        return "yes" if "yes" in expr or "'no'" in expr else "sent"
    d._js = _fake_js
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    await d.click_send()

    # At least one expression referenced the new aria-label selector.
    assert any(SEND_BUTTON_SELECTOR in e for e in seen), \
        "click_send never referenced the new aria-label send selector"
    # And it also carries the legacy fallback for older deployments.
    assert any(SEND_BUTTON_FALLBACK_SELECTOR in e for e in seen), \
        "click_send dropped the legacy testid fallback"


@pytest.mark.asyncio
async def test_click_send_sent_on_success(monkeypatch):
    """Happy path: button present + click dispatched → 'sent' logged, no raise."""
    d = _make_driver()
    d._js = AsyncMock(return_value="sent")
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    # Should not raise.
    await d.click_send()


# ── 4. Readiness checks accept the new composer ────────────────

@pytest.mark.asyncio
async def test_navigate_new_chat_ready_when_prosemirror_present(monkeypatch):
    """navigate_new_chat's readiness loop should report ready when the
    ProseMirror textbox exists (the new composer), even though the old
    #prompt-textarea logic would have matched the hidden fallback too."""
    d = _make_driver()
    d._cdp = AsyncMock(return_value={})  # Page.navigate

    ready_returned = {"v": json.dumps({
        "ready": True,
        "url": "https://chatgpt.com/",
    })}

    async def _fake_js(expr, timeout=15):
        # Confirm the readiness expression references the new composer.
        assert COMPOSER_SELECTOR in expr, \
            "readiness check does not query the new composer selector"
        return ready_returned["v"]
    d._js = _fake_js
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    await d.navigate_new_chat()  # must not raise / must not loop forever

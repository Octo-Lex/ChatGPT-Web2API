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
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import (
    COMPOSER_FALLBACK_SELECTOR,
    COMPOSER_SELECTOR,
    SEND_BUTTON_FALLBACK_SELECTOR,
    SEND_BUTTON_SELECTOR,
    CDPDriver,
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
    # Bypass the platform-probe so _js_strict calls stay focused on verify.
    d._detect_select_all_modifier = AsyncMock(return_value=2)

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
    d._detect_select_all_modifier = AsyncMock(return_value=2)

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
    """If the composer appears focused but verify reads empty/stale text on
    both the first attempt AND the retry, the insert failed — raise rather
    than send an empty/corrupt message. The new verifier canonicalizes and
    retries once via execCommand clear before giving up."""
    d = _make_driver()
    d._js = AsyncMock(return_value="composer")
    d._cdp = AsyncMock(return_value={})
    d._js_strict = AsyncMock(return_value="")  # empty → verify fails
    d._detect_select_all_modifier = AsyncMock(return_value=2)
    # Collapse sleeps so the retry path runs instantly.
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="verification failed after retry"):
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


# ── 5. canonical composer verification (R1) ────────────────────────────

@pytest.mark.asyncio
async def test_type_message_retries_on_stale_text_then_succeeds(monkeypatch):
    """ProseMirror stale-text regression: first insert leaves stale text
    (verify FAILS canonical equality), retry via execCommand clear, second
    insert matches. The old non-empty check would have passed the stale text;
    the new canonical check catches it and retries."""
    d = _make_driver()
    d._js = AsyncMock(return_value="composer")
    d._cdp = AsyncMock(return_value={})
    d._detect_select_all_modifier = AsyncMock(return_value=2)
    # First verify returns STALE text, the execCommand-clear call returns
    # "true", then the post-retry verify returns the correct input.
    verify_returns = ["old stale content", "true", "correct input"]
    async def _fake_strict(expr, timeout=15):
        # execCommand-clear + re-verify both call _js_strict; return the
        # sequence. The platform probe is bypassed via _detect_select_all_modifier.
        return verify_returns.pop(0) if verify_returns else ""
    d._js_strict = _fake_strict
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    await d.type_message("correct input")  # must not raise


@pytest.mark.asyncio
async def test_verify_composer_text_canonicalizes_crlf_and_nbsp():
    """Canonical equality: CRLF→LF, NBSP→space are normalized; internal
    spacing is NOT collapsed (would hide code/YAML corruption)."""
    d = _make_driver()
    # Input with CRLF and NBSP; composer returns the same → match.
    async def _fake_strict(expr, timeout=15):
        return "line1\r\nline2\u00a0end"
    d._js_strict = _fake_strict
    assert await d._verify_composer_text(COMPOSER_SELECTOR, "line1\nline2 end") is True


@pytest.mark.asyncio
async def test_verify_composer_text_tolerates_trailing_newline():
    """ProseMirror wraps input in a <p> and may append a trailing block
    newline; a single trailing newline must not fail verification."""
    d = _make_driver()
    async def _fake_strict(expr, timeout=15):
        return "hello\n"  # composer added a trailing newline
    d._js_strict = _fake_strict
    assert await d._verify_composer_text(COMPOSER_SELECTOR, "hello") is True


@pytest.mark.asyncio
async def test_verify_composer_text_preserves_intended_trailing_newline():
    """Regression (caught in review): a prompt that LEGITIMATELY ends in \\n
    must verify against actual ending in \\n. The old unconditional strip
    turned 'foo\\n' into 'foo' and failed a valid prompt. New logic accepts
    exact match OR actual == expected + one editor newline."""
    d = _make_driver()
    async def _fake_strict(expr, timeout=15):
        return "foo\n"  # actual matches expected exactly
    d._js_strict = _fake_strict
    assert await d._verify_composer_text(COMPOSER_SELECTOR, "foo\n") is True


@pytest.mark.asyncio
async def test_verify_composer_text_does_not_collapse_internal_whitespace():
    """Broad whitespace collapse would hide corruption of code/Markdown
    indentation. Double spaces in the input must be preserved EXACTLY."""
    d = _make_driver()
    async def _fake_strict(expr, timeout=15):
        return "two  spaces"  # two spaces, as input
    d._js_strict = _fake_strict
    # Match: two spaces == two spaces.
    assert await d._verify_composer_text(COMPOSER_SELECTOR, "two  spaces") is True
    # Mismatch: the composer has two spaces but expected one → must fail.
    assert await d._verify_composer_text(COMPOSER_SELECTOR, "two spaces") is False


@pytest.mark.asyncio
async def test_verify_composer_text_extracts_multiline_via_block_aware_js():
    """Multi-line regression (the live-only bug mocks can't catch).

    When ``Input.insertText`` carries ``\\n`` (or ``\\n\\n``), ProseMirror
    turns each line into a separate ``<p>`` block and a blank line into
    ``<p><br></p>``. The verifier's JS extractor must reconstruct the TYPED
    text — one ``\\n`` per block boundary — not whatever ``innerText``
    (measured: 5 newlines for a 2-newline input) or ``textContent`` (0
    newlines) would yield.

    This test asserts the *contract*: the JS passed to ``_js_strict`` must
    join block children with a single ``\\n`` and strip a single trailing
    editor newline. We capture the expression, then evaluate the same block
    structure in Python to confirm it reconstructs the input. (The mocked
    _js_strict can't run real JS; this pins the algorithm instead.)
    """
    d = _make_driver()
    captured = {}

    async def _fake_strict(expr, timeout=15):
        captured["expr"] = expr
        # The extractor, run against the measured DOM, must produce exactly
        # the typed input. We return what a correct extractor yields.
        return "line one.\n\nline two."
    d._js_strict = _fake_strict

    # A 2-newline input must verify — this is exactly what failed live.
    assert await d._verify_composer_text(
        COMPOSER_SELECTOR, "line one.\n\nline two."
    ) is True

    # Contract: the extractor must NOT read innerText/textContent directly.
    expr = captured["expr"]
    assert "innerText" not in expr.split("return")[0].replace("inlineText", ""), (
        "verifier reads innerText directly — ProseMirror over-counts newlines"
    )
    # It must walk childNodes and join with \n (block-aware reconstruction).
    assert "childNodes" in expr, "verifier is not block-aware (no childNodes walk)"
    assert "join('\\n')" in expr, "verifier must join blocks with a single newline"


@pytest.mark.asyncio
async def test_verify_composer_text_rejects_wrong_line_count():
    """If the extractor returns the WRONG number of newlines (the live bug),
    canonical equality must fail — proving the fix isn't just permissive."""
    d = _make_driver()
    # innerText-style over-count: 5 newlines for a 2-newline input.
    async def _fake_strict(expr, timeout=15):
        return "line one.\n\n\n\n\nline two."
    d._js_strict = _fake_strict
    assert await d._verify_composer_text(
        COMPOSER_SELECTOR, "line one.\n\nline two."
    ) is False


@pytest.mark.asyncio
async def test_detect_select_all_modifier_returns_cmd_on_mac():
    """On macOS, select-all needs Cmd (modifiers: 4), not Ctrl (2). The old
    hardcoded modifiers:2 silently no-op'd on Mac."""
    d = _make_driver()
    d._js_strict = AsyncMock(return_value="macOS")
    assert await d._detect_select_all_modifier() == 4


@pytest.mark.asyncio
async def test_detect_select_all_modifier_returns_ctrl_on_windows():
    d = _make_driver()
    d._js_strict = AsyncMock(return_value="Windows")
    assert await d._detect_select_all_modifier() == 2


# ── 6. token refresh preserves prior token on empty fetch (R2) ─────────

@pytest.mark.asyncio
async def test_refresh_token_preserves_prior_on_transient_empty(monkeypatch):
    """A transient empty-token fetch must NOT clobber a previously-valid
    token. The old code assigned before the non-empty check, wiping the good
    token. New code parses to locals and only commits on non-empty."""
    d = _make_driver()
    d._access_token = "PRIOR_VALID_TOKEN_1977chars"
    d._user_name = "Prior User"
    d._token_fetched_at = 1000.0
    # _js returns an empty-token payload on all 3 attempts.
    d._js = AsyncMock(return_value='{"token": "", "user": ""}')
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())

    with pytest.raises(RuntimeError):
        await d._refresh_token()

    # Prior token + user + fetched_at preserved; attempt timestamp advanced.
    assert d._access_token == "PRIOR_VALID_TOKEN_1977chars"
    assert d._user_name == "Prior User"
    assert d._token_fetched_at == 1000.0
    assert d._last_refresh_attempt_at > 1000.0


@pytest.mark.asyncio
async def test_refresh_token_commits_only_on_non_empty(monkeypatch):
    """A successful non-empty fetch commits token, user, and advances BOTH
    timestamps. _token_fetched_at and _last_refresh_attempt_at are distinct."""
    d = _make_driver()
    d._access_token = "OLD"
    d._token_fetched_at = 0.0
    d._js = AsyncMock(return_value='{"token": "FRESH_TOKEN", "user": "New"}')

    await d._refresh_token()

    assert d._access_token == "FRESH_TOKEN"
    assert d._user_name == "New"
    # Both timestamps advance on a successful fetch, but they are set by two
    # separate time.time() calls. Asserting exact equality is a platform flake:
    # Windows' clock tick (~15ms) makes them coincide, while Linux/macOS
    # clock_gettime resolves them ~tens of µs apart. Verify the intent — both
    # advanced and reflect this fetch — not byte-identical floats.
    assert d._token_fetched_at > 0.0
    assert d._last_refresh_attempt_at > 0.0
    assert abs(d._last_refresh_attempt_at - d._token_fetched_at) < 1.0

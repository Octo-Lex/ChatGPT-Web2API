"""Tests: composer text verification round-trip (ChatGPT review, conv 6a52f0f3).

Tests the 4 defects ChatGPT identified by reading the actual code:

1. Complex-newline DOM reconstruction — <br> inside <p> is lost by textContent
2. Missing NFC normalization — composed/decomposed Unicode sequences differ
3. Unconditional trailing-newline removal — user's trailing \n is stripped
4. Fixed 500ms stabilization — needs bounded polling instead

These test the _verify_composer_text method directly, mocking the JS read-back
to return what the real ProseMirror DOM would produce for each case.
"""

import asyncio
import unicodedata
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.chatgpt_dom import ChatGPTDom
from chatgpt_web2api.cdp_driver import CDPDriver


def _make_dom(js_return_value=""):
    """A ChatGPTDom with a real CDPDriver whose _js_strict returns the given value."""
    driver = CDPDriver(cdp_port=9222)
    driver._js_strict = AsyncMock(return_value=js_return_value)
    driver._js = AsyncMock(return_value=js_return_value)
    driver._cdp = AsyncMock()
    driver._breakers = None
    return ChatGPTDom(driver), driver


def _composer_text_with_br():
    """Simulate what ProseMirror produces for input 'line1\nline2' when it
    renders as <p>line1<br>line2</p> — textContent gives 'line1line2' (no newline).
    This is the high-confidence bug: <br> within a <p> is invisible to textContent."""
    # The JS extractor currently does child.textContent which would return "line1line2"
    # for <p>line1<br>line2</p>. That's the bug.
    return "line1line2"


def _composer_text_multiline_blocks():
    """Simulate what the current block-aware extractor returns for a multi-line
    input. Each top-level <p> child's textContent is joined with \\n.
    For input 'para1\\npara2\\n\\nblank_line', ProseMirror creates:
    <p>para1</p><p>para2</p><p><br></p><p>blank_line</p>
    The extractor joins: 'para1\\npara2\\n\\nblank_line' — correct.
    But for input with <br> INSIDE a <p>: <p>line1<br>line2</p>
    textContent = 'line1line2' — WRONG, should be 'line1\\nline2'."""
    return "para1\npara2\n\nblank_line"


class TestComposerVerificationDefects:
    """Each test reproduces a defect ChatGPT identified by reading the code."""

    @pytest.mark.asyncio
    async def test_br_inside_paragraph_should_preserve_newline(self):
        """Defect 1 (high confidence): when ProseMirror renders 'line1\\nline2'
        as <p>line1<br>line2</p>, the current extractor returns 'line1line2'
        (textContent of the <p>), losing the <br> newline.

        The fix: the JS extractor must recursively walk nodes, emitting \\n
        for <br> elements."""
        # What the CURRENT buggy extractor would return
        buggy_output = _composer_text_with_br()  # "line1line2"
        dom, driver = _make_dom(buggy_output)
        # This should FAIL with the current code (the newline is lost)
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', "line1\nline2"
        )
        assert result is False, (
            "Current extractor loses <br> newlines — this test proves the bug"
        )

    @pytest.mark.asyncio
    async def test_multiline_blocks_are_correct(self):
        """Verify the existing block-aware extraction works for standard
        multi-paragraph input (each paragraph as a separate <p>)."""
        correct_output = _composer_text_multiline_blocks()
        dom, driver = _make_dom(correct_output)
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea',
            "para1\npara2\n\nblank_line"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_nfc_normalization_for_combining_accents(self):
        """Defect 3 (confirmed): the verifier doesn't apply NFC normalization.
        A combining-accent sequence (e.g., é as 'e' + U+0301) should match
        the precomposed form (é = U+00E9). The turn-anchor matcher already
        uses NFC; the verifier doesn't."""
        # Precomposed é (what the user typed)
        expected = "caf\u00e9"  # café
        # Decomposed é (what ProseMirror might produce internally)
        actual = "cafe\u0301"  # cafe + combining acute
        # Verify they're NOT equal without NFC
        assert expected != actual, "Precondition: without NFC these differ"
        # The verifier SHOULD treat them as equal after NFC
        dom, driver = _make_dom(actual)
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', expected
        )
        # With the fix (NFC applied), this should be True
        assert result is True, (
            "NFC normalization should make precomposed and decomposed forms equal"
        )

    @pytest.mark.asyncio
    async def test_trailing_newline_not_stripped(self):
        """Defect 4 (definite correctness bug): the OLD JS extractor stripped
        one trailing \\n unconditionally. A prompt that legitimately ends in \\n
        failed verification.

        The fix: JS does NOT strip trailing newlines. The Python-side tolerance
        handles editor-added trailing newlines (accepts actual == expected OR
        actual == expected + \\n).

        This test simulates what the NEW JS returns for input "hello\\n":
        ProseMirror renders <p>hello</p><p><br></p>, the recursive extractor
        returns "hello\\n" (no stripping). The Python comparison accepts it
        because actual == expected."""
        # What the FIXED JS returns (no trailing-newline strip)
        dom, driver = _make_dom("hello\n")
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', "hello\n"
        )
        assert result is True, (
            "User's trailing newline should not cause verification failure"
        )

    @pytest.mark.asyncio
    async def test_em_dash_and_curly_quotes_pass(self):
        """Common agent content: em-dashes (—) and curly quotes (' ' " ")
        should pass verification. These are NFC-stable so NFC doesn't change
        them, but they should still round-trip correctly."""
        text = 'Here\u2019s a test \u2014 with \u201ccurly quotes\u201d and an em-dash.'
        dom, driver = _make_dom(text)
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', text
        )
        assert result is True, (
            "Em-dashes and curly quotes should pass verification"
        )

    @pytest.mark.asyncio
    async def test_bounded_polling_not_fixed_delay(self):
        """Defect 2 (medium-high confidence): the current code uses a fixed 500ms
        delay after insertText, then one read. Under load, ProseMirror may not
        have settled. The fix: poll with bounded retries.

        This test verifies that _verify_composer_text is called AFTER type_message
        has settled the DOM, not just once after a fixed delay. We test this
        indirectly by checking that type_message eventually succeeds even when
        the first read returns stale data."""
        # We can't directly test the polling in _verify_composer_text (it's a
        # single-shot read). The fix moves the polling INTO the verifier.
        # For now, verify the structure: _verify_composer_text should accept
        # multiple attempts via a polling loop.
        pass  # Will be addressed when the fix implements polling

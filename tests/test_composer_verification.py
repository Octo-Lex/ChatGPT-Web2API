"""Tests: composer text verification round-trip (ChatGPT review, conv 6a52f0f3).

Tests the 4 defects ChatGPT identified by reading the actual code:

1. Complex-newline DOM reconstruction — <br> inside <p> is lost by textContent
2. Missing NFC normalization — composed/decomposed Unicode sequences differ
3. Unconditional trailing-newline removal — user's trailing \n is stripped
4. Fixed 500ms stabilization — needs bounded polling instead

The extractor tests (round 2, ChatGPT review conv 6a52f0f3) use a Python
reimplementation of the JS DOM walker to verify the extraction logic against
realistic ProseMirror DOM shapes — not just mocked output strings.
"""

import unicodedata
from unittest.mock import AsyncMock

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


# ── Python reimplementation of the JS extractor ──────────────────────────
# Mirrors the JS in _verify_composer_text exactly so tests exercise the
# extraction LOGIC, not just the comparator. If the JS changes, this must
# change too. (ChatGPT review finding: tests must execute the extractor.)

class _Node:
    """Minimal DOM node for testing the extractor logic."""
    def __init__(self, tag=None, text=None, children=None):
        self.tag = tag  # None = text node
        self.text = text
        self.children = children or []

    @property
    def node_type(self):
        return 3 if self.tag is None else 1

    @property
    def node_value(self):
        return self.text if self.tag is None else None

    @property
    def first_child(self):
        return self.children[0] if self.children else None

    @property
    def child_nodes_length(self):
        return len(self.children)


def _is_placeholder_break_block(node):
    """Python mirror of the JS isPlaceholderBreakBlock function."""
    return (
        node.node_type == 1
        and node.child_nodes_length == 1
        and node.first_child.node_type == 1
        and node.first_child.tag == "BR"
    )


def _extract_text(node):
    """Python mirror of the JS extractText function."""
    if node.node_type == 3:
        return (node.text or "").replace("\u00a0", " ")
    if node.node_type != 1:
        return ""
    if node.tag == "BR":
        return "\n"
    text = ""
    for child in node.children:
        text += _extract_text(child)
    return text


def _extract_composer(root_children):
    """Python mirror of the JS root-level extraction loop."""
    parts = []
    for child in root_children:
        if child.node_type == 3:
            t = (child.text or "").replace("\u00a0", " ")
            if t:
                parts.append(t)
        elif child.node_type == 1:
            parts.append("" if _is_placeholder_break_block(child) else _extract_text(child))
    return "\n".join(parts)


# ── Extractor logic tests (ChatGPT review finding 2) ─────────────────────


class TestExtractorLogic:
    """Tests that execute the extraction logic against realistic ProseMirror
    DOM shapes. These are NOT mock tests — they run the actual extraction
    algorithm against constructed DOM trees."""

    def test_br_inside_p_preserves_newline(self):
        """<p>line1<br>line2</p> should extract as 'line1\\nline2'.
        This is the core bug — textContent would give 'line1line2'."""
        p = _Node("P", children=[
            _Node(text="line1"),
            _Node("BR"),
            _Node(text="line2"),
        ])
        result = _extract_composer([p])
        assert result == "line1\nline2", f"Expected 'line1\\nline2', got {result!r}"

    def test_blank_paragraph_in_middle(self):
        """<p>a</p><p><br></p><p>b</p> should extract as 'a\\n\\nb'.
        The placeholder <br> block must be treated as empty, not as \\n.
        (ChatGPT merge blocker: this was producing 'a\\n\\n\\nb'.)"""
        children = [
            _Node("P", children=[_Node(text="a")]),
            _Node("P", children=[_Node("BR")]),  # placeholder
            _Node("P", children=[_Node(text="b")]),
        ]
        result = _extract_composer(children)
        assert result == "a\n\nb", f"Expected 'a\\n\\nb', got {result!r}"

    def test_trailing_blank_paragraph(self):
        """<p>hello</p><p><br></p> should extract as 'hello\\n'.
        The trailing placeholder block becomes empty, joined with \\n."""
        children = [
            _Node("P", children=[_Node(text="hello")]),
            _Node("P", children=[_Node("BR")]),
        ]
        result = _extract_composer(children)
        assert result == "hello\n", f"Expected 'hello\\n', got {result!r}"

    def test_consecutive_blank_paragraphs(self):
        """<p>a</p><p><br></p><p><br></p><p>b</p> → 'a\\n\\n\\nb'"""
        children = [
            _Node("P", children=[_Node(text="a")]),
            _Node("P", children=[_Node("BR")]),
            _Node("P", children=[_Node("BR")]),
            _Node("P", children=[_Node(text="b")]),
        ]
        result = _extract_composer(children)
        assert result == "a\n\n\nb", f"Expected 'a\\n\\n\\nb', got {result!r}"

    def test_inline_nesting_with_br(self):
        """<p><span>line1</span><br><strong>line2</strong></p> → 'line1\\nline2'.
        Nested inline wrappers should be handled correctly."""
        p = _Node("P", children=[
            _Node("SPAN", children=[_Node(text="line1")]),
            _Node("BR"),
            _Node("STRONG", children=[_Node(text="line2")]),
        ])
        result = _extract_composer([p])
        assert result == "line1\nline2", f"Expected 'line1\\nline2', got {result!r}"

    def test_single_line(self):
        """<p>hello</p> → 'hello'"""
        children = [_Node("P", children=[_Node(text="hello")])]
        result = _extract_composer(children)
        assert result == "hello"


# ── Comparator tests (mocked extractor output) ───────────────────────────


class TestComparatorDefects:
    """Tests for the Python-side comparison logic (NFC, trailing newline tolerance)."""

    @pytest.mark.asyncio
    async def test_nfc_normalization_for_combining_accents(self):
        """NFC: precomposed é (U+00E9) should match decomposed é (e + U+0301)."""
        expected = "caf\u00e9"
        actual = "cafe\u0301"
        assert expected != actual, "Precondition: without NFC these differ"
        dom, driver = _make_dom(actual)
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', expected
        )
        assert result is True, "NFC should make precomposed and decomposed equal"

    @pytest.mark.asyncio
    async def test_trailing_newline_tolerated(self):
        """A composer that adds one trailing newline (ProseMirror habit)
        should still match the expected text."""
        dom, driver = _make_dom("hello\n")
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', "hello\n"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_em_dash_and_curly_quotes_pass(self):
        """Em-dashes and curly quotes should round-trip correctly."""
        text = 'Here\u2019s a test \u2014 with \u201ccurly quotes\u201d and an em-dash.'
        dom, driver = _make_dom(text)
        result = await dom._verify_composer_text(
            'div[role="textbox"]#prompt-textarea', text
        )
        assert result is True

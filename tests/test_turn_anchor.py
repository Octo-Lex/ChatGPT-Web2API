"""Tests for the A2 turn-anchor module (Step 4).

Pure-Python tests for the matcher, selectors, and tri-state collapse. No CDP,
no real browser — operates on synthetic projected mappings.
"""
from __future__ import annotations

import pytest

from chatgpt_web2api.turn_anchor import (
    TurnAnchor,
    TurnEndResult,
    collapse_to_end_turn_status,
    normalize_text,
    select_end_turn_for_turn,
    select_text_for_turn,
    user_text_matches_sent,
)

# ── Test fixtures ─────────────────────────────────────────────────────────

def _user_node(node_id: str, text: str, create_time: float, *, children=None) -> dict:
    return {
        "id": node_id,
        "message": {
            "id": node_id,
            "author": {"role": "user"},
            "create_time": create_time,
            "content": {"content_type": "text", "parts": [text]},
        },
        "children": children or [],
    }


def _assistant_node(node_id: str, text: str, create_time: float, *,
                    end_turn: bool = False, content_type: str = "text",
                    parent: str | None = None, children=None) -> dict:
    return {
        "id": node_id,
        "parent": parent,
        "message": {
            "id": node_id,
            "author": {"role": "assistant"},
            "create_time": create_time,
            "end_turn": end_turn,
            "content": {"content_type": content_type,
                        "parts": [text] if text else []},
        },
        "children": children or [],
    }


def _mapping(*nodes) -> dict:
    """Build a projected mapping from a list of (id, node_dict)."""
    return {"nodes": {n[0]: n[1] for n in nodes}}


# ── Matcher tests ─────────────────────────────────────────────────────────

class TestMatcher:
    def test_full_equality_short(self):
        assert user_text_matches_sent("hello", "hello")

    def test_full_equality_with_whitespace_normalization(self):
        assert user_text_matches_sent("  hello  ", "hello")
        assert user_text_matches_sent("hello\r\nworld", "hello\nworld")

    def test_full_equality_case_sensitive(self):
        # Matcher is case-sensitive after normalization (no lowercasing).
        assert not user_text_matches_sent("Hello", "hello")

    def test_truncated_prefix_long_accepted(self):
        parent = "A" * 1024
        sent = "A" * 1024 + "B" * 100  # sent is longer; parent is a prefix
        assert user_text_matches_sent(parent, sent)

    def test_short_prefix_rejected(self):
        # 64-char shared prefix — below threshold.
        parent = "X" * 64
        sent = "X" * 64 + "Y"
        assert not user_text_matches_sent(parent, sent)

    def test_below_threshold_must_match_exactly(self):
        parent = "A" * 500
        sent = "A" * 500 + "B"
        assert not user_text_matches_sent(parent, sent)

    def test_empty_strings(self):
        assert not user_text_matches_sent("", "")
        assert not user_text_matches_sent("hello", "")


# ── Selector: primary ID path ─────────────────────────────────────────────

class TestPrimaryIdPath:
    def test_captured_id_exact_match(self):
        user = _user_node("u-1", "hello", 100.0, children=["a-1"])
        asst = _assistant_node("a-1", "Hi there", 101.0, end_turn=True, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst))
        anchor = TurnAnchor(
            sent_text="hello", mode="captured_id",
            captured_user_message_id="u-1",
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Hi there"

    def test_captured_id_not_yet_in_mapping(self):
        anchor = TurnAnchor(
            sent_text="hello", mode="captured_id",
            captured_user_message_id="u-MISSING",
        )
        mapping = _mapping(("u-1", _user_node("u-1", "hello", 100.0)))
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "not_ready"
        assert "id_not_yet_in_mapping" in result.diagnostic.get("reason", "")


# ── Selector: fallback existing_conversation ──────────────────────────────

class TestExistingConversationFallback:
    def test_text_match_with_anchor_newer(self):
        old_user = _user_node("u-old", "hello", 90.0, children=["a-old"])
        new_user = _user_node("u-new", "hello", 100.0, children=["a-new"])
        asst = _assistant_node("a-new", "Fresh reply", 101.0, end_turn=True, parent="u-new")
        mapping = _mapping(("u-old", old_user), ("u-new", new_user), ("a-new", asst))
        anchor = TurnAnchor(
            sent_text="hello", mode="existing_conversation",
            latest_user_node_id="u-old", latest_user_create_time=90.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Fresh reply"

    def test_timestamp_tie_node_id_disambiguates(self):
        # Two user nodes with same text and same create_time; different ids.
        u1 = _user_node("u-1", "hello", 100.0, children=["a-1"])
        u2 = _user_node("u-2", "hello", 100.0, children=["a-2"])
        a1 = _assistant_node("a-1", "Reply 1", 101.0, end_turn=True, parent="u-1")
        a2 = _assistant_node("a-2", "Reply 2", 102.0, end_turn=True, parent="u-2")
        mapping = _mapping(("u-1", u1), ("u-2", u2), ("a-1", a1), ("a-2", a2))
        anchor = TurnAnchor(
            sent_text="hello", mode="existing_conversation",
            latest_user_node_id="u-PREVIOUS", latest_user_create_time=100.0,
        )
        # Both u-1 and u-2 are different from u-PREVIOUS and >= 100.0 → ambiguous.
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "ambiguous"

    def test_identical_answer_text_still_matches_newest(self):
        # Same text sent twice; both responses identical. Selector must still
        # find the NEWER end_turn=true descendant.
        old_user = _user_node("u-old", "ping", 90.0, children=["a-old"])
        new_user = _user_node("u-new", "ping", 100.0, children=["a-new"])
        a_old = _assistant_node("a-old", "pong", 91.0, end_turn=True, parent="u-old")
        a_new = _assistant_node("a-new", "pong", 101.0, end_turn=True, parent="u-new")
        mapping = _mapping(("u-old", old_user), ("u-new", new_user),
                           ("a-old", a_old), ("a-new", a_new))
        anchor = TurnAnchor(
            sent_text="ping", mode="existing_conversation",
            latest_user_node_id="u-old", latest_user_create_time=90.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        # Should return the newer assistant's text (both "pong" but from a-new).
        assert result.diagnostic.get("assistant_node") == "a-new"

    def test_no_fresh_text_match(self):
        # Only the previous user node matches; it's not newer.
        old_user = _user_node("u-old", "hello", 90.0, children=["a-old"])
        mapping = _mapping(("u-old", old_user))
        anchor = TurnAnchor(
            sent_text="hello", mode="existing_conversation",
            latest_user_node_id="u-old", latest_user_create_time=90.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "not_ready"


# ── Selector: degraded_existing ───────────────────────────────────────────

class TestDegradedExisting:
    def test_degraded_never_matches_single_fresh(self):
        """Degraded mode never accepts a match — it only polls or times out.
        Even a single fresh node with no stale alternatives is not sufficient
        evidence, because we can't prove it's the NEW turn vs the previous
        same-text turn still within the freshness window."""
        user = _user_node("u-1", "hello", 100.0, children=["a-1"])
        asst = _assistant_node("a-1", "Reply", 101.0, end_turn=True, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst))
        anchor = TurnAnchor(
            sent_text="hello", mode="degraded_existing",
            pre_send_wall_time=95.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status != "matched"
        assert result.status == "not_ready"

    def test_degraded_stale_single_match_rejected(self):
        # Old user node from 80.0; pre_send_wall_time=95.0.
        # 80.0 >= 95.0 - 8.0 = 87.0? No → stale.
        user = _user_node("u-old", "hello", 80.0, children=["a-old"])
        mapping = _mapping(("u-old", user))
        anchor = TurnAnchor(
            sent_text="hello", mode="degraded_existing",
            pre_send_wall_time=95.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "degraded_not_fresh"

    def test_degraded_ambiguous_two_fresh(self):
        u1 = _user_node("u-1", "hello", 100.0, children=["a-1"])
        u2 = _user_node("u-2", "hello", 101.0, children=["a-2"])
        mapping = _mapping(("u-1", u1), ("u-2", u2))
        anchor = TurnAnchor(
            sent_text="hello", mode="degraded_existing",
            pre_send_wall_time=95.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "ambiguous"

    def test_degraded_same_text_repeat_does_not_silently_match(self):
        """PR #39 review finding #3: degraded mode with one fresh + one stale
        same-text match must NOT silently pick the fresh one — we can't
        distinguish 'new node propagated' from 'old node still fresh enough.'
        Must return not_ready (keep polling) until disambiguated."""
        # Previous turn's user node — stale (below freshness floor).
        u_old = _user_node("u-old", "continue", 82.0, children=["a-old"])
        # New turn's user node — fresh (within the TOL=8 window).
        u_new = _user_node("u-new", "continue", 100.0, children=["a-new"])
        mapping = _mapping(("u-old", u_old), ("u-new", u_new))
        anchor = TurnAnchor(
            sent_text="continue", mode="degraded_existing",
            pre_send_wall_time=95.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status != "matched"
        assert result.status in ("not_ready", "degraded_ambiguous_with_stale")

    def test_degraded_single_fresh_previous_turn_not_accepted(self):
        """The narrowest rapid-repeat case: only ONE matching user node exists,
        it passes the freshness floor, but it's actually the PREVIOUS turn's
        node (the new node hasn't propagated yet). Degraded mode must NOT
        accept this — it has insufficient evidence to prove this is the new
        turn rather than the old one.
        (PR #39 review — the prior implementation accepted this case.)"""
        # Previous "continue" sent ~3s ago; backend create_time is ~5s ahead
        # of local, so this node's create_time=100.0 is within the freshness
        # floor of pre_send_wall_time=95.0 (100.0 >= 95.0 - 8.0 = 87.0).
        # But the NEW "continue" send's node hasn't propagated to the backend
        # mapping yet — there's only one match, and it's the OLD one.
        user = _user_node("u-prev", "continue", 100.0, children=["a-prev"])
        mapping = _mapping(("u-prev", user))
        anchor = TurnAnchor(
            sent_text="continue", mode="degraded_existing",
            pre_send_wall_time=95.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status != "matched"
        assert result.status == "not_ready"


# ── Selector: fresh_chat ──────────────────────────────────────────────────

class TestFreshChat:
    def test_fresh_chat_single_match(self):
        user = _user_node("u-1", "hello", 100.0, children=["a-1"])
        asst = _assistant_node("a-1", "Hi!", 101.0, end_turn=True, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst))
        anchor = TurnAnchor(sent_text="hello", mode="fresh_chat")
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Hi!"

    def test_fresh_chat_no_match_returns_not_ready(self):
        mapping = _mapping(("u-1", _user_node("u-1", "different", 100.0)))
        anchor = TurnAnchor(sent_text="hello", mode="fresh_chat")
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "not_ready"


# ── Selector: terminal selection (newest end_turn, not first) ─────────────

class TestTerminalSelection:
    def test_picks_newest_end_turn_not_first_text_descendant(self):
        # Graph: user → reasoning → draft_text → final_text(end_turn)
        # The draft has text but no end_turn; the final has end_turn=true.
        user = _user_node("u-1", "hello", 100.0, children=["r-1"])
        reasoning = {
            "id": "r-1", "parent": "u-1",
            "message": {"id": "r-1", "author": {"role": "assistant"},
                        "create_time": 100.5, "content": {"content_type": "reasoning_recap", "parts": []}},
            "children": ["a-draft"],
        }
        draft = _assistant_node("a-draft", "Draft text", 101.0, parent="r-1", children=["a-final"])
        final = _assistant_node("a-final", "Final text", 102.0, end_turn=True, parent="a-draft")
        mapping = _mapping(("u-1", user), ("r-1", reasoning),
                           ("a-draft", draft), ("a-final", final))
        anchor = TurnAnchor(sent_text="hello", mode="captured_id",
                            captured_user_message_id="u-1")
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Final text"  # NOT "Draft text"
        assert result.diagnostic.get("assistant_node") == "a-final"


# ── Selector: non-text completion ─────────────────────────────────────────

class TestNonTextCompletion:
    def test_non_text_assistant_no_text_match(self):
        user = _user_node("u-1", "gen image", 100.0, children=["a-img"])
        img = _assistant_node("a-img", "", 101.0, end_turn=True,
                              content_type="multimodal_text", parent="u-1")
        mapping = _mapping(("u-1", user), ("a-img", img))
        anchor = TurnAnchor(sent_text="gen image", mode="captured_id",
                            captured_user_message_id="u-1")
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "non_text"

    def test_end_turn_non_text_completes_only_with_dom_guard(self):
        user = _user_node("u-1", "gen image", 100.0, children=["a-img"])
        img = _assistant_node("a-img", "", 101.0, end_turn=True,
                              content_type="multimodal_text", parent="u-1")
        mapping = _mapping(("u-1", user), ("a-img", img))
        anchor = TurnAnchor(sent_text="gen image", mode="captured_id",
                            captured_user_message_id="u-1")

        # Without DOM guard → not_ready.
        r1 = select_end_turn_for_turn(mapping, anchor, had_non_text_content=False)
        assert r1.status == "not_ready"

        # With DOM guard → matched (complete).
        r2 = select_end_turn_for_turn(mapping, anchor, had_non_text_content=True)
        assert r2.status == "matched"


# ── Tri-state collapse ────────────────────────────────────────────────────

class TestTriStateCollapse:
    @pytest.mark.parametrize("internal,expected", [
        ("matched", "complete"),
        ("not_ready", "not_ready"),
        ("ambiguous", "not_ready"),
        ("degraded_not_fresh", "not_ready"),
        ("fetch_failed", "fetch_failed"),
    ])
    def test_collapse(self, internal, expected):
        result = TurnEndResult(status=internal)
        assert collapse_to_end_turn_status(result) == expected

    def test_non_text_without_dom_guard_collapses_to_not_ready(self):
        # non_text status (text selector) isn't an end_turn status, but verify
        # that if it leaks through, it collapses to not_ready (safe default).
        result = TurnEndResult(status="non_text")
        assert collapse_to_end_turn_status(result) == "not_ready"


# ── with_captured_id ─────────────────────────────────────────────────────

class TestWithCapturedId:
    def test_with_uuid_returns_new_anchor_with_id(self):
        base = TurnAnchor(sent_text="hello", mode="existing_conversation")
        updated = base.with_captured_id("uuid-123")
        assert updated.captured_user_message_id == "uuid-123"
        assert updated.sent_text == "hello"
        # Original is unchanged (immutable).
        assert base.captured_user_message_id is None

    def test_with_none_returns_same_anchor(self):
        base = TurnAnchor(sent_text="hello", mode="existing_conversation")
        updated = base.with_captured_id(None)
        assert updated is base


# ── Normalization ─────────────────────────────────────────────────────────

class TestNormalize:
    def test_nfc_normalization(self):
        # é can be composed (NFC) or decomposed (NFD).
        composed = "caf\u00e9"  # é as one char
        decomposed = "cafe\u0301"  # e + combining accent
        assert normalize_text(composed) == normalize_text(decomposed)

    def test_zero_width_stripped(self):
        assert normalize_text("hello\u200bworld") == "helloworld"

    def test_crlf_to_lf(self):
        assert normalize_text("a\r\nb\rc") == "a\nb\nc"

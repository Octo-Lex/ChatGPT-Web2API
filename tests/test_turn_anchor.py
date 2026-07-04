import pytest

from chatgpt_web2api.models import TurnAnchor
from chatgpt_web2api.selectors import (
    TurnEndResult,
    collapse_to_end_turn_status,
    normalize_text,
    select_end_turn_for_turn,
    select_text_for_turn,
)


def _user_node(id: str, text: str, create_time: float, children=None):
    return {
        "id": id,
        "parent": None,
        "message": {
            "id": id,
            "author": {"role": "user"},
            "create_time": create_time,
            "content": {"content_type": "text", "parts": [text]},
        },
        "children": children or [],
    }


def _assistant_node(
    id: str,
    text: str,
    create_time: float,
    end_turn: bool = False,
    parent: str = None,
    children=None,
    content_type: str = "text",
):
    return {
        "id": id,
        "parent": parent,
        "message": {
            "id": id,
            "author": {"role": "assistant"},
            "create_time": create_time,
            "content": {"content_type": content_type, "parts": [text]},
            "metadata": {"is_complete": end_turn},
        },
        "children": children or [],
    }


def _mapping(*pairs):
    return {id: node for id, node in pairs}


class TestExistingConversation:
    def test_simple_match(self):
        user = _user_node("u-1", "hello", 100.0, children=["a-1"])
        asst = _assistant_node("a-1", "Hi", 101.0, end_turn=True, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst))
        anchor = TurnAnchor(
            sent_text="hello",
            mode="existing_conversation",
            latest_user_node_id="u-1",
            latest_user_create_time=100.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Hi"

    def test_ambiguous_if_two_children(self):
        user = _user_node("u-1", "hello", 100.0, children=["a-1", "a-2"])
        asst1 = _assistant_node("a-1", "Hi", 101.0, parent="u-1")
        asst2 = _assistant_node("a-2", "Hello", 101.0, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst1), ("a-2", asst2))
        anchor = TurnAnchor(
            sent_text="hello", mode="existing_conversation",
            latest_user_node_id="u-1", latest_user_create_time=100.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "ambiguous"

    def test_fresh_if_user_newer_than_anchor(self):
        user = _user_node("u-new", "hello", 105.0, children=["a-1"])
        asst = _assistant_node("a-1", "Hi", 106.0, end_turn=True, parent="u-new")
        mapping = _mapping(("u-new", user), ("a-1", asst))
        # Anchor reflects the old state.
        anchor = TurnAnchor(
            sent_text="hello",
            mode="existing_conversation",
            latest_user_node_id="u-old",
            latest_user_create_time=100.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "fresh"

    def test_not_ready_if_no_reply(self):
        user = _user_node("u-1", "hello", 100.0, children=[])
        mapping = _mapping(("u-1", user))
        anchor = TurnAnchor(
            sent_text="hello", mode="existing_conversation",
            latest_user_node_id="u-1", latest_user_create_time=100.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "not_ready"

    def test_not_ready_if_user_not_newer(self):
        # If the latest user node is the same as the one in the anchor,
        # we assume we've already processed it. So we need a newer one.
        user = _user_node("u-1", "hello", 100.0, children=["a-1"])
        asst = _assistant_node("a-1", "Hi", 101.0, end_turn=True, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst))
        anchor = TurnAnchor(
            sent_text="hello", mode="existing_conversation",
            latest_user_node_id="u-1", latest_user_create_time=100.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "not_ready"

    def test_not_ready_if_previous_user_node_matches_its_not_newer(self):
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
    def test_degraded_fresh_match_accepted(self):
        # Backend create_time is ~5s ahead of local; pre_send_wall_time=95.0.
        # New user at 100.0 → 100.0 >= 95.0 - 8.0 = 87.0 → fresh.
        user = _user_node("u-1", "hello", 100.0, children=["a-1"])
        asst = _assistant_node("a-1", "Reply", 101.0, end_turn=True, parent="u-1")
        mapping = _mapping(("u-1", user), ("a-1", asst))
        anchor = TurnAnchor(
            sent_text="hello", mode="degraded_existing",
            pre_send_wall_time=95.0,
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Reply"

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

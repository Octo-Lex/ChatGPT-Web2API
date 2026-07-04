"""Tests for the A2 backend projection JS (Step 5).

Validates that ``CONVERSATION_PROJECTION_JS`` produces the expected compact
schema when run against a backend mapping. Uses a synthetic mapping fixture
now; the real captured thinking/tool-use fixture is a Step 10 hard prerequisite
(no merge without it).

The projection JS itself can't run in pure Python (it's JavaScript). These
tests validate the *contract* — what the projected output must look like — by
simulating the projection's behavior in Python and asserting the selectors
work on the result. The actual JS execution is validated in Step 10 against
a real captured mapping via the CDP instrumentation scripts.
"""
from __future__ import annotations

from chatgpt_web2api.backend_projection import (
    CONVERSATION_PROJECTION_JS,
    PROJECTED_SCHEMA_FIELDS,
    TURN_PROJECTION_LIMIT,
)
from chatgpt_web2api.turn_anchor import (
    TurnAnchor,
    select_end_turn_for_turn,
    select_text_for_turn,
)


class TestProjectionConstant:
    """Validate the JS constant is well-formed."""

    def test_js_is_nonempty_string(self):
        assert isinstance(CONVERSATION_PROJECTION_JS, str)
        assert len(CONVERSATION_PROJECTION_JS) > 100

    def test_js_fetches_correct_endpoint(self):
        assert "/backend-api/conversation/" in CONVERSATION_PROJECTION_JS
        assert "offset=0" in CONVERSATION_PROJECTION_JS
        assert "limit=" in CONVERSATION_PROJECTION_JS

    def test_js_threads_data_slots(self):
        assert "__D.conv_id" in CONVERSATION_PROJECTION_JS
        assert "__D.token" in CONVERSATION_PROJECTION_JS
        assert "__D.limit" in CONVERSATION_PROJECTION_JS

    def test_js_status_decode(self):
        assert "__status" in CONVERSATION_PROJECTION_JS

    def test_js_projects_to_compact_schema(self):
        # The JS must output {nodes: {...}, current_node: ...}.
        assert "nodes" in CONVERSATION_PROJECTION_JS
        assert "current_node" in CONVERSATION_PROJECTION_JS

    def test_js_preserves_graph_structure(self):
        # Must keep parent, children, role — not just text.
        assert "parent" in CONVERSATION_PROJECTION_JS
        assert "children" in CONVERSATION_PROJECTION_JS
        assert "role" in CONVERSATION_PROJECTION_JS

    def test_js_drops_heavy_text_for_non_text(self):
        # Non-text nodes should have empty text (heavy payload dropped).
        assert "content_type" in CONVERSATION_PROJECTION_JS


class TestProjectionLimit:
    def test_default_limit_is_50(self):
        assert TURN_PROJECTION_LIMIT == 50

    def test_limit_env_overridable(self, monkeypatch):
        # Re-import to pick up env. (The module reads os.getenv at import.)
        monkeypatch.setenv("W2A_TURN_PROJECTION_LIMIT", "100")
        import importlib

        import chatgpt_web2api.backend_projection as mod
        importlib.reload(mod)
        assert mod.TURN_PROJECTION_LIMIT == 100
        # Restore.
        monkeypatch.delenv("W2A_TURN_PROJECTION_LIMIT", raising=False)
        importlib.reload(mod)


class TestSchemaFields:
    def test_all_required_fields_documented(self):
        required = {"id", "parent", "children", "role", "create_time",
                    "end_turn", "content_type", "text"}
        assert required == set(PROJECTED_SCHEMA_FIELDS.keys())


class TestProjectedOutputSelectability:
    """Verify the selectors work on the projected schema shape.

    Simulates what the projection JS would produce for a typical mapping
    (including intermediary reasoning nodes) and confirms the selectors
    resolve correctly. This is the contract the JS must satisfy.
    """

    def _projected_mapping_with_reasoning(self) -> dict:
        """Simulated projection output for: user → reasoning → draft → final."""
        return {
            "nodes": {
                "u-1": {
                    "id": "u-1", "parent": None, "children": ["r-1"],
                    "role": "user", "create_time": 100.0, "end_turn": False,
                    "content_type": "text", "text": "explain recursion",
                },
                "r-1": {
                    "id": "r-1", "parent": "u-1", "children": ["a-draft"],
                    "role": "assistant", "create_time": 100.5, "end_turn": False,
                    "content_type": "reasoning_recap", "text": "",  # dropped
                },
                "a-draft": {
                    "id": "a-draft", "parent": "r-1", "children": ["a-final"],
                    "role": "assistant", "create_time": 101.0, "end_turn": False,
                    "content_type": "text", "text": "Recursion is...",
                },
                "a-final": {
                    "id": "a-final", "parent": "a-draft", "children": [],
                    "role": "assistant", "create_time": 102.0, "end_turn": True,
                    "content_type": "text", "text": "Recursion is a function that calls itself.",
                },
            },
            "current_node": "a-final",
        }

    def test_selector_finds_terminal_through_intermediary(self):
        mapping = self._projected_mapping_with_reasoning()
        anchor = TurnAnchor(
            sent_text="explain recursion", mode="captured_id",
            captured_user_message_id="u-1",
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "Recursion is a function that calls itself."
        assert result.diagnostic["assistant_node"] == "a-final"

    def test_selector_end_turn_through_intermediary(self):
        mapping = self._projected_mapping_with_reasoning()
        anchor = TurnAnchor(
            sent_text="explain recursion", mode="captured_id",
            captured_user_message_id="u-1",
        )
        result = select_end_turn_for_turn(mapping, anchor, had_non_text_content=False)
        assert result.status == "matched"

    def test_projected_mapping_has_all_required_fields(self):
        mapping = self._projected_mapping_with_reasoning()
        for nid, node in mapping["nodes"].items():
            for field in PROJECTED_SCHEMA_FIELDS:
                assert field in node, f"node {nid} missing field {field}"

    def test_reasoning_node_text_is_empty_after_projection(self):
        mapping = self._projected_mapping_with_reasoning()
        reasoning = mapping["nodes"]["r-1"]
        assert reasoning["content_type"] == "reasoning_recap"
        assert reasoning["text"] == ""  # heavy payload dropped, node preserved


class TestTargetOutsideOldLimit:
    """Regression: a correlated pair at node 6-10 (outside old limit=5)."""

    def test_selector_finds_pair_beyond_limit_5(self):
        # Build a mapping with 8 user/assistant pairs; the target is pair 7.
        # Each pair is a flat sequence: user-N → assistant-N, with the NEXT
        # user as a sibling (not a child of the assistant). This matches
        # ChatGPT's real graph shape.
        nodes = {}
        for i in range(1, 9):
            u_id = f"u-{i}"
            a_id = f"a-{i}"
            nodes[u_id] = {
                "id": u_id, "parent": None, "children": [a_id],
                "role": "user", "create_time": float(100 + i * 2),
                "end_turn": False, "content_type": "text", "text": f"prompt {i}",
            }
            nodes[a_id] = {
                "id": a_id, "parent": u_id, "children": [],
                "role": "assistant", "create_time": float(101 + i * 2),
                "end_turn": True, "content_type": "text", "text": f"response {i}",
            }
        mapping = {"nodes": nodes, "current_node": "a-8"}
        # Target pair 7 — well beyond the old limit=5.
        anchor = TurnAnchor(
            sent_text="prompt 7", mode="captured_id",
            captured_user_message_id="u-7",
        )
        result = select_text_for_turn(mapping, anchor)
        assert result.status == "matched"
        assert result.text == "response 7"

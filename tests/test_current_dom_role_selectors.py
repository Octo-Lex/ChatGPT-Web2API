"""Behavioral CDP DOM tests for the 2026-11-20 aria-only ChatGPT UI.

Executes the real Python-composed selectors and phase-2 JavaScript in Node vm
against miniature current and legacy DOM fixtures (not a live browser).
No source-text grep or fake returns standing in for CSS query semantics.
"""

import json
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.completion_detector import CompletionDetector
from chatgpt_web2api.dom_turn_signals import (
    ASSISTANT_TURN_COUNT_JS,
    USER_TURN_COUNT_JS,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnEndResult, TurnTextResult

NODE_AVAILABLE = shutil.which("node") is not None


_JS_FIXTURE = r"""
const fs = require('fs');
const vm = require('vm');
const {script, mode, scenario} = JSON.parse(fs.readFileSync(0, 'utf8'));
const state = scenario || {};
function el(kind) {
  return {
    offsetParent: {},
    getClientRects: () => [{}],
    getBoundingClientRect: () => ({top:100, bottom:180}),
    getAttribute: k => k === 'aria-label' ? kind : null,
    closest: selector => selector.includes('article') ? root : null,
  };
}
const root = {
  innerText: mode === 'current'
    ? 'ChatGPT said:\n\n' + (state.answer ?? 'Current UI answer.')
    : (state.answer ?? 'Legacy answer.'),
  innerHTML: '<p>Current UI answer</p>'.repeat(3),
  children: [1],
  parentElement: null,
  getBoundingClientRect: () => ({top:100, bottom:180}),
  querySelector: selector => {
    if (selector.includes('result-thinking')) return null;
    if (mode === 'legacy' && selector.includes('markdown')) {
      return {textContent: state.answer || 'Legacy answer.'};
    }
    return null; // Current UI has NO .markdown or data-message-author-role
  },
  querySelectorAll: selector => {
    if (selector.includes('turn-action-button') && mode === 'legacy') {
      return [el('old-action')];
    }
    return [];
  },
};
const copies = Array.from({length:state.copies ?? (mode === 'current' ? 1 : 0)}, () => el('Copy'));
const edits = Array.from({length:state.edits ?? (mode === 'current' ? 1 : 0)}, () => el('Edit message'));
const legacyAssistants = mode === 'legacy' ? [root] : [];
const legacyUsers = mode === 'legacy' ? [el('user')] : [];
const hiddenMenus = state.hiddenMenus || [];  // irrelevant role=menuitem nodes
const assistantExtras = ['Read aloud', 'Share', 'More actions'];
const userExtras = ['Share prompt'];
const document = {
  body: {innerText: state.bodyText || '<script>Copy x12 Retry x12</script>'},
  querySelectorAll: selector => {
    if (selector.startsWith('button[aria-label="Copy"]')) return copies;
    if (selector.startsWith('button[aria-label="Edit message"]')) return edits;
    if (mode === 'current') {
      for (const label of [...assistantExtras, ...userExtras]) {
        if (selector === 'button[aria-label="' + label + '"]') return [el(label)];
      }
    }
    if (selector.startsWith('[data-message-author-role="assistant"]')) return legacyAssistants;
    if (selector.startsWith('[data-message-author-role="user"]')) return legacyUsers;
    if (selector.includes('stop-button')) return state.stop ? [el('Stop')] : [];
    if (selector.includes('[role="menuitem"]')) return hiddenMenus;
    return [];
  },
};
try {
  const result = vm.runInNewContext(script, {document}, {timeout:1000});
  console.log(JSON.stringify({result}));
} catch(e) {
  console.log(JSON.stringify({error:String(e)}));
  process.exit(1);
}
"""


def _eval_js(script, mode="current", **scenario):
    if not NODE_AVAILABLE:
        pytest.skip("Node.js not installed")
    p = subprocess.run(
        ["node", "-e", _JS_FIXTURE],
        input=json.dumps({"script": script, "mode": mode, "scenario": scenario}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(p.stdout)["result"]


class _CapturedProbe(Exception):
    pass


async def _actual_phase2_expression(initial_count=0):
    """Collect the actual generated JS, rather than reimplementing it in tests."""
    d = MagicMock()
    d._current_conv_id = None
    d._pre_send_user_count = None
    seen = []

    async def _js(expr, timeout=15):
        if "body.innerText" in expr:
            return json.dumps({"text": ""})
        if "copyButtons" in expr and "has_action" in expr:
            seen.append(expr)
            raise _CapturedProbe
        return str(initial_count + 1)  # Phase-1 assistant-count delta

    d._js_strict = _js
    det = CompletionDetector(d)
    with pytest.raises(_CapturedProbe):
        async for _ in det.stream_until_complete(
            initial_count=initial_count,
            timeout=10,
            turn_anchor=TurnAnchor(sent_text="probe", mode="fresh_chat"),
        ):
            pass
    assert len(seen) == 1
    return seen[0]


@pytest.mark.parametrize("mode,copies,edits,assistant,user", [
    ("current", 4, 4, 4, 4),
    ("current", 0, 0, 0, 0),
    ("legacy", 0, 0, 1, 1),
])
def test_executable_count_selectors(mode, copies, edits, assistant, user):
    kw = {"copies": copies, "edits": edits}
    assert _eval_js(ASSISTANT_TURN_COUNT_JS, mode, **kw) == assistant
    assert _eval_js(USER_TURN_COUNT_JS, mode, **kw) == user


@pytest.mark.parametrize("mode,baseline,expect_new,expected_text", [
    ("current", 0, True, "October visible answer."),
    ("legacy", 0, False, "Legacy answer."),
    ("current", 1, False, ""),
])
@pytest.mark.asyncio
async def test_phase2_executes_real_js_in_new_and_legacy_layouts(
    mode, baseline, expect_new, expected_text
):
    js = await _actual_phase2_expression(baseline)
    output = json.loads(
        _eval_js(
            js, mode,
            copies=(1 if mode == "current" else 0),
            answer=expected_text or "Prior answer.",
        )
    )
    assert output["copy_delta"] is expect_new
    if mode == "legacy":
        assert output["text"] == expected_text
    else:
        # The Copy/article wrapper is not a clean answer container: the
        # current UI includes the prefix "ChatGPT said:" in innerText.
        # Its text MUST NOT become any response delta, even on a new turn.
        assert output["text"] == ""
        assert output["md_text"] == ""
    if baseline:
        assert output["text"] == ""  # no stale previous turn
        assert output["has_action"] is False


@pytest.mark.asyncio
async def test_hidden_menu_and_script_text_do_not_create_turn_signals():
    """Counts operate on ELEMENTS, not source text or hidden sidebar labels."""
    assert _eval_js(
        ASSISTANT_TURN_COUNT_JS,
        "current",
        copies=0,
        edits=0,
        bodyText='<script>Copy Copy Copy Retry</script>',
        hiddenMenus=["mounted-but-hidden"],
    ) == 0
    assert _eval_js(USER_TURN_COUNT_JS, "current", copies=0, edits=0) == 0


@pytest.mark.asyncio
async def test_phase1_user_edit_delta_unblocks_before_copy_action():
    d = MagicMock()
    d._current_conv_id = None
    d._pre_send_user_count = 0
    d._get_live_conversation_id_best_effort = AsyncMock(return_value="")

    phase2_calls = [0]

    async def evaluate(expr, timeout=15):
        if "body.innerText" in expr:
            return '{"text": ""}'
        if expr == ASSISTANT_TURN_COUNT_JS:
            return "0"  # Copy not yet mounted, despite user turn appearing
        if expr == USER_TURN_COUNT_JS:
            return "1"  # Edit message appeared after send
        if "has_action" in expr:
            phase2_calls[0] += 1
            return json.dumps({
                "text": "Answer", "md_text": "Answer", "html_len": 60,
                "child_count": 1, "has_action": True,
                "copy_delta": True, "stop_present": False,
                "is_thinking": False,
            })
        raise AssertionError(expr)

    d._js_strict = evaluate
    det = CompletionDetector(d)
    chunks = []
    async for chunk in det.stream_until_complete(
        initial_count=0, timeout=10,
        turn_anchor=TurnAnchor(sent_text="hello", mode="fresh_chat"),
    ):
        chunks.append(chunk)

    assert phase2_calls[0] >= 2  # one extra stable-text poll
    assert "".join(c.delta for c in chunks) == "Answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_delta", [False, True])
async def test_anchored_backend_text_recovers_when_current_dom_has_no_container(
    copy_delta,
):
    d = MagicMock()
    d._current_conv_id = "conv-current"
    d._pre_send_user_count = 0
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="matched")
    )
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text="Anchored final text")
    )

    async def evaluate(expr, timeout=15):
        if "body.innerText" in expr:
            return '{"text": ""}'
        if expr == ASSISTANT_TURN_COUNT_JS:
            return "0"
        if expr == USER_TURN_COUNT_JS:
            return "1"
        if "has_action" in expr:
            return json.dumps({
                "text": "", "md_text": "", "html_len": 0,
                "child_count": 0, "has_action": False,
                "copy_delta": copy_delta, "stop_present": False,
                "is_thinking": False,
            })
        raise AssertionError(expr)

    d._js_strict = evaluate
    det = CompletionDetector(d)
    chunks = []
    async for chunk in det.stream_until_complete(
        initial_count=0, timeout=10,
        turn_anchor=TurnAnchor(sent_text="hello", mode="fresh_chat"),
    ):
        chunks.append(chunk)
    assert [c.delta for c in chunks] == ["Anchored final text"]
    assert det.last_dom_text == "Anchored final text"
    d._fetch_text_for_turn.assert_awaited_once()



@pytest.mark.asyncio
@pytest.mark.parametrize("backend_enabled,stop_present", [
    (True, False),   # backend 'not_ready' must not be overridden by Copy
    (False, True),   # Stop still visible blocks DOM-only completion
])
async def test_copy_cannot_override_backend_not_ready_or_visible_stop(
    backend_enabled, stop_present
):
    class _EndProbe(Exception):
        pass

    d = MagicMock()
    d._current_conv_id = "known-conv" if backend_enabled else None
    d._pre_send_user_count = 0
    d._get_live_conversation_id_best_effort = AsyncMock(return_value="")
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="not_ready")
    )
    phase2_calls = [0]

    async def evaluate(expr, timeout=15):
        if "body.innerText" in expr:
            return '{"text": ""}'
        if expr == ASSISTANT_TURN_COUNT_JS:
            return "0"
        if expr == USER_TURN_COUNT_JS:
            return "1"
        if "has_action" in expr:
            phase2_calls[0] += 1
            if phase2_calls[0] > 4:
                raise _EndProbe
            return json.dumps({
                "text": "In-progress answer",
                "md_text": "In-progress answer",
                "html_len": 60,
                "child_count": 1,
                "has_action": True,
                "copy_delta": True,
                "stop_present": stop_present,
                "is_thinking": False,
            })
        raise AssertionError(expr)

    d._js_strict = evaluate
    det = CompletionDetector(d)
    with pytest.raises(_EndProbe):
        async for _ in det.stream_until_complete(
            initial_count=0, timeout=10,
            turn_anchor=TurnAnchor(sent_text="hello", mode="fresh_chat"),
        ):
            pass
    assert phase2_calls[0] == 5
    if backend_enabled:
        d._fetch_end_turn_for_turn.assert_awaited()



@pytest.mark.asyncio
async def test_action_toolbar_alone_is_not_a_non_text_response():
    """A newly mounted Copy toolbar alone must not imply usable rich content."""
    js = await _actual_phase2_expression(0)
    state = json.loads(_eval_js(js, "current", copies=1, answer=""))
    assert state["copy_delta"] is True
    assert state["text"] == ""
    assert state["html_len"] == 0
    assert state["child_count"] == 0



@pytest.mark.parametrize("label", [
    "Copy", "Read aloud", "Share", "More actions",
    "Edit message", "Share prompt",
])
def test_current_layout_exposes_exact_action_row_labels_without_role_attrs(
    label,
):
    selector = json.dumps(f'button[aria-label="{label}"]')
    expression = f"document.querySelectorAll({selector}).length"
    assert _eval_js(expression, "current", copies=1, edits=1) == 1
    assert _eval_js(expression, "legacy", copies=0, edits=0) == 0


@pytest.mark.asyncio
async def test_current_dom_role_chrome_is_replaced_by_anchored_final_text():
    """Reproduce live HTTP-200 corruption: article has ChatGPT said + answer.

    Phase 1 sees the new user Edit control, but no assistant role nodes.
    Phase 2 executes the REAL detector JS in the Node current-DOM fixture,
    where a Copy/article wrapper has the role label. Only the anchored
    backend answer may be yielded; no role chrome or sliced head.
    """
    sentinel = "PR57-LIVE-dd2290aa"
    expr = await _actual_phase2_expression(0)

    d = MagicMock()
    d._current_conv_id = "6b000e1c-5974-83eb-9691-bc3450677411"
    d._pre_send_user_count = 0
    d._fetch_end_turn_for_turn = AsyncMock(
        return_value=TurnEndResult(status="matched")
    )
    d._fetch_text_for_turn = AsyncMock(
        return_value=TurnTextResult(status="matched", text=sentinel)
    )

    async def evaluate(js, timeout=15):
        if "body.innerText" in js:
            return '{"text": ""}'
        if js == ASSISTANT_TURN_COUNT_JS:
            return "0"
        if js == USER_TURN_COUNT_JS:
            return "1"
        if "copyButtons" in js and "has_action" in js:
            assert js == expr
            return _eval_js(expr, "current", copies=1, answer=sentinel)
        raise AssertionError(js)

    d._js_strict = evaluate
    detector = CompletionDetector(d)
    pieces = []
    async for chunk in detector.stream_until_complete(
        initial_count=0, timeout=10,
        turn_anchor=TurnAnchor(sent_text="test", mode="fresh_chat"),
    ):
        pieces.append(chunk.delta)

    assert pieces == [sentinel]  # no optimistic 'ChatGPT said:' chunk
    assert detector.last_dom_text == sentinel
    d._fetch_text_for_turn.assert_awaited_once()

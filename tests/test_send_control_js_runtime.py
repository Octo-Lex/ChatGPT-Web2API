"""Execute embedded navigation/Send JavaScript in a tiny real JS runtime.

This is a deterministic fake DOM, not a browser or live ChatGPT qualification.
It closes the gap between text-substring tests and actual JS parse/control flow.
Node is optional for developers without Node; GitHub Actions runner images
include Node, where these tests should execute rather than skip.
"""

import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js unavailable for JS-runtime probe"
)

_JS_FAKE_DOM = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const scenario = input.scenario || {};
const events = [];
function element(kind, cfg={}) {
  return {
    disabled: !!cfg.disabled,
    textContent: cfg.text || '',
    getClientRects: () => cfg.hidden ? [] : [{}],
    getAttribute: (key) => {
      if (key === 'aria-label') return cfg.label || null;
      if (key === 'data-testid') return cfg.testid || null;
      if (key === 'title') return cfg.title || null;
      if (key === 'aria-disabled') return cfg.ariaDisabled ? 'true' : null;
      return null;
    },
    closest: () => kind === 'composer' ? (scenario.noForm ? null : form) : null,
    dispatchEvent: (event) => { events.push(event.type); return true; },
    focus: () => {},
  };
}
const form = {
  querySelector: (selector) => scenario.noButton ? null :
    selector.includes('aria-label') ? button : null,
};
const composer = element('composer', {hidden: !!scenario.hiddenComposer});
const button = element('button', {
  disabled: !!scenario.disabledButton,
  label: scenario.buttonLabel || 'Send message',
  testid: scenario.buttonTestid || 'send-button',
});
const stops = (scenario.stops || []).map(s => element('stop', s));
const document = {
  readyState: 'complete',
  querySelector: (selector) => {
    if (selector.startsWith('div[')) return scenario.noComposer ? null : composer;
    if (selector.startsWith('textarea')) return null;
    return null;
  },
  querySelectorAll: (selector) => {
    if (selector.includes('stop-button')) return stops;
    return [];
  },
};
const context = {
  document,
  URL,
  window: {},
  location: {href: scenario.url || 'https://chatgpt.com/?model=auto'},
  MouseEvent: class MouseEvent {constructor(type) {this.type = type;}},
};
let result;
try {
  result = vm.runInNewContext(input.expression, context, {timeout: 1000});
} catch (error) {
  console.log(JSON.stringify({error: String(error), events}));
  process.exit(1);
}
console.log(JSON.stringify({result, events}));
"""


def _run_js(expression, scenario=None):
    completed = subprocess.run(
        ["node", "-e", _JS_FAKE_DOM],
        input=json.dumps({"expression": expression, "scenario": scenario or {}}),
        text=True,
        capture_output=True,
        check=True,
        timeout=8,
    )
    return json.loads(completed.stdout)


async def _capture_click_expression(expected_context=None):
    driver = CDPDriver(cdp_port=9222)
    driver._expected_send_context = expected_context
    driver._breakers = None
    driver._js = AsyncMock(side_effect=["yes", "sent"])
    await driver.click_send()
    return driver._js.await_args_list[-1].args[0]


async def _capture_navigation_expression(monkeypatch):
    driver = CDPDriver(cdp_port=9222)
    driver._cdp = AsyncMock(return_value={})
    driver._js = AsyncMock(return_value=json.dumps({
        "url": "https://chatgpt.com/?model=auto",
        "ready_state": "complete", "composer": True, "stop_present": False,
    }))
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", AsyncMock())
    await driver.navigate_new_chat()
    return driver._js.await_args_list[-1].args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,expected,click_count", [
    ({}, "sent", 5),
    ({"stops": [{"label": "Stop", "disabled": True}]},
     "stop button present", 0),
    ({"stops": [{"label": "Stop", "hidden": True},
                {"label": "Stop", "disabled": True}]},
     "stop button present", 0),
    ({"stops": [{"label": "Stop", "hidden": True}]}, "sent", 5),
    ({"buttonLabel": "Stop generating"}, "stop button present", 0),
    ({"noButton": True}, "no send button", 0),
    ({"noComposer": True}, "no send button", 0),
    ({"hiddenComposer": True}, "no send button", 0),
    ({"disabledButton": True}, "button disabled or hidden", 0),
])
async def test_js_runtime_send_control(scenario, expected, click_count):
    script = await _capture_click_expression()
    got = _run_js(script, scenario)
    assert got["result"] == expected
    assert len(got["events"]) == click_count
    if click_count:
        assert got["events"] == [
            "pointerdown", "mousedown", "pointerup", "mouseup", "click"
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,stop_present", [
    ({}, False),
    ({"stops": [{"label": "Stop", "disabled": True}]}, True),
    ({"stops": [{"label": "Stop", "hidden": True},
                {"label": "Stop", "disabled": True}]}, True),
    ({"stops": [{"label": "Stop", "hidden": True}]}, False),
])
async def test_js_runtime_navigation_stop_state(
    monkeypatch, scenario, stop_present
):
    script = await _capture_navigation_expression(monkeypatch)
    got = _run_js(script, scenario)
    state = json.loads(got["result"])
    assert state["composer"] is True
    assert state["stop_present"] is stop_present


@pytest.mark.asyncio
async def test_js_runtime_rejects_invisible_editor_before_navigation_ready(
    monkeypatch,
):
    script = await _capture_navigation_expression(monkeypatch)
    state = json.loads(
        _run_js(script, {"hiddenComposer": True})["result"]
    )
    assert state["composer"] is False



@pytest.mark.asyncio
@pytest.mark.parametrize("context,url,expected", [
    (("fresh", "/"), "https://chatgpt.com/?model=auto", "sent"),
    (("fresh", "/"), "https://chatgpt.com/c/foreign", "wrong send route"),
    (("fresh", "/"), "https://chatgpt.com.evil.test/", "wrong send route"),
    (("fresh", "/g/g-123/project"),
     "https://chatgpt.com/g/g-123/project", "sent"),
    (("fresh", "/g/g-123/project"),
     "https://chatgpt.com/g/other/project", "wrong send route"),
    (("conversation", "conv-123"),
     "https://chatgpt.com/c/conv-123?model=auto", "sent"),
    (("conversation", "conv-123"),
     "https://chatgpt.com/g/g-123/c/conv-123", "sent"),
    (("conversation", "conv-123"),
     "https://chatgpt.com/c/other", "wrong send route"),
    (("conversation", "conv-123"),
     "http://chatgpt.com/c/conv-123", "wrong send route"),
])
async def test_js_runtime_send_action_binds_expected_route(
    context, url, expected
):
    script = await _capture_click_expression(context)
    result = _run_js(script, {"url": url})
    assert result["result"] == expected
    assert len(result["events"]) == (5 if expected == "sent" else 0)


@pytest.mark.asyncio
async def test_js_runtime_typing_refuses_displaced_route(monkeypatch):
    """The focus mutation fails closed before Input.insertText."""
    d = CDPDriver(cdp_port=9222)
    d._expected_send_context = ("conversation", "expected")
    d._js = AsyncMock(return_value="composer")
    d._detect_select_all_modifier = AsyncMock(return_value=2)
    d._verify_composer_text = AsyncMock(return_value=True)
    d._cdp = AsyncMock()
    monkeypatch.setattr("chatgpt_web2api.chatgpt_dom.asyncio.sleep", AsyncMock())
    await d.type_message("example")

    focus_js = d._js.await_args_list[0].args[0]
    assert _run_js(
        focus_js, {"url": "https://chatgpt.com/c/foreign"}
    )["result"] == "wrong send route"
    assert _run_js(
        focus_js, {"url": "https://chatgpt.com/c/expected"}
    )["result"] == "composer"
    assert "if (!routeOk)" in focus_js

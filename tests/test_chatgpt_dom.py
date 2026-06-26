"""Wiring tests for ChatGPTDom (Phase 5 PR3 extraction).

These verify the extraction moved the 9 composer-DOM methods + 4 selector
constants onto ChatGPTDom and that the DOM layer reaches the driver's
transport/breaker/navigation through the correct seam. They are NOT
behavioral tests — the behavior of each method is already covered by
test_composer_selectors / test_conversation_guard / test_resilience and the
broad suite, which stub the driver's DOM methods and confirm the delegators
preserve the surface. This file guards the wiring: no method is dropped, the
DOM layer talks to the driver (not to itself) for transport/breaker, state
stays on the driver, selectors stay importable from cdp_driver, and the
driver wires _dom in __init__.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.chatgpt_dom import (
    COMPOSER_FALLBACK_SELECTOR,
    COMPOSER_SELECTOR,
    SEND_BUTTON_FALLBACK_SELECTOR,
    SEND_BUTTON_SELECTOR,
    ChatGPTDom,
)


def _make_dom():
    """A ChatGPTDom backed by a mock driver with the attributes/seam the DOM
    layer reaches through. The driver's JS evaluators default to AsyncMocks so
    individual tests override only what they assert on."""
    driver = MagicMock()
    driver._breakers = None
    driver._current_conv_id = None
    driver._js = AsyncMock(return_value="")
    driver._js_strict = AsyncMock(return_value="")
    driver._cdp = AsyncMock(return_value={})
    driver.navigate_new_chat = AsyncMock()
    driver._capture_selector_diagnostic = AsyncMock()
    driver._has_composer = AsyncMock(return_value=True)
    driver._wait_for_composer = AsyncMock(return_value=True)
    driver._detect_select_all_modifier = AsyncMock(return_value=2)
    driver._verify_composer_text = AsyncMock(return_value=True)
    return ChatGPTDom(driver), driver


# ── 1. Every moved method exists on ChatGPTDom ────────────────────────


def test_dom_has_all_moved_methods():
    """The extraction must not drop a method. Each of these is delegated by
    CDPDriver and must live on ChatGPTDom."""
    dom, _ = _make_dom()
    expected = [
        "_has_composer",
        "_wait_for_composer",
        "_ensure_send_ready",
        "type_message",
        "_detect_select_all_modifier",
        "_verify_composer_text",
        "click_send",
        "dismiss_rate_limit",
        "_capture_selector_diagnostic",
    ]
    missing = [name for name in expected if not callable(getattr(dom, name, None))]
    assert missing == [], f"ChatGPTDom is missing moved methods: {missing}"


# ── 2. Selectors moved canonically + re-exported from cdp_driver ──────


def test_selectors_live_in_chatgpt_dom():
    """The 4 selector constants must be defined in chatgpt_dom."""
    assert "ProseMirror" in COMPOSER_SELECTOR
    assert "prompt-textarea" in COMPOSER_FALLBACK_SELECTOR
    assert "Send" in SEND_BUTTON_SELECTOR
    assert "send-button" in SEND_BUTTON_FALLBACK_SELECTOR


def test_selectors_reexported_from_cdp_driver():
    """cdp_driver must re-export the selectors for back-compat (tests and the
    navigation methods that stay there import them from cdp_driver)."""
    from chatgpt_web2api import cdp_driver

    assert cdp_driver.COMPOSER_SELECTOR is COMPOSER_SELECTOR
    assert cdp_driver.COMPOSER_FALLBACK_SELECTOR is COMPOSER_FALLBACK_SELECTOR
    assert cdp_driver.SEND_BUTTON_SELECTOR is SEND_BUTTON_SELECTOR
    assert cdp_driver.SEND_BUTTON_FALLBACK_SELECTOR is SEND_BUTTON_FALLBACK_SELECTOR


# ── 3. Transport seam: DOM reaches driver._js / _js_strict / _cdp ─────


@pytest.mark.asyncio
async def test_has_composer_reaches_driver_js():
    """_has_composer must evaluate JS through the driver's soft evaluator."""
    dom, driver = _make_dom()
    driver._js = AsyncMock(return_value='{"ready": true}')
    assert await dom._has_composer() is True
    driver._js.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_selector_diagnostic_reaches_driver_js_strict():
    dom, driver = _make_dom()
    driver._js_strict = AsyncMock(return_value='{"url":"https://chatgpt.com/"}')
    await dom._capture_selector_diagnostic("test selector")
    driver._js_strict.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_composer_text_reaches_driver_js_strict():
    """_verify_composer_text reads the composer via the driver's strict
    evaluator and canonicalizes locally."""
    dom, driver = _make_dom()
    driver._js_strict = AsyncMock(return_value="hello")
    assert await dom._verify_composer_text(COMPOSER_SELECTOR, "hello") is True
    driver._js_strict.assert_awaited_once()


# ── 4. DOM peer calls route through the driver seam ──────────────────
#
# type_message → driver._detect_select_all_modifier / driver._verify_composer_text;
# _ensure_send_ready → driver.navigate_new_chat / driver._wait_for_composer /
# driver._capture_selector_diagnostic. All through self._driver, NOT self, so
# monkeypatches of those driver methods keep intercepting.


@pytest.mark.asyncio
async def test_type_message_routes_peer_calls_through_driver():
    """type_message must call driver._detect_select_all_modifier and
    driver._verify_composer_text (not the dom's own), so driver patches hold."""
    dom, driver = _make_dom()
    driver._js = AsyncMock(return_value="composer")  # focus result
    await dom.type_message("hello")
    driver._detect_select_all_modifier.assert_awaited_once()
    driver._verify_composer_text.assert_awaited()
    driver._cdp.assert_awaited()  # select-all + insertText via driver._cdp


@pytest.mark.asyncio
async def test_ensure_send_ready_routes_navigation_through_driver():
    """When the composer is missing, _ensure_send_ready must navigate via
    driver.navigate_new_chat (not a dom-local nav) and record the breaker
    failure through driver._breakers."""
    from chatgpt_web2api.breakers import BreakerKind, BreakerRegistry
    from chatgpt_web2api.cdp_driver import SendReadinessError

    dom, driver = _make_dom()
    reg = BreakerRegistry()
    driver._breakers = reg
    driver._wait_for_composer = AsyncMock(side_effect=[False, False])  # never ready
    driver.navigate_new_chat = AsyncMock()

    with pytest.raises(SendReadinessError):
        await dom._ensure_send_ready()
    driver.navigate_new_chat.assert_awaited_once()
    driver._capture_selector_diagnostic.assert_awaited_once()
    # record_failure reached through driver._breakers (threshold is 3, so one
    # failure is recorded but the breaker is not yet tripped — assert the
    # failure was recorded, not that it's open).
    state = reg._states[BreakerKind.COMPOSER_SEND_READINESS]
    assert len(state.recent_failures) == 1


@pytest.mark.asyncio
async def test_click_send_records_success_through_driver_breaker():
    """A confirmed send must record_success via driver._breakers (half-open
    recovery) — the registry stays on the driver, not the dom."""
    from chatgpt_web2api.breakers import BreakerKind, BreakerRegistry

    dom, driver = _make_dom()
    reg = BreakerRegistry()
    driver._breakers = reg
    driver._js = AsyncMock(return_value="sent")

    await dom.click_send()
    # Not open after a success record (record_success clears failures).
    assert not reg.is_open(BreakerKind.COMPOSER_SEND_READINESS)


# ── 5. dismiss_rate_limit tri-state via driver._js_strict ────────────


@pytest.mark.asyncio
async def test_dismiss_rate_limit_click_then_clear_returns_true():
    dom, driver = _make_dom()
    # First call: click succeeded; second call: re-scan shows no rate-limit text.
    driver._js_strict = AsyncMock(side_effect=['{"clicked": true}', '{"text":"normal text"}'])
    assert await dom.dismiss_rate_limit() is True


@pytest.mark.asyncio
async def test_dismiss_rate_limit_no_dialog_returns_false():
    dom, driver = _make_dom()
    driver._js_strict = AsyncMock(return_value='{"clicked": false}')
    assert await dom.dismiss_rate_limit() is False


@pytest.mark.asyncio
async def test_dismiss_rate_limit_scan_error_returns_none():
    dom, driver = _make_dom()
    driver._js_strict = AsyncMock(side_effect=['{"clicked": true}', RuntimeError("scan failed")])
    assert await dom.dismiss_rate_limit() is None


# ── 6. State stays on the driver (not migrated into the dom) ──────────


def test_dom_holds_no_driver_state():
    """The dom must NOT own _breakers / _current_conv_id / _js / _cdp — those
    stay on the driver so connect/reconnect/close and test stubs that poke
    driver._breakers keep working."""
    dom, _ = _make_dom()
    for attr in ("_breakers", "_current_conv_id", "_js", "_js_strict", "_cdp"):
        assert not hasattr(dom, attr), (
            f"ChatGPTDom must not own driver state '{attr}' (Phase 5 PR3 contract)"
        )


# ── 7. Integration: CDPDriver wires ChatGPTDom in __init__ ───────────


def test_driver_wires_chatgpt_dom():
    """CDPDriver.__init__ constructs a ChatGPTDom and delegates through it."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    assert isinstance(d._dom, ChatGPTDom)
    assert d._dom._driver is d


def test_driver_delegators_preserve_signatures():
    """All 9 moved methods remain callable on the driver (delegators), so
    internal call sites and test stubs that patch driver.type_message /
    driver.click_send etc. keep working unchanged."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    for name in (
        "_has_composer",
        "_wait_for_composer",
        "_ensure_send_ready",
        "type_message",
        "_detect_select_all_modifier",
        "_verify_composer_text",
        "click_send",
        "dismiss_rate_limit",
        "_capture_selector_diagnostic",
    ):
        assert callable(getattr(d, name, None)), f"CDPDriver lost delegator {name}"


# ── 8. Breaker stays on driver; dom only reaches through ─────────────


def test_breaker_registry_stays_on_driver():
    """The breaker registry must live on CDPDriver (default None, set via
    ctor), never on ChatGPTDom."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    assert hasattr(d, "_breakers")
    assert not hasattr(d._dom, "_breakers")

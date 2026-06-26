"""Tests for CDPDriver JS-injection safety.

The previous ``_js_with_data`` declared ``__D`` at the top level
(``const __D = {...}; <body>``). ChatGPT's own page already defines a
global ``__D``, so every such call threw
``SyntaxError: Identifier '__D' has already been declared`` and returned
empty — silently breaking memories/projects/conversation-detail reads.
A live integration test surfaced it; these tests guard the fix.
"""

import json

import pytest

# ── The fix: __D must be block-scoped, not global ─────────────

@pytest.mark.asyncio
async def test_js_with_data_does_not_declare_global___D():
    """The injected __D must NOT be a top-level const.

    A top-level ``const __D`` collides with any existing global ``__D``
    on the host page (chatgpt.com defines one), producing a SyntaxError.
    The expression must instead wrap the declaration in a function scope
    (IIFE) so __D is local.
    """
    from chatgpt_web2api.cdp_driver import CDPDriver
    driver = CDPDriver(cdp_port=9222)
    captured = {}
    async def fake_js(expr, timeout=15):
        captured["expr"] = expr
        return "ok"
    driver._js = fake_js

    await driver._js_with_data("return __D.x;", {"x": 1})

    expr = captured["expr"]
    # The dangerous form: a top-level "const __D = ..." declaration.
    # This collides with chatgpt.com's global __D and raises
    # "Identifier '__D' has already been declared".
    assert not expr.startswith("const __D"), (
        f"_js_with_data must not declare __D at top level — it collides "
        f"with chatgpt.com's global __D. Got: {expr[:60]!r}"
    )
    # __D must be introduced as a function PARAMETER (shadowing any global
    # __D within the expression's scope), not a declaration. The data must
    # be passed as a JSON-serialized argument, not concatenated into the body.
    assert "(__D)" in expr or "(__D) =>" in expr, (
        f"__D must be passed as a function parameter, not declared. "
        f"Got: {expr[:80]!r}"
    )


@pytest.mark.asyncio
async def test_js_with_data_still_passes_data_through():
    """The IIFE scoping fix must not break data injection.

    __D must still carry the injected data and the body must be able to
    read it. We assert the serialized data appears inside the expression
    and the body text is preserved.
    """
    from chatgpt_web2api.cdp_driver import CDPDriver
    driver = CDPDriver(cdp_port=9222)
    captured = {}
    async def fake_js(expr, timeout=15):
        captured["expr"] = expr
        return "ok"
    driver._js = fake_js

    payload = {"token": "abc123", "conv_id": "xyz"}
    await driver._js_with_data("__D.token + __D.conv_id", payload)

    expr = captured["expr"]
    # The data must be embedded (json-serialized) somewhere in the expression
    assert json.dumps(payload) in expr, "injected data missing from expression"
    # The body must be present
    assert "__D.token + __D.conv_id" in expr, "body expression dropped"


@pytest.mark.asyncio
async def test_js_with_data_returns_driver_value():
    """The return value of _js_with_data is whatever _js returns."""
    from chatgpt_web2api.cdp_driver import CDPDriver
    driver = CDPDriver(cdp_port=9222)
    async def fake_js(expr, timeout=15):
        return "RESULT"
    driver._js = fake_js

    out = await driver._js_with_data("1", {"a": 1})
    assert out == "RESULT"

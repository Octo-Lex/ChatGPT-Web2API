"""Wave 1 tests for _js_strict — the strict JS evaluation variant.

Verifies that _js_strict raises CDPJSError on the three failure modes that
_js silently collapses to "", and returns the value normally on success.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from chatgpt_web2api.cdp_driver import CDPDriver, CDPJSError


def _driver_with_cdp_response(resp: dict):
    """Build a driver whose _cdp returns the given response dict."""
    d = CDPDriver(cdp_port=9222)
    d._cdp = AsyncMock(return_value=resp)
    return d


@pytest.mark.asyncio
async def test_js_strict_returns_value_on_success():
    """Normal CDP response with a value returns it as a string."""
    d = _driver_with_cdp_response({
        "id": 1,
        "result": {"result": {"type": "string", "value": "hello world"}},
    })
    result = await d._js_strict("any expression")
    assert result == "hello world"


@pytest.mark.asyncio
async def test_js_strict_raises_on_exception_details():
    """JS threw an exception → CDPJSError with the exception description."""
    d = _driver_with_cdp_response({
        "id": 1,
        "result": {
            "result": {"type": "undefined"},
            "exceptionDetails": {
                "exception": {"description": "TypeError: Cannot read properties of null"},
                "text": "Uncaught",
            },
        },
    })
    with pytest.raises(CDPJSError) as ei:
        await d._js_strict("bad expression")
    assert "TypeError" in str(ei.value)
    assert ei.value.details.get("exception", {}).get("description") == "TypeError: Cannot read properties of null"


@pytest.mark.asyncio
async def test_js_strict_raises_on_cdp_error():
    """CDP-level error (e.g. execution context destroyed) → CDPJSError."""
    d = _driver_with_cdp_response({
        "id": 1,
        "error": {"code": -32000, "message": "Execution context was destroyed."},
    })
    with pytest.raises(CDPJSError) as ei:
        await d._js_strict("any expression")
    assert "Execution context" in str(ei.value)


@pytest.mark.asyncio
async def test_js_strict_raises_on_undefined():
    """JS returned undefined (no value key) → CDPJSError."""
    d = _driver_with_cdp_response({
        "id": 1,
        "result": {"result": {"type": "undefined"}},
    })
    with pytest.raises(CDPJSError) as ei:
        await d._js_strict("void(0)")
    assert "undefined" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_js_with_data_strict_returns_value_on_success():
    """_js_with_data_strict wraps the expression and returns on success."""
    d = _driver_with_cdp_response({
        "id": 1,
        "result": {"result": {"type": "string", "value": "data-injected"}},
    })
    result = await d._js_with_data_strict("__D.key", {"key": "data-injected"})
    assert result == "data-injected"


@pytest.mark.asyncio
async def test_js_with_data_strict_raises_on_js_exception():
    """_js_with_data_strict raises CDPJSError on JS exception."""
    d = _driver_with_cdp_response({
        "id": 1,
        "result": {
            "result": {"type": "undefined"},
            "exceptionDetails": {"exception": {"description": "ReferenceError: __D is not defined"}},
        },
    })
    with pytest.raises(CDPJSError):
        await d._js_with_data_strict("__D.nonexistent", {})

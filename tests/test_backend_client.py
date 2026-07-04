"""Wiring tests for BackendClient (Phase 5 PR1 extraction).

These verify the extraction moved every method onto BackendClient and that
the client reaches the driver's transport + state through the correct seam.
They are NOT behavioral tests — the behavior of each method is already
covered exhaustively by test_reliability / test_end_turn_primary /
test_breaker_failfast / test_business, which stub the driver methods and
confirm the delegators preserve the surface. This file guards the wiring:
no method is dropped, and the client talks to the driver (not to itself)
for transport and token state.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.backend_client import TOKEN_TTL_SECONDS, BackendClient


def _make_client():
    """A BackendClient backed by a mock driver with the attributes/seam the
    client reaches through. The driver's JS evaluators default to AsyncMocks
    so individual tests override only what they assert on."""
    driver = MagicMock()
    driver._access_token = "tok"
    driver._user_name = "tester"
    driver._token_fetched_at = 0.0
    driver._last_refresh_attempt_at = 0.0
    driver._current_conv_id = None
    driver._breakers = None
    driver._js = AsyncMock(return_value="")
    driver._js_strict = AsyncMock(return_value="")
    driver._js_with_data = AsyncMock(return_value="")
    driver._js_with_data_strict = AsyncMock(return_value="")
    driver.ensure_token = AsyncMock(return_value="tok")
    driver._refresh_token = AsyncMock()
    return BackendClient(driver), driver


# ── 1. Every moved method exists on BackendClient ─────────────────────


def test_backend_client_has_all_moved_methods():
    """The extraction must not drop a method. Each of these is delegated by
    CDPDriver and must live on BackendClient."""
    client, _ = _make_client()
    expected = [
        # token / session
        "_refresh_token",
        "ensure_token",
        "recover_auth",
        # conversation fetch (A2 anchored fetchers + id resolution)
        "_fetch_recent_conversation_projection",
        "_fetch_text_for_turn",
        "_fetch_end_turn_for_turn",
        "_conversation_id_from_url",
        "_get_live_conversation_id_best_effort",
        "_check_auth_in_raw",
        # backend-api read/mutate
        "get_models",
        "get_projects",
        "get_conversations",
        "get_conversation",
        "delete_conversation",
        "rename_conversation",
        "create_project",
        "update_project_instructions",
        "get_project_detail",
        "archive_conversation",
        "get_memories",
        "create_memory",
        "delete_memory",
        "delete_project",
        "list_gpts",
        "get_project_files",
    ]
    missing = [name for name in expected if not callable(getattr(client, name, None))]
    assert missing == [], f"BackendClient is missing moved methods: {missing}"


def test_token_ttl_seconds_moved():
    """TOKEN_TTL_SECONDS canonical home is now backend_client.py."""
    assert TOKEN_TTL_SECONDS == 3600


# ── 2. Transport seam: client reaches driver._js*, not a local copy ──


@pytest.mark.asyncio
async def test_get_models_reaches_driver_js_with_data_strict():
    client, driver = _make_client()
    driver._js_with_data_strict = AsyncMock(
        return_value=json.dumps({"models": [{"slug": "auto"}]})
    )
    result = await client.get_models()
    driver._js_with_data_strict.assert_awaited_once()
    assert result == [{"slug": "auto"}]


@pytest.mark.asyncio
async def test_conversation_id_from_url_reaches_driver_js_strict():
    client, driver = _make_client()
    driver._js_strict = AsyncMock(return_value="https://chatgpt.com/c/abc-123")
    result = await client._conversation_id_from_url()
    driver._js_strict.assert_awaited_once()
    assert result == "abc-123"


# ── 3. Token seam: ensure_token reaches driver._refresh_token ────────


@pytest.mark.asyncio
async def test_ensure_token_refreshes_through_driver_when_stale():
    """ensure_token must call driver._refresh_token (the driver delegator),
    not the client's own _refresh_token, so driver monkeypatches intercept.
    Token is empty → stale → refresh called."""
    client, driver = _make_client()
    driver._access_token = ""
    await client.ensure_token()
    driver._refresh_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_token_no_refresh_when_fresh():
    client, driver = _make_client()
    import time

    driver._token_fetched_at = time.time()
    driver._access_token = "fresh"
    await client.ensure_token()
    driver._refresh_token.assert_not_awaited()


# ── 4. State stays on the driver (not migrated into the client) ──────


def test_backend_client_holds_no_token_state():
    """The client must NOT own _access_token / _user_name / _breakers — those
    stay on the driver so external attribute reads keep working."""
    client, _ = _make_client()
    for attr in ("_access_token", "_user_name", "_token_fetched_at", "_breakers", "_current_conv_id"):
        assert not hasattr(client, attr), (
            f"BackendClient must not own driver state '{attr}' (Phase 5 PR1 contract)"
        )


# ── 5. _check_auth_in_raw trips the driver's breaker registry ───────


def test_check_auth_in_raw_trips_driver_breaker(monkeypatch):
    """On a login-page body, _check_auth_in_raw trips AUTH_EXPIRED on the
    DRIVER's breaker registry and raises AuthExpiredError."""
    from chatgpt_web2api.breakers import BreakerKind, BreakerRegistry
    from chatgpt_web2api.cdp_driver import AuthExpiredError

    client, driver = _make_client()
    driver._breakers = BreakerRegistry()
    login_html = "<html><body>Sign in to ChatGPT</body></html>"
    with pytest.raises(AuthExpiredError):
        client._check_auth_in_raw(login_html)
    assert driver._breakers.is_open(BreakerKind.AUTH_EXPIRED)


def test_check_auth_in_raw_noop_on_normal_body():
    client, _ = _make_client()
    # A normal JSON body must not raise.
    client._check_auth_in_raw('{"models": []}')


# ── 6. Integration: CDPDriver wires BackendClient in __init__ ────────


def test_driver_wires_backend_client():
    """CDPDriver.__init__ constructs a BackendClient and delegates through it."""
    from chatgpt_web2api.backend_client import BackendClient
    from chatgpt_web2api.cdp_driver import CDPDriver

    d = CDPDriver(cdp_port=9222)
    assert isinstance(d._backend_client, BackendClient)
    assert d._backend_client._driver is d


def test_driver_keeps_token_ttl_seconds_reexport():
    """cdp_driver re-exports TOKEN_TTL_SECONDS from backend_client."""
    from chatgpt_web2api import cdp_driver

    assert cdp_driver.TOKEN_TTL_SECONDS == 3600


# ── 7. create_memory verification uses the driver-facing get_memories seam ─


@pytest.mark.asyncio
async def test_create_memory_uses_driver_get_memories_for_verification():
    """Regression: BackendClient.create_memory must verify via
    driver.get_memories (the caller-facing seam that carries @diagnose and is
    interceptable by driver monkeypatches), NOT via self.get_memories (the
    client-internal method). Pre-extraction this resolved through the driver
    method; the extraction must not bypass it."""
    from chatgpt_web2api.cdp_driver import StreamChunk

    client, driver = _make_client()

    # driver.navigate_new_chat: no-op
    driver.navigate_new_chat = AsyncMock()
    # driver.send_and_stream: yield a small response chunk
    async def _stream(*a, **kw):
        yield StreamChunk(delta="ok", finish_reason=None)
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream
    # driver.get_memories: returns a memory matching the content prefix
    driver.get_memories = AsyncMock(return_value=[{"content": "remember this please"}])

    result = await client.create_memory("remember this please")

    assert result["success"] is True
    driver.get_memories.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_memory_success_false_when_get_memories_no_match():
    """When driver.get_memories returns no matching memory, success is False.
    Guards the verification path stays wired through the driver seam."""
    from chatgpt_web2api.cdp_driver import StreamChunk

    client, driver = _make_client()
    driver.navigate_new_chat = AsyncMock()

    async def _stream(*a, **kw):
        yield StreamChunk(delta="ok", finish_reason="stop")

    driver.send_and_stream = _stream
    driver.get_memories = AsyncMock(return_value=[{"content": "something unrelated"}])

    result = await client.create_memory("remember this please")
    assert result["success"] is False
    driver.get_memories.assert_awaited_once()

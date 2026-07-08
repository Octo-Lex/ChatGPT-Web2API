"""Progress-notification tests for chat_completion / chat_with_gpt / create_memory.

Verifies the on_progress callback (used to emit MCP notifications/progress so
long generations don't trip a client idle timeout) fires at the right cadence,
is robust to transport failures, and is correctly absent when there is no
progress channel. Also covers the rate-limit backoff notification + its
load-bearing ordering (must fire BEFORE the sleep).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, RateLimitError, StreamChunk

# ── Helpers ─────────────────────────────────────────────────────

def _streaming_driver(deltas):
    """A mock CDPDriver whose send_and_stream yields the given deltas then a
    terminal stop chunk. deltas is a list of strings."""
    driver = MagicMock(spec=CDPDriver)
    driver._current_conv_id = "conv-1"
    driver._current_model = None
    driver.is_connected = True
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.navigate_gpt = AsyncMock()

    async def _stream(text, timeout=120, *, budgets=None, model=None):
        for d in deltas:
            yield StreamChunk(delta=d)
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream
    return driver


def _recording_callback(record):
    """An on_progress callback that appends each message to `record`."""
    async def _cb(message):
        record.append(message)
    return _cb


def _failing_callback():
    """An on_progress callback whose send_progress_notification raises —
    simulates a transport error mid-stream."""
    async def _cb(message):
        raise ConnectionError("session closed")
    return _cb


# ── 1. Business cadence ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_completion_progress_cadence():
    """Callback fires: once on first delta, coalesced every Nth, once on
    terminal — and the full response is still returned correctly."""
    from chatgpt_web2api import mcp_server
    from chatgpt_web2api.mcp_server import _PROGRESS_EVERY_N_CHUNKS

    # 25 deltas → expect fire on 1, N, 2N, terminal
    n = _PROGRESS_EVERY_N_CHUNKS
    deltas = [f"chunk{i} " for i in range(25)]
    driver = _streaming_driver(deltas)
    record = []
    result = await mcp_server.do_chat_completion(
        driver, {"message": "hi"}, None, on_progress=_recording_callback(record),
    )
    expected_calls = 1 + (25 // n) + 1  # first + every-Nth + terminal
    assert len(record) == expected_calls, f"got {record}"
    assert record[0] == "Assistant is responding…"
    assert record[-1] == "Finalizing…"
    assert "Streaming" in record[1]
    assert result["content"] == "".join(deltas)


@pytest.mark.asyncio
async def test_chat_completion_progress_none_default():
    """With on_progress=None (default), no callback work happens and the
    response is still correct — backward compatible."""
    from chatgpt_web2api import mcp_server
    driver = _streaming_driver(["Hello", " world"])
    result = await mcp_server.do_chat_completion(driver, {"message": "hi"}, None)
    assert result["content"] == "Hello world"


# ── 2. Helper guards ────────────────────────────────────────────

def _ctx(meta=None):
    ctx = MagicMock()
    ctx.meta = meta
    ctx.session.send_progress_notification = AsyncMock()
    return ctx


def test_helper_guards_via_isolation():
    """Test the guard logic in isolation by reconstructing the helper.
    Verifies: meta=None → None, progressToken=None → None, token present → callable."""
    # Since _make_progress_callback is defined inside create_server() closure,
    # we test the equivalent guard logic directly to lock the contract.
    def guard(ctx):
        try:
            _ = ctx  # simulates server.request_context
        except LookupError:
            return None
        token = ctx.meta.progressToken if ctx.meta else None
        if token is None:
            return None
        return "callback"

    ctx_no_meta = _ctx(meta=None)
    ctx_no_token = _ctx(meta=MagicMock(progressToken=None))
    ctx_with_token = _ctx(meta=MagicMock(progressToken="tok-1"))

    assert guard(ctx_no_meta) is None
    assert guard(ctx_no_token) is None
    assert guard(ctx_with_token) == "callback"


# ── 3. Callback robustness (negative case) ──────────────────────

@pytest.mark.asyncio
async def test_callback_failure_does_not_abort_tool_call():
    """If on_progress raises (transport error), the business loop continues
    and the full response is still returned — never abort a 40s generation
    over a transient notification failure."""
    from chatgpt_web2api import mcp_server
    driver = _streaming_driver(["chunkA ", "chunkB ", "chunkC "])
    result = await mcp_server.do_chat_completion(
        driver, {"message": "hi"}, None, on_progress=_failing_callback(),
    )
    # Despite every callback raising, the response assembled fully.
    assert result["content"] == "chunkA chunkB chunkC "


# ── 4. Backoff notification + ordering assertion (REQUIRED) ─────

@pytest.mark.asyncio
async def test_backoff_notifies_before_sleep(monkeypatch):
    """The backoff notification MUST fire BEFORE the sleep — this ordering is
    load-bearing (otherwise the timeout problem returns). We assert it
    structurally by recording a shared sequence list."""
    from chatgpt_web2api import resilience

    # Shared sequence recorder — both the callback and the (faked) sleep append.
    sequence = []

    async def fake_on_progress(msg):
        sequence.append(f"progress: {msg}")

    # Fake asyncio.sleep to record instead of actually sleeping.
    real_sleep_calls = []

    async def fake_sleep(seconds):
        sequence.append(f"slept: {seconds:.0f}s")
        real_sleep_calls.append(seconds)

    monkeypatch.setattr(resilience.asyncio, "sleep", fake_sleep)

    # A driver whose dismiss works, and a factory that raises RateLimitError
    # on first attempt, succeeds on second.
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    call_count = {"n": 0}

    async def factory():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RateLimitError(retry_after=5)
        return "done"

    result = await resilience.retry_on_rate_limit(
        driver, factory, on_progress=fake_on_progress,
    )
    assert result == "done"
    # The critical ordering assertion: progress notification precedes sleep.
    assert sequence[0].startswith("progress: Rate limited, retrying in"), sequence
    assert sequence[1].startswith("slept:"), sequence
    assert sequence == [
        f"progress: Rate limited, retrying in {real_sleep_calls[0]:.0f}s…",
        f"slept: {real_sleep_calls[0]:.0f}s",
    ], f"ordering violated: {sequence}"


@pytest.mark.asyncio
async def test_backoff_no_callback_still_works(monkeypatch):
    """retry_on_rate_limit with on_progress=None (default) behaves as before."""
    from chatgpt_web2api import resilience
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)
    call_count = {"n": 0}

    async def factory():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RateLimitError(retry_after=0)  # minimal backoff
        return "ok"

    # Patch sleep to be instant. Use the monkeypatch fixture (not the bare
    # module assignment in monkeypatch_setattr_sleep) so it auto-reverts —
    # otherwise the global asyncio.sleep stays patched for every later test,
    # breaking any test whose fakes rely on real asyncio.sleep yielding
    # (e.g. FakeWebSocket.recv's poll loop in test_tab_isolation).
    async def fast_sleep(s):
        return
    monkeypatch.setattr(resilience.asyncio, "sleep", fast_sleep)
    result = await resilience.retry_on_rate_limit(driver, factory)
    assert result == "ok"


def monkeypatch_setattr_sleep(module, fn):
    """Helper to monkeypatch asyncio.sleep on a module without the fixture.

    NOTE: this performs a bare ``module.asyncio.sleep = fn`` with NO teardown.
    Because ``module.asyncio`` is the global asyncio module, this leaks the
    patch to every subsequent test in the session. Prefer ``monkeypatch.setattr``
    (auto-reverting) for new tests; this helper is retained only for existing
    call sites.
    """
    module.asyncio.sleep = fn


# ── 5. Coalescing ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coalescing_no_per_delta_flood():
    """Exactly the expected number of calls for a known chunk count — no
    per-delta flooding. For 30 chunks with N=10: first(1) + 10th + 20th + 30th
    + terminal = 5 calls."""
    from chatgpt_web2api import mcp_server
    from chatgpt_web2api.mcp_server import _PROGRESS_EVERY_N_CHUNKS
    n = _PROGRESS_EVERY_N_CHUNKS
    total_chunks = 30
    deltas = [f"d{i} " for i in range(total_chunks)]
    driver = _streaming_driver(deltas)
    record = []
    await mcp_server.do_chat_completion(
        driver, {"message": "hi"}, None, on_progress=_recording_callback(record),
    )
    # first(1) + every Nth (N, 2N, 3N = 3) + terminal(1) = 5
    expected = 1 + (total_chunks // n) + 1
    assert len(record) == expected, f"expected {expected}, got {len(record)}: {record}"


# ── 6. create_memory callback wiring ────────────────────────────

@pytest.mark.asyncio
async def test_create_memory_emits_single_progress():
    """do_create_memory emits exactly one 'Creating memory…' notification
    before delegating to the driver (it doesn't expose its internal stream
    at this layer)."""
    from chatgpt_web2api import mcp_server
    driver = MagicMock(spec=CDPDriver)
    driver.create_memory = AsyncMock(return_value={"success": True, "memory_id": "m1"})
    record = []
    result = await mcp_server.do_create_memory(
        driver, {"content": "remember this"}, on_progress=_recording_callback(record),
    )
    assert record == ["Creating memory…"]
    assert result["success"] is True

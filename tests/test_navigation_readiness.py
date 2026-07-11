"""P2: Navigation staged readiness tests.

Replaces the opaque "ready composer" poll with a diagnosable staged probe.
Co-designed with ChatGPT (vision-alignment cycle, conversation 6a4ebb2a).

The probe splits the readiness check into stages:
  url_correct → document_ready → app_shell_present → composer_present

Each stage's result is captured so that when the poll fails, the error
message says WHICH stage failed (instead of the current opaque "did not
reach a ready composer within the timeout").

Also adds:
  - Fast-fail on URL displacement (nav_displaced error)
  - document.readyState check (don't fail while page is still loading)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, NavigationReadinessProbe


def _probe_payload(*, url="https://chatgpt.com/c/conv-123",
                   ready_state="complete", app_shell=True,
                   composer=True):
    """Build the JSON the staged probe JS returns."""
    return json.dumps({
        "url": url,
        "ready_state": ready_state,
        "app_shell": app_shell,
        "composer": composer,
    })


# ── NavigationReadinessProbe ─────────────────────────────────────────────


class TestNavigationReadinessProbe:
    """The probe dataclass carries all stage results for diagnostics."""

    def test_all_stages_pass_when_everything_ready(self):
        probe = NavigationReadinessProbe(
            url="https://chatgpt.com/c/conv-123",
            ready_state="complete",
            app_shell_present=True,
            composer_present=True,
        )
        assert probe.is_ready(url_correct=True) is True

    def test_not_ready_when_composer_missing(self):
        """The key P2 case: URL correct, document loaded, app shell present,
        but composer selector didn't match (React still hydrating)."""
        probe = NavigationReadinessProbe(
            url="https://chatgpt.com/c/conv-123",
            ready_state="complete",
            app_shell_present=True,
            composer_present=False,
        )
        assert probe.is_ready(url_correct=True) is False

    def test_not_ready_when_url_wrong(self):
        probe = NavigationReadinessProbe(
            url="https://chatgpt.com/c/WRONG",
            ready_state="complete",
            app_shell_present=True,
            composer_present=True,
        )
        assert probe.is_ready(url_correct=False) is False

    def test_not_ready_when_document_loading(self):
        """Page still loading — don't fail, just report not ready."""
        probe = NavigationReadinessProbe(
            url="https://chatgpt.com/c/conv-123",
            ready_state="loading",
            app_shell_present=False,
            composer_present=False,
        )
        assert probe.is_ready(url_correct=True) is False

    def test_diagnostic_summary_includes_failed_stage(self):
        """The probe can describe WHICH stage failed for the error message."""
        probe = NavigationReadinessProbe(
            url="https://chatgpt.com/c/conv-123",
            ready_state="complete",
            app_shell_present=True,
            composer_present=False,
        )
        summary = probe.diagnostic_summary(url_correct=True)
        assert "composer" in summary.lower(), (
            f"diagnostic summary should name the failed stage, got: {summary}"
        )

    def test_diagnostic_summary_when_url_displaced(self):
        """When the URL moved away from target, the summary names displacement."""
        probe = NavigationReadinessProbe(
            url="https://chatgpt.com/c/DIFFERENT",
            ready_state="complete",
            app_shell_present=True,
            composer_present=True,
        )
        summary = probe.diagnostic_summary(url_correct=False)
        assert "url" in summary.lower() or "displac" in summary.lower(), (
            f"diagnostic summary should flag URL mismatch, got: {summary}"
        )


# ── navigate_conversation uses the staged probe ──────────────────────────


def _make_driver():
    """A CDPDriver with a mock transport for testing."""
    driver = MagicMock(spec=CDPDriver)
    driver._cdp = AsyncMock()
    driver._js_strict = AsyncMock(return_value="{}")
    driver._js = AsyncMock(return_value="")
    driver._current_conv_id = None
    driver._is_url_at_conversation = CDPDriver._is_url_at_conversation
    return driver


@pytest.mark.asyncio
async def test_navigate_succeeds_when_all_stages_pass(monkeypatch):
    """When all readiness stages pass, navigate_conversation completes
    without raising and sets _current_conv_id."""
    driver = CDPDriver(cdp_port=9222)
    driver._cdp = AsyncMock()
    driver._js_strict = AsyncMock(return_value=_probe_payload())

    await driver.navigate_conversation("conv-123")
    assert driver._current_conv_id == "conv-123"


@pytest.mark.asyncio
async def test_navigate_fails_with_diagnostic_on_composer_missing(monkeypatch):
    """When the composer is never found but everything else loads, the error
    message names the failed stage ('composer'), not just 'timeout'."""
    import time

    driver = CDPDriver(cdp_port=9222)
    driver._cdp = AsyncMock()
    # URL correct, doc loaded, app shell present, but composer never appears.
    driver._js_strict = AsyncMock(return_value=_probe_payload(composer=False))

    t = [0.0]

    _real_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await _real_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await driver.navigate_conversation("conv-123")

    msg = str(exc_info.value)
    assert "composer" in msg.lower(), (
        f"error should name the failed stage 'composer', got: {msg}"
    )


@pytest.mark.asyncio
async def test_navigate_fast_fails_on_url_displacement(monkeypatch):
    """If the URL moves AWAY from the target mid-poll, fail fast with a
    distinct error (nav_displaced), don't burn the full 15s."""
    import time

    driver = CDPDriver(cdp_port=9222)
    driver._cdp = AsyncMock()

    # First poll: URL is correct (still loading). Then 2 displaced polls
    # (debounce requires 2 consecutive wrong polls per ChatGPT review finding B).
    polls = [
        _probe_payload(url="https://chatgpt.com/c/conv-123", ready_state="loading",
                       app_shell=False, composer=False),
        _probe_payload(url="https://chatgpt.com/c/DIFFERENT",
                       ready_state="complete", app_shell=True, composer=True),
        _probe_payload(url="https://chatgpt.com/c/DIFFERENT",
                       ready_state="complete", app_shell=True, composer=True),
    ]
    call_count = {"n": 0}

    async def fake_js(expr, timeout=15):
        idx = min(call_count["n"], len(polls) - 1)
        call_count["n"] += 1
        return polls[idx]

    driver._js_strict = fake_js

    t = [0.0]

    _real_sleep = asyncio.sleep

    async def fast_sleep(d):
        t[0] += d
        await _real_sleep(0)

    monkeypatch.setattr(time, "monotonic", lambda: t[0])
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await driver.navigate_conversation("conv-123")

    msg = str(exc_info.value)
    assert "displac" in msg.lower(), (
        f"error should mention displacement, got: {msg}"
    )
    # Should NOT have burned the full 15s — fast-fail.
    assert t[0] < 15, f"fast-fail should not wait 15s, got t={t[0]}"


@pytest.mark.asyncio
async def test_navigate_waits_for_document_ready(monkeypatch):
    """When readyState='loading', the poll should wait, not count against
    the stall budget. This tests the document.readyState check."""

    driver = CDPDriver(cdp_port=9222)
    driver._cdp = AsyncMock()

    # First 3 polls: loading. Then: complete + composer present.
    loading = _probe_payload(ready_state="loading", app_shell=False, composer=False)
    ready = _probe_payload(ready_state="complete", app_shell=True, composer=True)
    polls = [loading, loading, loading, ready]
    call_count = {"n": 0}

    async def fake_js(expr, timeout=15):
        idx = min(call_count["n"], len(polls) - 1)
        call_count["n"] += 1
        return polls[idx]

    driver._js_strict = fake_js

    await driver.navigate_conversation("conv-123")
    assert driver._current_conv_id == "conv-123"
    # Should have polled at least 4 times (3 loading + 1 ready).
    assert call_count["n"] >= 4

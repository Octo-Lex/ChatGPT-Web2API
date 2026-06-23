"""Live completion smoke-pin (R5).

A tiny, fast, opt-in e2e that sends a nonce and asserts the bridge detects
COMPLETION within a SHORT timeout (30s) — far below the 90s stall ceiling.
This is the test the Phase-2 completion bug would have failed instantly: the
answer was in the DOM but has_action never fired, so every send stalled to
90s and raised GenerationStuckError. Pinning completion-under-a-short-timeout
makes selector drift a loud, fast CI failure instead of a slow stall.

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_completion.py -m e2e -v
"""

import secrets
import time

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, GenerationStuckError
from chatgpt_web2api.config import Config

pytestmark = pytest.mark.e2e

# Short ceiling: a nonce reply completes in a few seconds on a healthy bridge.
# 30s leaves headroom for cold-tab startup + reasoning models, but is far
# below the 90s stall window — so a completion-detection regression fails
# here loudly instead of silently stalling.
COMPLETION_TIMEOUT = 30


async def test_completion_detected_under_short_timeout(
    e2e_driver: CDPDriver, e2e_app_config: Config, e2e_created: dict
):
    """Send a nonce, assert the bridge detects completion within 30s.

    This is the smoke-pin for the Phase-2 completion detector. The Phase-2
    has_action bug (action buttons in a sibling container, selector queried
    descendants only) would have failed here: GenerationStuckError raised
    because completion was never detected, even though the full answer sat in
    the DOM. A green run means the DOM completion signal (or the end_turn
    backend fallback) is working against the live site.
    """
    nonce = f"SMOKE{secrets.token_hex(3).upper()}"
    await e2e_driver.navigate_new_chat()

    t0 = time.monotonic()
    full = ""
    try:
        async for chunk in e2e_driver.send_and_stream(
            f"Reply with exactly this token and nothing else: {nonce}",
            timeout=COMPLETION_TIMEOUT,
        ):
            if chunk.delta:
                full += chunk.delta
    except GenerationStuckError as e:
        elapsed = time.monotonic() - t0
        pytest.fail(
            f"Completion NOT detected within {COMPLETION_TIMEOUT}s (stalled at "
            f"{elapsed:.0f}s). The Phase-2 completion signal is broken against "
            f"the live DOM — exactly what this smoke-pin guards against. "
            f"Error: {e}"
        )

    elapsed = time.monotonic() - t0
    # Register the created conversation for session cleanup.
    if e2e_driver._current_conv_id:
        e2e_created["conversations"].add(e2e_driver._current_conv_id)

    # The nonce must appear in the response (proves the send worked AND the
    # completion returned real content, not an empty/truncated stream).
    assert nonce in full, (
        f"Nonce {nonce!r} not in response (got {full[:100]!r}). Send worked "
        f"but the streamed text is wrong/empty — completion returned too early."
    )
    # And it must have completed in well under the stall ceiling.
    assert elapsed < COMPLETION_TIMEOUT, (
        f"Took {elapsed:.0f}s to complete — over the {COMPLETION_TIMEOUT}s "
        f"smoke ceiling. Completion detection is too slow."
    )

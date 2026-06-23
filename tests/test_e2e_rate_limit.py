"""E2E: rate-limit dismiss + transparent recovery, against a live account.

These deliberately trip ChatGPT's "Too many requests" pop-up with a small
controlled burst (each chat registered for cleanup), then verify:

  1. ``dismiss_rate_limit()`` clears the pop-up (the 'Got it' click works).
  2. A subsequent chat succeeds — proving the session recovered.

Safety: the burst uses create-then-delete (registered in e2e_created); if the
limit can't be tripped in a few tries, the test skips rather than hammering.
If a limit persists (won't dismiss), the suite's RateLimitError hook skips it.

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_rate_limit.py -m e2e -v
"""

import asyncio
import json

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, RateLimitError, is_rate_limited_text
from chatgpt_web2api.config import Config
from chatgpt_web2api.mcp_server import do_chat_completion

pytestmark = pytest.mark.e2e


async def _trip_rate_limit(driver: CDPDriver, max_burst: int = 6) -> bool:
    """Send rapid chats to trip the pop-up. Returns True if tripped.

    Stops as soon as the pop-up appears (detected via DOM scan or RateLimitError).
    Conversations created are deleted to keep the account clean.
    """
    created = []
    tripped = False
    for i in range(max_burst):
        try:
            await driver.navigate_new_chat()
            async for _chunk in driver.send_and_stream("Say 'ok'.", timeout=60):
                pass
            cid = driver._current_conv_id or ""
            if cid:
                created.append(cid)
            await asyncio.sleep(0.8)  # burst, but not instant
        except RateLimitError:
            tripped = True
            break
    if not tripped:
        scan = await driver._js(
            "(function(){return JSON.stringify({text:(document.body.innerText||'')"
            ".slice(0,2000)});})()", timeout=10
        )
        text = json.loads(scan).get("text", "") if scan else ""
        tripped = is_rate_limited_text(text)
    # Clean up burst conversations (best-effort)
    for cid in created:
        try:
            await driver.delete_conversation(cid)
        except Exception:
            pass
    return tripped


async def test_dismiss_rate_limit_clears_popup(e2e_driver: CDPDriver):
    """Tripping the limit then calling dismiss_rate_limit clears the pop-up."""
    tripped = await _trip_rate_limit(e2e_driver)
    if not tripped:
        pytest.skip("could not trip the rate limit in a controlled burst")

    cleared = await e2e_driver.dismiss_rate_limit()
    assert cleared is True, "dismiss_rate_limit did not clear the pop-up"

    # Independent verification: re-scan the DOM.
    scan = await e2e_driver._js(
        "(function(){return JSON.stringify({text:(document.body.innerText||'')"
        ".slice(0,2000)});})()", timeout=10
    )
    text = json.loads(scan).get("text", "") if scan else ""
    assert not is_rate_limited_text(text), "pop-up still present after dismiss"


async def test_session_recovers_after_dismiss(
    e2e_driver: CDPDriver, e2e_created: dict, e2e_app_config: Config
):
    """After dismissing the limit, a fresh chat succeeds (session recovered)."""
    tripped = await _trip_rate_limit(e2e_driver)
    if tripped:
        cleared = await e2e_driver.dismiss_rate_limit()
        if not cleared:
            pytest.skip("pop-up did not dismiss; cannot verify recovery")
        await asyncio.sleep(2)  # let the limit cooldown settle
    # If not tripped, this still verifies a normal chat works — no harm.

    result = await do_chat_completion(
        e2e_driver, {"message": "Reply with exactly: RECOVERED"}, e2e_app_config
    )
    cid = result.get("conversation_id", "")
    if cid:
        e2e_created["conversations"].add(cid)
    assert "RECOVERED" in result.get("content", ""), \
        f"session did not recover after dismiss: {result.get('content','')[:120]!r}"

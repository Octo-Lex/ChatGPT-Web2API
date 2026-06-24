"""Live multi-line send regression.

The composer-text verifier read ``innerText``/``textContent`` to confirm the
typed prompt landed. For multi-line input, ProseMirror splits each line into a
separate ``<p>`` block; ``innerText`` then emits several newlines per block
boundary (measured: a 2-newline input read back as 5) and ``textContent``
emits none — so every multi-line prompt failed canonical equality and raised
``Composer text verification failed after retry``. Unit mocks can't catch this
(the extractor runs in JS inside the page). This opt-in e2e sends a
multi-line prompt and asserts it lands, pinning the block-aware extractor.

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_multiline.py -m e2e -v
"""

import secrets
import time

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, GenerationStuckError
from chatgpt_web2api.config import Config

pytestmark = pytest.mark.e2e

COMPLETION_TIMEOUT = 45


async def test_multiline_prompt_verifies_and_completes(
    e2e_driver: CDPDriver, e2e_app_config: Config, e2e_created: dict
):
    """A multi-line prompt (containing \\n\\n) must pass composer
    verification and complete. The pre-fix verifier raised before the send
    ever reached the network."""
    nonce = f"MLN{secrets.token_hex(3).upper()}"
    # Deliberately multi-line: two blank-line-separated paragraphs plus a
    # third line. Pre-fix, this raised "verification failed after retry".
    prompt = (
        f"First paragraph mentions token {nonce}.\n\n"
        "Second paragraph is here.\n"
        "Third line follows the second with a single newline."
    )
    await e2e_driver.navigate_new_chat()

    t0 = time.monotonic()
    full = ""
    try:
        async for chunk in e2e_driver.send_and_stream(
            prompt, timeout=COMPLETION_TIMEOUT
        ):
            if chunk.delta:
                full += chunk.delta
    except GenerationStuckError as e:
        pytest.fail(
            f"Multi-line send stalled (not completed in {COMPLETION_TIMEOUT}s). "
            f"Either verification or completion regressed. Error: {e}"
        )
    elapsed = time.monotonic() - t0

    if e2e_driver._current_conv_id:
        e2e_created["conversations"].add(e2e_driver._current_conv_id)

    # The nonce proves the multi-line prompt actually reached ChatGPT and the
    # response is real (not a stale/empty stream).
    assert nonce in full, (
        f"Nonce {nonce!r} absent from response (got {full[:120]!r}). The "
        f"multi-line prompt did not land correctly."
    )
    assert elapsed < COMPLETION_TIMEOUT

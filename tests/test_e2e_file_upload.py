from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.config import Config


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_file_upload_reaches_chatgpt(
    e2e_driver: CDPDriver, e2e_config: Config, e2e_created: dict, tmp_path: Path
):
    path = tmp_path / f"w2a-upload-{uuid.uuid4().hex[:8]}.log"
    path.write_text("W2A_UPLOAD_SENTINEL_7f3d", encoding="utf-8")
    await e2e_driver.navigate_new_chat()
    await e2e_driver.upload_files([path])

    prompt = "Read the attached file and reply with exactly: W2A_UPLOAD_RECEIVED"
    response = ""
    async for chunk in e2e_driver.send_and_stream(prompt, timeout=120):
        response += chunk.delta
    if e2e_driver._current_conv_id:
        e2e_created["conversations"].add(e2e_driver._current_conv_id)

    assert "W2A_UPLOAD_RECEIVED" in response

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.file_upload import UploadError, validate_upload_files


def test_validate_upload_files_accepts_log_zip_and_image(tmp_path: Path):
    files = []
    for name, payload in (("server.log", b"line 1\n"), ("bundle.zip", b"PK"), ("screen.png", b"png")):
        path = tmp_path / name
        path.write_bytes(payload)
        files.append(path)

    result = validate_upload_files(files)

    assert result == [path.resolve() for path in files]


def test_validate_upload_files_rejects_missing_and_directories(tmp_path: Path):
    with pytest.raises(UploadError, match="does not exist"):
        validate_upload_files([tmp_path / "missing.log"])
    with pytest.raises(UploadError, match="not a regular file"):
        validate_upload_files([tmp_path])


def test_validate_upload_files_enforces_configured_size_limit(tmp_path: Path, monkeypatch):
    path = tmp_path / "large.log"
    path.write_bytes(b"12345")
    monkeypatch.setenv("W2A_MAX_UPLOAD_BYTES", "4")

    with pytest.raises(UploadError, match="exceeds maximum"):
        validate_upload_files([path])


@pytest.mark.asyncio
async def test_mcp_chat_completion_uploads_files_before_send(tmp_path: Path):
    path = tmp_path / "diagnostic.log"
    path.write_text("failure")

    from chatgpt_web2api.cdp_driver import StreamChunk
    from chatgpt_web2api.mcp_server import do_chat_completion

    class Driver:
        _current_conv_id = None
        uploaded = None

        async def navigate_new_chat(self, gizmo_id=None):
            return None

        async def upload_files(self, paths):
            self.uploaded = paths

        async def send_and_stream(self, *args, **kwargs):
            assert self.uploaded == [str(path)]
            yield StreamChunk(delta="ok")
            yield StreamChunk(delta="", finish_reason="stop")

    result = await do_chat_completion(Driver(), {"message": "inspect", "files": [str(path)]}, None)

    assert result["content"] == "ok"


@pytest.mark.asyncio
async def test_upload_files_sets_file_input_via_cdp(tmp_path: Path):
    path = tmp_path / "server.log"
    path.write_text("hello")
    driver = object.__new__(type("Driver", (), {}))
    driver._js = AsyncMock(
        side_effect=["clicked", "events-dispatched", "ready", "already-closed"]
    )
    driver._cdp = AsyncMock(
        side_effect=[
            {"result": {"root": {"nodeId": 7}}},
            {"result": {"nodeId": 11}},
            {},
            {},
            {},
            {},
        ]
    )

    from chatgpt_web2api.file_upload import upload_files_to_composer

    await upload_files_to_composer(driver, [path])

    assert driver._cdp.await_args_list[0].args[0] == "DOM.getDocument"
    assert driver._cdp.await_args_list[1].args[0] == "DOM.querySelector"
    assert driver._cdp.await_args_list[2].args[0] == "DOM.setFileInputFiles"
    assert driver._cdp.await_args_list[2].args[1] == {"nodeId": 11, "files": [str(path.resolve())]}


@pytest.mark.asyncio
async def test_rest_multipart_parser_materializes_and_validates_file(tmp_path: Path):
    from chatgpt_web2api.api_server import APIServer

    class Part:
        def __init__(self, name, *, filename=None, payload=b"", text_value=""):
            self.name = name
            self.filename = filename
            self._payload = payload
            self._text = text_value
            self._read = False

        async def read_chunk(self):
            if self._read:
                return b""
            self._read = True
            return self._payload

        async def text(self):
            return self._text

    class Reader:
        def __init__(self, parts):
            self.parts = iter(parts)

        async def next(self):
            return next(self.parts, None)

    class Request:
        content_type = "multipart/form-data"

        async def multipart(self):
            return Reader([
                Part("messages", text_value='[{"role":"user","content":"inspect"}]'),
                Part("file", filename="server.log", payload=b"line 1\n"),
            ])

    server = object.__new__(APIServer)
    body, paths = await server._parse_chat_request(Request())
    try:
        assert body == {"messages": [{"role": "user", "content": "inspect"}]}
        assert len(paths) == 1
        assert paths[0].read_bytes() == b"line 1\n"
    finally:
        server._cleanup_uploads(paths)

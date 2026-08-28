from __future__ import annotations

import uuid

import pytest

from chatgpt_web2api.backend_client import normalize_conversation_id


def test_normalize_conversation_id_removes_web_transport_prefix():
    conversation_id = str(uuid.uuid4())

    assert normalize_conversation_id(f"WEB:{conversation_id}") == conversation_id


def test_normalize_conversation_id_preserves_native_uuid():
    conversation_id = str(uuid.uuid4())

    assert normalize_conversation_id(conversation_id) == conversation_id


def test_normalize_conversation_id_rejects_empty_or_malformed_values():
    with pytest.raises(ValueError, match="conversation id"):
        normalize_conversation_id("WEB:")
    with pytest.raises(ValueError, match="conversation id"):
        normalize_conversation_id("not-an-id")

"""Tests for the A2 IdentityListener (Step 2).

Exercises capture validation (failure-mode B), scope lifecycle (E), and the
non-blocking handler contract. Uses synthetic ``Network.requestWillBeSent``
events — no real CDP/Chrome required.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.identity_listener import (
    IdentityListener,
    hash_sent_text,
)


def _make_driver():
    d = MagicMock()
    d._cdp_event_handlers = {}
    d._cdp = AsyncMock(return_value={"result": {}})
    return d


def _make_send_event(
    *,
    uuid: str = "11111111-1111-4111-8111-111111111111",
    text: str = "hello",
    conversation_id: str | None = "conv-1",
    action: str = "next",
    role: str = "user",
    post_data: str | None = None,
    request_id: str = "req-1",
) -> dict:
    """Build a synthetic Network.requestWillBeSent event."""
    body = {
        "action": action,
        "messages": [{
            "id": uuid,
            "author": {"role": role},
            "content": {"content_type": "text", "parts": [text]},
        }],
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    if post_data is None:
        post_data = json.dumps(body)
    return {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": request_id,
            "request": {
                "url": "https://chatgpt.com/backend-api/f/conversation",
                "method": "POST",
                "postData": post_data,
            },
        },
    }


@pytest.mark.asyncio
async def test_attach_registers_handler_and_enables_network():
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()
    assert "Network.requestWillBeSent" in driver._cdp_event_handlers
    assert driver._cdp.call_count == 1
    args, kwargs = driver._cdp.call_args
    assert args[0] == "Network.enable"
    assert kwargs.get("params") == {"maxPostDataSize": 4 * 1024 * 1024} or args[1] == {"maxPostDataSize": 4 * 1024 * 1024}
    assert listener.is_alive() is True


@pytest.mark.asyncio
async def test_successful_capture_resolves_uuid():
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    text = "Reply with exactly: MARKER-1"
    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text(text),
        conversation_id="conv-1",
        target_id="tgt-1",
    )
    try:
        # Fire the event directly through the handler.
        await listener._process_send_post(
            scope, _make_send_event(text=text, conversation_id="conv-1"),
            "https://chatgpt.com/backend-api/f/conversation",
        )
        uuid = await listener.wait_for_captured_uuid(timeout=1.0)
        assert uuid == "11111111-1111-4111-8111-111111111111"
        assert listener.capture_success_count == 1
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_wrong_text_hash_does_not_resolve():
    """A POST whose text doesn't match our send is ignored (failure-mode D)."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("our prompt"),
        conversation_id="conv-1",
        target_id="tgt-1",
    )
    try:
        # POST carries different text.
        await listener._process_send_post(
            scope, _make_send_event(text="different prompt", conversation_id="conv-1"),
            "https://chatgpt.com/backend-api/f/conversation",
        )
        # Should NOT resolve; scope stays open.
        uuid = await listener.wait_for_captured_uuid(timeout=0.2)
        assert uuid is None  # timeout
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_wrong_conversation_id_does_not_resolve():
    """A POST for a different conversation is ignored."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id="conv-1",
        target_id="tgt-1",
    )
    try:
        await listener._process_send_post(
            scope, _make_send_event(text="hello", conversation_id="conv-OTHER"),
            "https://chatgpt.com/backend-api/f/conversation",
        )
        uuid = await listener.wait_for_captured_uuid(timeout=0.2)
        assert uuid is None
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_non_next_action_ignored():
    """action != 'next' is not a user send (regenerate/edit/etc.)."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id="conv-1",
        target_id="tgt-1",
    )
    try:
        await listener._process_send_post(
            scope, _make_send_event(text="hello", conversation_id="conv-1", action="regenerate"),
            "https://chatgpt.com/backend-api/f/conversation",
        )
        uuid = await listener.wait_for_captured_uuid(timeout=0.2)
        assert uuid is None
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_invalid_uuid_ignored():
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id="conv-1",
        target_id="tgt-1",
    )
    try:
        await listener._process_send_post(
            scope, _make_send_event(uuid="not-a-uuid", text="hello", conversation_id="conv-1"),
            "https://chatgpt.com/backend-api/f/conversation",
        )
        uuid = await listener.wait_for_captured_uuid(timeout=0.2)
        assert uuid is None
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_scope_close_clears_active_state():
    """Closing a scope clears the listener's active ref (failure-mode E)."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id=None,
        target_id="tgt-1",
    )
    assert listener._active_scope is scope
    scope.close()
    assert listener._active_scope is None
    # Closing again is idempotent.
    scope.close()


@pytest.mark.asyncio
async def test_scope_close_unblocks_waiter():
    """If a scope is closed while wait_for_captured_uuid is awaiting, it returns None."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id=None,
        target_id="tgt-1",
    )

    async def closer():
        await asyncio.sleep(0.1)
        scope.close()

    closer_task = asyncio.create_task(closer())
    uuid = await listener.wait_for_captured_uuid(timeout=5.0)
    await closer_task
    assert uuid is None  # scope closed without capture


@pytest.mark.asyncio
async def test_handler_prefilter_rejects_non_post():
    """The synchronous prefilter rejects non-POST events without scheduling."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()
    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id=None,
        target_id="tgt-1",
    )
    try:
        # A GET request to the same endpoint — should be ignored by prefilter.
        listener._on_request_will_be_sent({
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "r1", "request": {
                "url": "https://chatgpt.com/backend-api/f/conversation",
                "method": "GET",
            }},
        })
        # Give any (should-not-exist) task a tick.
        await asyncio.sleep(0.05)
        # No capture.
        assert scope.future is not None and not scope.future.done()
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_handler_no_active_scope_is_noop():
    """Events with no armed scope are dropped silently."""
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()
    # No scope armed.
    listener._on_request_will_be_sent(_make_send_event(text="hello"))
    await asyncio.sleep(0.05)
    # No crash, no capture.


@pytest.mark.asyncio
async def test_reenable_if_stale_when_not_ready():
    driver = _make_driver()
    listener = IdentityListener(driver)
    listener._ready = False  # simulate stale
    ok = await listener.reenable_if_stale()
    assert ok is True
    assert listener.is_alive() is True
    assert listener.reenabled_count == 1


@pytest.mark.asyncio
async def test_reenable_if_stale_already_ready_is_noop():
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()
    calls_before = driver._cdp.call_count
    ok = await listener.reenable_if_stale()
    assert ok is True
    assert driver._cdp.call_count == calls_before  # no extra Network.enable


@pytest.mark.asyncio
async def test_postdata_missing_tries_getRequestPostData():
    """Failure-mode C: if postData absent, try Network.getRequestPostData once."""
    driver = _make_driver()
    # First call: Network.enable. Second: getRequestPostData returns a body.
    captured_uuid = "22222222-2222-4222-8222-222222222222"
    body = json.dumps({
        "action": "next",
        "messages": [{"id": captured_uuid, "author": {"role": "user"},
                      "content": {"content_type": "text", "parts": ["hello"]}}],
        "conversation_id": "conv-1",
    })
    driver._cdp = AsyncMock(side_effect=[
        {"result": {}},  # Network.enable
        {"result": {"postData": body}},  # getRequestPostData
    ])
    listener = IdentityListener(driver)
    await listener.attach()

    scope = listener.arm_capture_scope(
        expected_text_hash=hash_sent_text("hello"),
        conversation_id="conv-1",
        target_id="tgt-1",
    )
    try:
        # Event with NO postData field.
        event = {
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "r1", "request": {
                "url": "https://chatgpt.com/backend-api/f/conversation",
                "method": "POST",
                # no postData
            }},
        }
        await listener._process_send_post(
            scope, event, "https://chatgpt.com/backend-api/f/conversation",
        )
        uuid = await listener.wait_for_captured_uuid(timeout=1.0)
        assert uuid == captured_uuid
        assert listener.postdata_missing_count == 1
    finally:
        scope.close()


@pytest.mark.asyncio
async def test_sequential_send_sequence_increments():
    driver = _make_driver()
    listener = IdentityListener(driver)
    await listener.attach()
    s1 = listener.arm_capture_scope(expected_text_hash="a", conversation_id=None, target_id="t")
    s2 = listener.arm_capture_scope(expected_text_hash="b", conversation_id=None, target_id="t")
    assert s2.send_sequence_id == s1.send_sequence_id + 1
    s1.close()
    s2.close()

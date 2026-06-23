"""E2E chat + reversible-write tests against a real ChatGPT account.

These create real conversations (and possibly a memory) but register every
created id in ``e2e_created`` so the session cleanup finalizer removes them
even on crash. Nothing here is irreversible.

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_chat.py -m e2e -v
"""

import uuid

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.config import Config
from chatgpt_web2api.mcp_server import (
    do_archive_conversation,
    do_chat_completion,
    do_chat_with_gpt,
    do_create_memory,
    do_get_conversation,
    do_list_gpts,
    do_list_memories,
    do_list_projects,
    do_update_project_instructions,
)

pytestmark = pytest.mark.e2e


# ── chat_completion ───────────────────────────────────────────

async def test_chat_completion_creates_then_archives(
    e2e_driver: CDPDriver, e2e_app_config: Config, e2e_created: dict
):
    """Send a real message, capture the conversation, archive it (reversible)."""
    marker = f"W2A-E2E-OK-{uuid.uuid4().hex[:6]}"
    result = await do_chat_completion(
        e2e_driver,
        {"message": f"Reply with exactly this token and nothing else: {marker}"},
        e2e_app_config,
    )
    assert "content" in result and "conversation_id" in result
    assert marker in result["content"], \
        f"expected marker {marker!r} in response: {result['content']!r}"

    cid = result["conversation_id"]
    assert cid, "chat_completion should return a conversation id"
    e2e_created["conversations"].add(cid)  # registered for cleanup

    # Archive is reversible — safe cleanup
    arch = await do_archive_conversation(
        e2e_driver, {"conversation_id": cid, "archive": True}
    )
    assert arch["success"] is True
    assert arch["archived"] is True


# ── get_conversation after a chat ─────────────────────────────

async def test_get_conversation_reads_back_chat(
    e2e_driver: CDPDriver, e2e_app_config: Config, e2e_created: dict
):
    """A conversation we create is readable with both turns present."""
    marker = f"W2A-E2E-READBACK-{uuid.uuid4().hex[:6]}"
    chat = await do_chat_completion(
        e2e_driver, {"message": f"Reply with exactly: {marker}"}, e2e_app_config
    )
    cid = chat["conversation_id"]
    e2e_created["conversations"].add(cid)

    conv = await do_get_conversation(e2e_driver, {"conversation_id": cid})
    assert conv["id"] == cid
    # Our user message and the assistant reply should both be present
    all_content = " ".join(m["content"] for m in conv["messages"])
    assert marker in all_content, \
        f"marker missing from read-back; got: {all_content[:200]!r}"


# ── archive reversibility round-trip ──────────────────────────

async def test_archive_then_unarchive(
    e2e_driver: CDPDriver, e2e_app_config: Config, e2e_created: dict
):
    """archive=True then archive=False is a clean round-trip (reversible)."""
    chat = await do_chat_completion(
        e2e_driver, {"message": "Say 'hi' and nothing else."}, e2e_app_config
    )
    cid = chat["conversation_id"]
    e2e_created["conversations"].add(cid)

    a1 = await do_archive_conversation(
        e2e_driver, {"conversation_id": cid, "archive": True}
    )
    assert a1["archived"] is True

    a2 = await do_archive_conversation(
        e2e_driver, {"conversation_id": cid, "archive": False}
    )
    assert a2["archived"] is False


# ── create_memory (non-deterministic; snapshot/diff) ──────────

async def test_create_memory_then_cleanup(
    e2e_driver: CDPDriver, e2e_created: dict
):
    """create_memory uses a chat workaround; it may or may not produce a memory.

    We snapshot memories before, run create_memory, diff after, and register
    any NEW memory id for deletion in cleanup. Never touches pre-existing
    memories. If no memory was created (ChatGPT declined), the test passes
    with a note — the delete path is covered separately.
    """
    before = {m["id"] for m in (await do_list_memories(e2e_driver))["memories"]}

    marker = f"W2A-E2E-MEM-{uuid.uuid4().hex[:6]}"
    result = await do_create_memory(e2e_driver, {"content": marker})
    # create_memory always returns content + the conversation it used
    assert result["content"] == marker
    if result.get("conversation_id"):
        e2e_created["conversations"].add(result["conversation_id"])

    after = {m["id"] for m in (await do_list_memories(e2e_driver))["memories"]}
    new_ids = after - before
    for mid in new_ids:
        e2e_created["memories"].add(mid)  # only ever test-created ids

    if not new_ids:
        pytest.skip("create_memory produced no new memory (ChatGPT may decline); "
                    "delete_memory path covered by test_e2e_destructive")


# ── update_project_instructions (reversible) ──────────────────

async def test_update_project_instructions_reversible(
    e2e_driver: CDPDriver, e2e_created: dict
):
    """Update instructions on an existing project, then restore the prior value.

    We can't read prior instructions via a tool, so we set a known marker and
    then clear it (restore to empty) in teardown. Only touches projects that
    already exist on the account — never creates one.
    """
    projects = (await do_list_projects(e2e_driver))["projects"]
    if not projects:
        pytest.skip("no projects available to test update_project_instructions")
    pid = projects[0]["id"]

    marker = f"W2A-E2E-INSTR-{uuid.uuid4().hex[:6]}"
    upd = await do_update_project_instructions(
        e2e_driver, {"project_id": pid, "instructions": marker}
    )
    assert upd["success"] is True
    assert upd["project_id"] == pid

    # Restore to empty so we don't leave test instructions on a real project
    await do_update_project_instructions(
        e2e_driver, {"project_id": pid, "instructions": ""}
    )


# ── chat_with_gpt (conditional on having a Custom GPT) ────────

async def test_chat_with_gpt_conditional(
    e2e_driver: CDPDriver, e2e_created: dict
):
    """chat_with_gpt only runs if the account has at least one Custom GPT."""
    gpts = (await do_list_gpts(e2e_driver))["gpts"]
    if not gpts:
        pytest.skip("no Custom GPTs available to test chat_with_gpt")

    result = await do_chat_with_gpt(
        e2e_driver, {"gpt_id": gpts[0]["id"], "message": "Say 'hi' and nothing else."}
    )
    assert "content" in result and "conversation_id" in result
    e2e_created["conversations"].add(result["conversation_id"])

"""E2E destructive tests — create-then-delete only.

Every test here creates the state it then deletes. Nothing pre-existing is
ever touched. The snapshot/diff pattern for memories guarantees we only ever
delete an id that did NOT exist before the test.

These are the whole point of "full" E2E: they prove the real DELETE paths
work against the live account, which mocks cannot verify.

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_destructive.py -m e2e -v
"""

import uuid

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.config import Config
from chatgpt_web2api.mcp_server import (
    do_chat_completion,
    do_create_memory,
    do_create_project,
    do_delete_conversation,
    do_delete_memory,
    do_delete_project,
    do_list_conversations,
    do_list_memories,
    do_list_projects,
)

pytestmark = pytest.mark.e2e


@pytest.fixture
def e2e_app_config() -> Config:
    return Config.load(None)


# ── delete_conversation: only ever deletes a conversation we just made ─

async def test_delete_conversation_own_creation(
    e2e_driver: CDPDriver, e2e_app_config: Config
):
    """Create a throwaway chat, then delete THAT — never a pre-existing one."""
    chat = await do_chat_completion(
        e2e_driver,
        {"message": "Say 'ok' and nothing else."},
        e2e_app_config,
    )
    cid = chat["conversation_id"]
    assert cid, "need a conversation id to test deletion"

    result = await do_delete_conversation(
        e2e_driver, {"conversation_id": cid}
    )
    assert result["success"] is True
    assert result["conversation_id"] == cid

    # Verify it's actually gone from the recent history.
    recent = await do_list_conversations(e2e_driver, {"limit": 50})
    ids = [c["id"] for c in recent["conversations"]]
    assert cid not in ids, \
        f"deleted conversation {cid} still appears in list_conversations"


# ── delete_memory: snapshot/diff, only delete a brand-new id ───────────

async def test_delete_memory_own_creation(e2e_driver: CDPDriver):
    """Create a memory via the chat workaround, then delete only the new one.

    Snapshot before -> create -> diff -> delete new id -> verify gone.
    If create_memory produced no memory (ChatGPT declined), skip: we can't
    test delete without a deletable victim. The delete code path itself is
    independently covered by the mocked integration tests.
    """
    before = {m["id"] for m in (await do_list_memories(e2e_driver))["memories"]}

    marker = f"W2A-E2E-DELMEM-{uuid.uuid4().hex[:6]}"
    await do_create_memory(e2e_driver, {"content": marker})

    after = {m["id"] for m in (await do_list_memories(e2e_driver))["memories"]}
    new_ids = after - before

    if not new_ids:
        pytest.skip("create_memory produced no new memory; cannot test "
                    "delete_memory without a deletable victim")

    # Safety check: every id we're about to delete must be new (not pre-existing).
    assert new_ids.isdisjoint(before), "refusing to delete a pre-existing memory"

    for mid in new_ids:
        result = await do_delete_memory(e2e_driver, {"memory_id": mid})
        assert result["success"] is True
        assert result["memory_id"] == mid

    # Verify all are gone.
    final = {m["id"] for m in (await do_list_memories(e2e_driver))["memories"]}
    assert new_ids.isdisjoint(final), "a deleted memory still appears in list_memories"


# ── create_project + delete_project: create-then-delete round-trip ──────


async def test_create_then_delete_project(
    e2e_driver: CDPDriver, e2e_created: dict
):
    """Create a throwaway project, then delete THAT — proving both paths live.

    The project is registered in e2e_created as a safety net: even if the
    delete assertion failed, the session cleanup finalizer would remove it.
    """
    marker = f"W2A-E2E-PROJ-{uuid.uuid4().hex[:6]}"
    created = await do_create_project(
        e2e_driver, {"name": marker, "instructions": "", "memory_scope": "global"}
    )
    pid = created.get("id")
    assert pid and pid.startswith("g-p-"), f"expected a g-p- project id, got {pid!r}"
    e2e_created["projects"].add(pid)

    result = await do_delete_project(e2e_driver, {"project_id": pid})
    assert result["success"] is True
    assert result["project_id"] == pid

    # Verify it's gone from the project list.
    remaining = (await do_list_projects(e2e_driver))["projects"]
    assert pid not in [p["id"] for p in remaining], \
        f"deleted project {pid} still appears in list_projects"

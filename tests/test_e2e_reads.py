"""E2E read-only tests against a real ChatGPT account.

Zero account risk — every operation here is a GET. These establish that the
driver↔ChatGPT boundary works for all read tools, which is exactly what the
mocked unit tests cannot verify (and where the _js_with_data bug hid).

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_reads.py -m e2e -v
"""


import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.mcp_server import (
    do_get_conversation,
    do_list_conversations,
    do_list_gpts,
    do_list_memories,
    do_list_models,
    do_list_project_files,
    do_list_projects,
)

pytestmark = pytest.mark.e2e


# ── list_models ───────────────────────────────────────────────

async def test_list_models_returns_real_models(e2e_driver: CDPDriver):
    result = await do_list_models(e2e_driver)
    assert "models" in result
    models = result["models"]
    assert len(models) >= 1, "account should expose at least one model"
    # Each model has the documented shape
    for m in models:
        assert "id" in m and "title" in m, f"malformed model: {m}"
    # At least one OpenAI model slug is present (resilient to renaming)
    slugs = " ".join(m["id"] for m in models).lower()
    assert "gpt" in slugs or "auto" in slugs, f"no known model slug in {slugs}"


# ── list_conversations ────────────────────────────────────────

async def test_list_conversations_returns_history(e2e_driver: CDPDriver):
    result = await do_list_conversations(e2e_driver, {"limit": 5})
    convs = result["conversations"]
    assert len(convs) >= 1, "account should have conversation history"
    for c in convs:
        assert "id" in c and "title" in c, f"malformed conversation: {c}"


async def test_list_conversations_ids_are_uuids(e2e_driver: CDPDriver):
    result = await do_list_conversations(e2e_driver, {"limit": 3})
    for c in result["conversations"]:
        # ChatGPT conversation ids are UUIDs
        assert len(c["id"]) >= 8, f"suspiciously short id: {c['id']}"


# ── get_conversation (reads a real one) ───────────────────────

async def test_get_conversation_reads_messages(e2e_driver: CDPDriver):
    recent = await do_list_conversations(e2e_driver, {"limit": 1})
    convs = recent["conversations"]
    assert convs, "need at least one conversation to test get_conversation"
    cid = convs[0]["id"]

    result = await do_get_conversation(e2e_driver, {"conversation_id": cid})
    assert result["id"] == cid
    msgs = result["messages"]
    assert len(msgs) >= 1, "a conversation should have at least one message"
    for m in msgs:
        assert m["role"] in ("user", "assistant", "system", "tool"), \
            f"unexpected role: {m['role']}"
        assert isinstance(m["content"], str)


# ── list_projects ─────────────────────────────────────────────

async def test_list_projects_returns_real_projects(e2e_driver: CDPDriver):
    result = await do_list_projects(e2e_driver)
    projects = result["projects"]
    # An account may legitimately have zero projects; if there are some,
    # validate their shape and the g- id prefix.
    for p in projects:
        assert "id" in p and "name" in p and "memory_scope" in p, \
            f"malformed project: {p}"
        assert p["id"].startswith("g-"), f"project id should start with g-: {p['id']}"


# ── list_project_files (needs a real project) ─────────────────

async def test_list_project_files_reads_kb(e2e_driver: CDPDriver):
    result = await do_list_projects(e2e_driver)
    projects = result["projects"]
    if not projects:
        pytest.skip("no projects available to test list_project_files")
    pid = projects[0]["id"]

    files_result = await do_list_project_files(e2e_driver, {"project_id": pid})
    assert files_result["project_id"] == pid
    assert "files" in files_result
    for f in files_result["files"]:
        assert "id" in f and "name" in f, f"malformed file entry: {f}"


# ── list_memories ─────────────────────────────────────────────

async def test_list_memories_returns_shape(e2e_driver: CDPDriver):
    result = await do_list_memories(e2e_driver)
    memories = result["memories"]
    # Count may legitimately be 0 on a fresh account; validate shape only.
    for m in memories:
        assert "id" in m and "content" in m, f"malformed memory: {m}"


# ── list_gpts ─────────────────────────────────────────────────

async def test_list_gpts_returns_shape(e2e_driver: CDPDriver):
    result = await do_list_gpts(e2e_driver)
    gpts = result["gpts"]
    # A user may have no Custom GPTs; validate shape only.
    for g in gpts:
        assert "id" in g and "name" in g, f"malformed gpt: {g}"

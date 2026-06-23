"""Tests for the diagnostic detector + capture."""

from chatgpt_web2api.diagnostics import EXPECTED_SHAPES, classify_result


def test_classify_healthy_list():
    """A list[dict] result with the expected keys is healthy."""
    result = [{"slug": "gpt-5", "title": "GPT-5"}]
    healthy, mismatch = classify_result("get_models", result)
    assert healthy is True
    assert mismatch is None


def test_classify_broken_returns_error_shape():
    """A result that is an {'error': ...} dict is broken."""
    result = {"error": "HTTP 422", "body": "..."}
    healthy, mismatch = classify_result("create_project", result)
    assert healthy is False
    assert "error" in mismatch.lower()


def test_classify_broken_wrong_type():
    """A method promising list[dict] but returning a str is broken (the get_models bug)."""
    result = '{"models": [...]}'  # raw string, not parsed
    healthy, mismatch = classify_result("get_models", result)
    assert healthy is False
    assert "list" in mismatch.lower() or "type" in mismatch.lower()


def test_classify_broken_missing_required_key():
    """A dict result missing a required key is broken."""
    result = {"name": "Foo"}  # no id
    healthy, mismatch = classify_result("create_project", result)
    assert healthy is False
    assert "id" in mismatch


def test_expected_shapes_registry_covers_key_tools():
    """The registry must define shapes for the tools most likely to drift."""
    for fn in ("create_project", "get_models", "get_conversations", "get_memories"):
        assert fn in EXPECTED_SHAPES, f"{fn} missing from EXPECTED_SHAPES"


# ── semantic assertions (Phase A) ─────────────────────────────
# A shape check isn't enough: a result can match {id,name} yet be the wrong
# KIND of thing (e.g. create_project returning a gpt-type gizmo instead of a
# snorlax Project). Semantic assertions catch that.


def test_create_project_has_semantic_assertion_for_gizmo_type():
    """The registry must assert create_project produces a snorlax (Project) gizmo.

    This is the exact bug that surfaced during the create_project repair: the
    endpoint created a gpt-type Custom GPT, not a Project, but the {id,name}
    shape was identical so the classifier passed it. A semantic assertion on
    gizmo_type catches this class of drift.
    """
    spec = EXPECTED_SHAPES["create_project"]
    assertions = spec.get("assertions", [])
    gizmo_assertions = [a for a in assertions if a[0] == "gizmo_type"]
    assert gizmo_assertions, "create_project must assert gizmo_type"
    assert gizmo_assertions[0][1] == "snorlax", "must require snorlax (Project)"


def test_classify_flags_wrong_gizmo_type_with_otherwise_valid_shape():
    """A result with correct {id,name} but gizmo_type=gpt is broken (semantic)."""
    result = {"id": "g-abc", "name": "Foo", "gizmo_type": "gpt"}
    healthy, mismatch = classify_result("create_project", result)
    assert healthy is False
    assert "gizmo_type" in mismatch.lower() or "snorlax" in mismatch.lower()


def test_classify_passes_correct_gizmo_type():
    """A result with correct shape AND gizmo_type=snorlax is healthy."""
    result = {"id": "g-p-abc", "name": "Foo", "gizmo_type": "snorlax"}
    healthy, mismatch = classify_result("create_project", result)
    assert healthy is True
    assert mismatch is None


def test_classify_semantic_ignored_when_asserted_key_absent():
    """If the result lacks the asserted key entirely, don't crash — skip the check.

    This keeps the classifier graceful: a method that doesn't yet surface the
    asserted field isn't falsely flagged, just not semantically verified.
    (The method SHOULD surface it — but that's enforced by the shape check.)
    """
    # create_project normally includes gizmo_type now, but if some path returns
    # without it, the classifier must not raise.
    result = {"id": "g-p-abc", "name": "Foo"}  # no gizmo_type
    healthy, mismatch = classify_result("create_project", result)
    assert healthy is True  # shape is fine; semantic skipped (key absent)


# ── redaction + capture (Task 2) ──────────────────────────────

import json

from chatgpt_web2api.diagnostics import DiagnosticsDir, redact


def test_redact_strips_auth_tokens_and_emails():
    """Auth tokens, cookie values, and emails are replaced with <redacted>."""
    s = redact({
        "headers": {"Authorization": "Bearer eyJabc123.def"},
        "cookie": "__Secure-next-auth.session-token=longvalue",
        "email": "user@example.com",
        "url": "https://chatgpt.com/backend-api/conversation/abc-123",
    })
    dumped = json.dumps(s)
    assert "eyJabc123" not in dumped
    assert "<redacted>" in dumped
    # conversation IDs in URLs are NOT PII — keep them
    assert "abc-123" in s["url"]


def test_redact_truncates_long_bodies():
    """Captured response bodies are truncated to a safe size."""
    s = redact({"body": "x" * 10000})
    assert len(s["body"]) <= 2000


def test_capture_artifact_writes_redacted_json(tmp_path):
    """capture writes a redacted JSON file named <func>-<ts>.json."""
    diag = DiagnosticsDir(base=tmp_path)
    path = diag.capture(
        function="create_project",
        request={"expression": "fetch(...)", "data": {"token": "secret"}},
        response={"status": 422, "body": "validation error"},
        expected={"kind": "dict", "required_keys": ["id", "name"]},
        actual={"error": "HTTP 422"},
        mismatch="returned error shape: HTTP 422",
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["function"] == "create_project"
    assert data["mismatch"] == "returned error shape: HTTP 422"
    assert data["request"]["data"]["token"] == "<redacted>"


def test_capture_volume_cap_keeps_newest(tmp_path):
    """Only the N most recent artifacts per function are kept."""
    import time as _time
    diag = DiagnosticsDir(base=tmp_path, max_per_function=3)
    for i in range(5):
        diag.capture(
            function="get_models", request={}, response={}, expected={},
            actual={}, mismatch=f"m{i}",
        )
        _time.sleep(0.01)  # ensure distinct timestamps in filenames
    files = sorted(tmp_path.glob("get_models-*.json"))
    assert len(files) == 3
    kept = [json.loads(f.read_text())["mismatch"] for f in files]
    assert "m4" in kept and "m0" not in kept  # newest kept, oldest evicted


# ── @diagnose decorator (Task 3) ──────────────────────────────

import asyncio

from chatgpt_web2api.diagnostics import diagnose, set_capture_enabled


def test_diagnose_decorator_passes_through_healthy(monkeypatch, tmp_path):
    """A healthy result is returned unchanged; no artifact written."""
    from chatgpt_web2api.diagnostics import DiagnosticsDir
    monkeypatch.setattr("chatgpt_web2api.diagnostics._DIAG_DIR",
                        DiagnosticsDir(base=tmp_path))
    set_capture_enabled(True)

    class Stub:
        @diagnose("get_models")
        async def get_models(self_inner):
            return [{"slug": "a", "title": "A"}]

    result = asyncio.run(Stub().get_models())
    assert result == [{"slug": "a", "title": "A"}]
    assert not list(tmp_path.glob("*.json"))  # no artifact
    set_capture_enabled(False)


def test_diagnose_decorator_captures_on_broken(monkeypatch, tmp_path):
    """A broken result is still returned, but an artifact is captured."""
    from chatgpt_web2api.diagnostics import DiagnosticsDir
    monkeypatch.setattr("chatgpt_web2api.diagnostics._DIAG_DIR",
                        DiagnosticsDir(base=tmp_path))
    set_capture_enabled(True)

    class Stub:
        @diagnose("create_project",
                  capture_js=lambda self_inner: ("POST /backend-api/gizmos", {"token": "x"}))
        async def create_project(self_inner, name="x"):
            return {"error": "HTTP 422", "body": "bad"}

    result = asyncio.run(Stub().create_project(name="Foo"))
    assert result == {"error": "HTTP 422", "body": "bad"}  # caller sees original
    files = list(tmp_path.glob("create_project-*.json"))
    assert len(files) == 1
    art = json.loads(files[0].read_text())
    assert art["function"] == "create_project"
    assert art["request"]["expression"] == "POST /backend-api/gizmos"
    set_capture_enabled(False)


def test_diagnose_capture_disabled_by_default_writes_nothing(monkeypatch, tmp_path):
    """Capture is OFF unless enabled — no surprise disk writes."""
    from chatgpt_web2api.diagnostics import DiagnosticsDir
    monkeypatch.setattr("chatgpt_web2api.diagnostics._DIAG_DIR",
                        DiagnosticsDir(base=tmp_path))
    set_capture_enabled(False)

    class Stub:
        @diagnose("get_models")
        async def get_models(self_inner):
            return "wrong type"

    asyncio.run(Stub().get_models())
    assert not list(tmp_path.glob("*.json"))  # disabled → no capture


def test_diagnose_capture_failure_never_masks_original(monkeypatch, tmp_path):
    """If the capture itself errors, the original result is still returned."""
    from chatgpt_web2api.diagnostics import DiagnosticsDir
    # A DiagnosticsDir whose base can't be written to → capture raises internally
    monkeypatch.setattr("chatgpt_web2api.diagnostics._DIAG_DIR",
                        DiagnosticsDir(base=tmp_path))
    set_capture_enabled(True)
    # Make capture blow up by patching redact to raise.
    monkeypatch.setattr("chatgpt_web2api.diagnostics.redact",
                        lambda obj: (_ for _ in ()).throw(RuntimeError("boom")))

    class Stub:
        @diagnose("get_models")
        async def get_models(self_inner):
            return "wrong type"

    result = asyncio.run(Stub().get_models())  # must not raise
    assert result == "wrong type"
    set_capture_enabled(False)


# ── env-gated enablement (Task 4) ─────────────────────────────

def test_capture_enabled_by_env(monkeypatch):
    """W2A_DIAGNOSE=1 enables capture; absent/untruthy leaves it off."""
    import chatgpt_web2api.diagnostics as dmod
    monkeypatch.setenv("W2A_DIAGNOSE", "1")
    dmod.apply_env_enablement()
    assert dmod._capture_enabled is True

    monkeypatch.delenv("W2A_DIAGNOSE", raising=False)
    dmod.apply_env_enablement()
    assert dmod._capture_enabled is False

    monkeypatch.setenv("W2A_DIAGNOSE", "false")
    dmod.apply_env_enablement()
    assert dmod._capture_enabled is False

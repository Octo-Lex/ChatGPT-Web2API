"""Tests for the doctor subcommand: artifact discovery + evidence printing.

doctor is the human/agent-facing surface for the assisted-fix workflow. It
auto-discovers broken functions from captured artifacts (no human needs to
name the function) and prints the evidence an AI repair agent reads.
"""

import json
from pathlib import Path

from chatgpt_web2api.doctor import (
    latest_artifact_for,
    list_broken_functions,
    print_evidence,
)


def _write(base: Path, function: str, mismatch: str, seq: int = 1) -> Path:
    """Helper: write a minimal artifact and return its path."""
    p = base / f"{function}-20260619-120000-{seq:06d}.json"
    p.write_text(json.dumps({
        "function": function,
        "timestamp": "20260619-120000",
        "request": {"expression": "POST /backend-api/gizmos", "data": {}},
        "response": {"result": {"error": "HTTP 422"}},
        "expected": {"kind": "dict", "required_keys": ["id", "name"]},
        "actual": {"error": "HTTP 422"},
        "mismatch": mismatch,
    }))
    return p


def test_list_broken_functions_discovers_from_artifacts(tmp_path):
    """list_broken_functions returns distinct function names with artifacts."""
    _write(tmp_path, "get_models", "wrong type")
    _write(tmp_path, "get_models", "wrong type again", seq=2)
    _write(tmp_path, "create_project", "error shape")
    fns = list_broken_functions(tmp_path)
    assert set(fns) == {"get_models", "create_project"}


def test_list_broken_functions_empty_when_no_artifacts(tmp_path):
    """No artifacts → empty list."""
    assert list_broken_functions(tmp_path) == []


def test_latest_artifact_for_returns_newest(tmp_path):
    """latest_artifact_for returns the most recent artifact for a function."""
    older = _write(tmp_path, "get_models", "m1", seq=1)
    newer = _write(tmp_path, "get_models", "m2", seq=2)
    assert latest_artifact_for(tmp_path, "get_models") == newer
    assert latest_artifact_for(tmp_path, "get_models") != older


def test_print_evidence_outputs_key_fields(capsys, tmp_path):
    """print_evidence reads an artifact and prints function/mismatch/request."""
    art = _write(tmp_path, "create_project", "returned error shape: HTTP 422")
    print_evidence(art)
    out = capsys.readouterr().out
    assert "create_project" in out
    assert "HTTP 422" in out
    assert "POST /backend-api/gizmos" in out
    assert "id" in out  # expected keys shown


# ── doctor --verify (Task 6) ──────────────────────────────────

import asyncio
from unittest.mock import AsyncMock, MagicMock

from chatgpt_web2api import doctor_verify


def test_verify_reports_pass_when_healthy(monkeypatch, capsys):
    """verify runs the live function and reports PASS when the shape is healthy."""
    driver = MagicMock()
    driver.get_models = AsyncMock(return_value=[{"slug": "gpt-5", "title": "GPT-5"}])
    driver.close = AsyncMock()
    monkeypatch.setattr(doctor_verify, "_connect_driver", AsyncMock(return_value=driver))

    code = asyncio.run(doctor_verify.verify_function("get_models"))
    out = capsys.readouterr().out
    assert "PASS" in out
    assert code == 0


def test_verify_reports_fail_when_still_broken(monkeypatch, capsys):
    """verify reports FAIL when a read function returns a broken shape."""
    driver = MagicMock()
    # get_models has a runner; return a broken shape (string, not list)
    driver.get_models = AsyncMock(return_value='{"models": [...]}')
    driver.close = AsyncMock()
    monkeypatch.setattr(doctor_verify, "_connect_driver", AsyncMock(return_value=driver))

    code = asyncio.run(doctor_verify.verify_function("get_models"))
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert code == 1


def test_verify_returns_no_runner_for_mutating_tools(monkeypatch, capsys):
    """Functions without a safe verify runner report it (don't mutate account)."""
    driver = MagicMock()
    driver.close = AsyncMock()
    monkeypatch.setattr(doctor_verify, "_connect_driver", AsyncMock(return_value=driver))

    _code = asyncio.run(doctor_verify.verify_function("create_project"))
    out = capsys.readouterr().out
    # create_project is mutating — no safe runner → pointer to E2E suite
    assert "no safe verify runner" in out.lower() or "e2e" in out.lower()

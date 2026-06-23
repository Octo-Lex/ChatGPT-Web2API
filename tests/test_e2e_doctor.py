"""E2E: the diagnostic capture mechanism + doctor auto-discovery, live.

These tests prove the reactive loop end-to-end against a real Chrome session:
when a driver method returns a broken shape, an artifact is captured, and
`doctor` auto-discovers it (no human names the function) and prints evidence.

To keep the tests STABLE — independent of which functions happen to be broken
against the live API at any given time — the breakage is SYNTHETIC: we patch
the driver's low-level JS-eval helpers (BOTH _js_with_data and its strict
variant _js_with_data_strict) to return a payload that the method parses into
a broken shape. Patching both is deliberate: decorated methods may use either
variant, and coupling the test to which one a given method happens to call
broke this suite silently when get_projects migrated to the strict variant.
The real @diagnose-decorated method runs, hits the broken path, returns the
broken shape, and the decorator captures it — exactly the production code path,
just with a forced failure.

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_doctor.py -m e2e -v
"""

import json

import pytest

import chatgpt_web2api.diagnostics as dmod
from chatgpt_web2api.cdp_driver import CDPDriver

pytestmark = pytest.mark.e2e


async def test_broken_return_triggers_capture(e2e_driver: CDPDriver, tmp_path, monkeypatch):
    """A decorated driver method returning a broken shape writes an artifact.

    We force get_projects' underlying fetch to return an error payload (by
    patching _js_with_data), so the real @diagnose-decorated method runs, parses
    a broken result, and the decorator classifies + captures it. This is the
    production code path with a forced failure — stable regardless of which
    functions are currently broken live.
    """
    monkeypatch.setattr(dmod, "_DIAG_DIR", dmod.DiagnosticsDir(base=tmp_path))
    monkeypatch.setattr(dmod, "_capture_enabled", True)

    # Force get_projects' fetch to return an error-shaped JSON. get_projects
    # parses the response; an {"error": ...} body yields a broken dict result.
    # Patch BOTH JS-eval variants so the test is immune to which one the
    # decorated method calls (get_projects uses the strict variant today).
    async def _broken_js_with_data(self, template, data, timeout=15):
        return json.dumps({"error": "HTTP 500", "body": "synthetic drift"})

    monkeypatch.setattr(type(e2e_driver), "_js_with_data", _broken_js_with_data)
    monkeypatch.setattr(type(e2e_driver), "_js_with_data_strict", _broken_js_with_data)

    result = await e2e_driver.get_projects()
    assert isinstance(result, dict) and "error" in result, \
        f"expected broken dict, got {result!r}"

    files = list(tmp_path.glob("get_projects-*.json"))
    assert len(files) == 1, "expected a capture artifact for the broken call"
    art = json.loads(files[0].read_text())
    assert art["function"] == "get_projects"
    assert "error" in str(art["actual"]).lower()
    # redaction sanity: no raw auth token leaked into the artifact
    assert "Bearer eyJ" not in files[0].read_text()


async def test_doctor_auto_discovers_and_prints_broken_function(
    e2e_driver: CDPDriver, tmp_path, monkeypatch, capsys
):
    """After capture, doctor auto-discovers the broken function + prints evidence.

    No human names the function — list_broken_functions reads the diagnostics dir.
    """
    monkeypatch.setattr(dmod, "_DIAG_DIR", dmod.DiagnosticsDir(base=tmp_path))
    monkeypatch.setattr(dmod, "_capture_enabled", True)

    async def _broken_js_with_data(self, template, data, timeout=15):
        return json.dumps({"error": "HTTP 500", "body": "synthetic drift"})

    monkeypatch.setattr(type(e2e_driver), "_js_with_data", _broken_js_with_data)
    monkeypatch.setattr(type(e2e_driver), "_js_with_data_strict", _broken_js_with_data)
    await e2e_driver.get_projects()

    from chatgpt_web2api.doctor import latest_artifact_for, list_broken_functions, print_evidence

    fns = list_broken_functions(tmp_path)
    assert "get_projects" in fns

    art = latest_artifact_for(tmp_path, "get_projects")
    assert art is not None
    print_evidence(art)
    out = capsys.readouterr().out
    assert "get_projects" in out
    assert "MISMATCH" in out

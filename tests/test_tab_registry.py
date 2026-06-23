"""Tests for the owned-tab registry (R3) and tab observability (R6).

The registry persists this instance's owned tab so a restarted process
reclaims its OWN prior tab (not cross-session adoption — the original Finding
1 bug). Reclaim is instance-scoped and lease-protected: never steals a live
owner's tab, never touches another instance's entry.
"""

import json
import os
import time
from pathlib import Path

from chatgpt_web2api.tab_registry import (
    LEASE_TTL_SECONDS,
    TabRegistry,
    _pid_alive,
)


def _make_registry(tmp_path: Path, instance_id: str = "inst-A") -> TabRegistry:
    """A registry with isolated paths under tmp_path."""
    reg_path = tmp_path / "owned_tabs.json"
    lock_path = tmp_path / "owned_tabs.json.lock"
    return TabRegistry(instance_id, registry_path=reg_path, lock_path=lock_path)


def _write_entry(registry: TabRegistry, target_id: str, owner_pid: int,
                 heartbeat_age: float = 0.0, url: str = "") -> None:
    """Bypass the API to inject a registry entry for testing."""
    data = {registry.instance_id: {
        "target_id": target_id,
        "url": url,
        "owner_pid": owner_pid,
        "owner_started_at": time.time(),
        "heartbeat_at": time.time() - heartbeat_age,
        "cdp_port": None,
    }}
    registry.registry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(registry.registry_path, "w") as f:
        json.dump(data, f)


# ── 1. instance_id derivation ─────────────────────────────────────────

def test_instance_id_override_takes_precedence(monkeypatch):
    """W2A_INSTANCE_ID is the recommended way to run named sessions."""
    monkeypatch.setenv("W2A_INSTANCE_ID", "pr-review")
    assert TabRegistry.derive_instance_id(cdp_port=9222) == "pr-review"


def test_instance_id_derived_from_config_when_no_override(monkeypatch):
    """Without override, identity is a stable hash of config — two different
    server identities on the same Chrome profile get DIFFERENT ids."""
    monkeypatch.delenv("W2A_INSTANCE_ID", raising=False)
    rest_id = TabRegistry.derive_instance_id(cdp_port=9222, server_identity="rest:8080")
    mcp_id = TabRegistry.derive_instance_id(cdp_port=9222, server_identity="mcp")
    assert rest_id != mcp_id
    # Same inputs → same id (stable across calls).
    assert TabRegistry.derive_instance_id(cdp_port=9222, server_identity="rest:8080") == rest_id


# ── 2. reclaim: the core lease logic ──────────────────────────────────

def test_reclaim_returns_none_when_no_entry(tmp_path):
    """No prior entry → nothing to reclaim → caller creates a new tab."""
    r = _make_registry(tmp_path)
    assert r.reclaim({"tab-1"}) is None


def test_reclaim_returns_target_when_owner_dead(tmp_path):
    """A prior entry whose owner process is GONE is reclaimable."""
    r = _make_registry(tmp_path)
    _write_entry(r, "prior-tab", owner_pid=999999)  # nonexistent pid
    assert r.reclaim({"prior-tab"}) == "prior-tab"


def test_reclaim_returns_none_when_target_no_longer_live(tmp_path):
    """The recorded tab is gone from /json/list → can't reclaim, create new."""
    r = _make_registry(tmp_path)
    _write_entry(r, "dead-tab", owner_pid=999999)
    # "dead-tab" is NOT in the live set.
    assert r.reclaim({"other-tab"}) is None


def test_reclaim_returns_none_when_owner_alive_and_lease_fresh(tmp_path):
    """A live owner with a fresh heartbeat must NOT have its tab stolen —
    that's the cross-session-corruption guard (Finding 1 in reverse)."""
    r = _make_registry(tmp_path)
    _write_entry(r, "live-tab", owner_pid=os.getpid(), heartbeat_age=1.0)
    assert r.reclaim({"live-tab"}) is None


def test_reclaim_returns_target_when_owner_alive_but_lease_stale(tmp_path):
    """Owner process exists but heartbeat is stale (crashed/hung without
    clearing) → reclaimable. A long generation can't cause this if the
    heartbeat task is running (20s cadence, 60s TTL)."""
    r = _make_registry(tmp_path)
    _write_entry(r, "stale-tab", owner_pid=os.getpid(),
                 heartbeat_age=LEASE_TTL_SECONDS + 10)
    assert r.reclaim({"stale-tab"}) == "stale-tab"


def test_reclaim_writes_new_owner_atomically(tmp_path):
    """After a successful reclaim, the entry's owner_pid is US — a concurrent
    process checking immediately after sees the lease as taken."""
    r = _make_registry(tmp_path)
    _write_entry(r, "reclaim-tab", owner_pid=999999)
    r.reclaim({"reclaim-tab"})
    # Read the raw file to confirm we're now the owner.
    with open(r.registry_path) as f:
        entry = json.load(f)["inst-A"]
    assert entry["owner_pid"] == os.getpid()


# ── 3. instance isolation ─────────────────────────────────────────────

def test_reclaim_never_touches_other_instance_entry(tmp_path):
    """Reclaim is instance-scoped: instance A never sees/reclaims instance B's
    tab, even if B's owner is dead. This is the core anti-Finding-1 guarantee."""
    reg_path = tmp_path / "owned_tabs.json"
    lock_path = tmp_path / "owned_tabs.json.lock"
    # Instance B owns a tab.
    with open(reg_path, "w") as f:
        json.dump({"inst-B": {
            "target_id": "bs-tab", "owner_pid": 999999,
            "heartbeat_at": time.time(), "url": "",
        }}, f)
    # Instance A reclaims — must get None (its own entry doesn't exist).
    r_a = TabRegistry("inst-A", registry_path=reg_path, lock_path=lock_path)
    assert r_a.reclaim({"bs-tab"}) is None
    # B's entry is untouched.
    with open(reg_path) as f:
        assert json.load(f)["inst-B"]["target_id"] == "bs-tab"


# ── 4. record / heartbeat / clear ─────────────────────────────────────

def test_record_writes_entry(tmp_path):
    r = _make_registry(tmp_path)
    r.record("new-tab", url="https://chatgpt.com/")
    with open(r.registry_path) as f:
        entry = json.load(f)["inst-A"]
    assert entry["target_id"] == "new-tab"
    assert entry["owner_pid"] == os.getpid()
    assert entry["url"] == "https://chatgpt.com/"


def test_heartbeat_refreshes_timestamp(tmp_path):
    r = _make_registry(tmp_path)
    _write_entry(r, "hb-tab", owner_pid=os.getpid(), heartbeat_age=30.0)
    old_hb = json.load(open(r.registry_path))["inst-A"]["heartbeat_at"]
    time.sleep(0.05)
    r.heartbeat()
    new_hb = json.load(open(r.registry_path))["inst-A"]["heartbeat_at"]
    assert new_hb > old_hb


def test_heartbeat_noop_when_no_entry(tmp_path):
    """Heartbeat before record() must not crash (no entry yet)."""
    r = _make_registry(tmp_path)
    r.heartbeat()  # must not raise


def test_clear_removes_only_this_instance(tmp_path):
    r = _make_registry(tmp_path)
    _write_entry(r, "my-tab", owner_pid=os.getpid())
    # Also write a B entry.
    data = json.load(open(r.registry_path))
    data["inst-B"] = {"target_id": "bs-tab"}
    with open(r.registry_path, "w") as f:
        json.dump(data, f)
    r.clear()
    data = json.load(open(r.registry_path))
    assert "inst-A" not in data
    assert "inst-B" in data  # untouched


def test_clear_if_owner_clears_when_our_tab(tmp_path):
    """Clean shutdown: entry still points to our tab + our pid → cleared."""
    r = _make_registry(tmp_path)
    _write_entry(r, "my-tab", owner_pid=os.getpid())
    assert r.clear_if_owner("my-tab") is True
    data = json.load(open(r.registry_path))
    assert "inst-A" not in data


def test_clear_if_owner_preserves_reclaimed_entry(tmp_path):
    """Crash-reclaim race guard: if another process reclaimed our instance's
    entry (overwrote target_id + owner_pid), clear_if_owner must NOT delete
    their lease. This prevents a late-shutting-down process from wiping a
    new owner's tab record."""
    r = _make_registry(tmp_path)
    # Simulate: our instance's entry was reclaimed by pid 88888 pointing at a
    # DIFFERENT tab (we crashed, went stale, they took over).
    _write_entry(r, "their-new-tab", owner_pid=88888)
    # We try to clear on shutdown, but our target_id was "my-old-tab".
    assert r.clear_if_owner("my-old-tab") is False
    data = json.load(open(r.registry_path))
    assert "inst-A" in data  # their entry preserved
    assert data["inst-A"]["target_id"] == "their-new-tab"


def test_clear_if_owner_noop_when_no_entry(tmp_path):
    r = _make_registry(tmp_path)
    assert r.clear_if_owner("any") is False


# ── 5. R6 observability ───────────────────────────────────────────────

def test_status_returns_snapshot(tmp_path):
    r = _make_registry(tmp_path)
    r.record("obs-tab", url="https://chatgpt.com/c/x")
    status = r.status()
    assert status["instance_id"] == "inst-A"
    assert status["target_id"] == "obs-tab"
    assert status["lease_ttl_s"] == LEASE_TTL_SECONDS
    assert status["heartbeat_age_s"] is not None


def test_driver_tab_status_includes_registry(tmp_path):
    """CDPDriver.tab_status() surfaces the registry state for /health + logs."""
    from chatgpt_web2api.cdp_driver import CDPDriver
    d = CDPDriver(cdp_port=9222, tab_mode="owned")
    d._target_id = "drv-tab"
    d._owns_target = True
    d._current_conv_id = "conv-1"
    status = d.tab_status()
    assert status["tab_mode"] == "owned"
    assert status["target_id"] == "drv-tab"
    assert status["owns_target"] is True
    assert status["conv_id"] == "conv-1"
    assert "instance_id" in status
    assert "registry" in status


def test_driver_tab_status_no_registry_in_adopt_mode():
    """Adopt mode has no registry (no owned tabs to persist). tab_status still works."""
    from chatgpt_web2api.cdp_driver import CDPDriver
    d = CDPDriver(cdp_port=9222, tab_mode="adopt")
    status = d.tab_status()
    assert status["tab_mode"] == "adopt"
    assert "registry" not in status


# ── 6. pid_alive sanity ───────────────────────────────────────────────

def test_pid_alive_self_is_true():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_dead_pid_is_false():
    # 999999 is almost certainly not a running pid.
    assert _pid_alive(999999) is False


def test_pid_alive_zero_is_false():
    assert _pid_alive(0) is False

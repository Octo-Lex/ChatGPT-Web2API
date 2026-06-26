"""Tests for A1 (config auto-load) and A2 (safe bind defaults)."""

import json

import pytest

from chatgpt_web2api.config import Config
from chatgpt_web2api.service import Service

# ── A1: config auto-discovery ─────────────────────────────────────────

def test_config_auto_loads_default_when_no_path(tmp_path, monkeypatch):
    """A1: when --config is omitted and ~/.chatgpt-web2api/config.json exists,
    it's loaded (the docs tell users to create it; the old code ignored it)."""
    # Point HOME/USERPROFILE at tmp_path so Path.home() finds our fake config
    # on both Unix (HOME) and Windows (USERPROFILE).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg_dir = tmp_path / ".chatgpt_web2api"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({
        "port": 9999, "api_keys": ["secret-key"],
    }))
    cfg = Config.load(None)
    assert cfg.server.port == 9999
    assert cfg.server.api_keys == ["secret-key"]


def test_config_no_default_uses_builtin_defaults(tmp_path, monkeypatch):
    """No --config and no default file → built-in defaults, no error."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = Config.load(None)
    assert cfg.server.port == 8080  # built-in default
    assert cfg.server.host == "127.0.0.1"


def test_config_explicit_path_overrides_default(tmp_path, monkeypatch):
    """An explicit --config path takes precedence over auto-discovery."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Both a default file AND an explicit file exist.
    cfg_dir = tmp_path / ".chatgpt_web2api"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"port": 7777}))
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"port": 6666}))
    cfg = Config.load(str(explicit))
    assert cfg.server.port == 6666  # explicit wins


def test_config_malformed_default_does_not_crash(tmp_path, monkeypatch):
    """A malformed default config is warned about, not crashed on — fall back
    to built-in defaults rather than preventing startup."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg_dir = tmp_path / ".chatgpt_web2api"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text("{ not valid json")
    cfg = Config.load(None)
    assert cfg.server.port == 8080  # fell back to defaults


# ── A2: safe bind defaults ────────────────────────────────────────────

def _make_cfg_with_host(host: str, api_keys=None):
    """Build a Config with the given host + api_keys for bind-safety testing."""
    cfg = Config()
    cfg.server.host = host
    cfg.server.api_keys = api_keys or []
    return cfg


def test_bind_safety_loopback_no_keys_allows(monkeypatch):
    """Loopback + no keys → allowed (the documented local no-auth mode)."""
    cfg = _make_cfg_with_host("127.0.0.1")
    # Must not raise.
    Service._check_bind_safety(cfg)


def test_bind_safety_remote_with_keys_allows(monkeypatch):
    """Non-loopback + keys → allowed (auth is on)."""
    cfg = _make_cfg_with_host("0.0.0.0", api_keys=["k"])
    Service._check_bind_safety(cfg)  # must not raise


def test_bind_safety_remote_no_keys_fails(monkeypatch):
    """Non-loopback + no keys + no override → RuntimeError at startup. This is
    the fail-safe: never silently expose an unauthenticated API to the network."""
    monkeypatch.delenv("W2A_ALLOW_UNAUTH_REMOTE", raising=False)
    cfg = _make_cfg_with_host("0.0.0.0")
    with pytest.raises(RuntimeError, match="W2A_ALLOW_UNAUTH_REMOTE"):
        Service._check_bind_safety(cfg)


def test_bind_safety_remote_no_keys_override_allows(monkeypatch):
    """Non-loopback + no keys + W2A_ALLOW_UNAUTH_REMOTE=1 → allowed (with warning)."""
    monkeypatch.setenv("W2A_ALLOW_UNAUTH_REMOTE", "1")
    cfg = _make_cfg_with_host("0.0.0.0")
    Service._check_bind_safety(cfg)  # must not raise


def test_bind_safety_localhost_treated_as_loopback():
    """'localhost' and '::1' are loopback too, not just 127.0.0.1."""
    for host in ("localhost", "::1", ""):
        cfg = _make_cfg_with_host(host)
        Service._check_bind_safety(cfg)  # must not raise


def test_bind_safety_error_names_the_override():
    """The error message must name W2A_ALLOW_UNAUTH_REMOTE so a user who hits
    it knows the escape hatch without reading docs."""
    cfg = _make_cfg_with_host("0.0.0.0")
    try:
        Service._check_bind_safety(cfg)
        pytest.fail("should have raised")
    except RuntimeError as e:
        assert "W2A_ALLOW_UNAUTH_REMOTE" in str(e)


# ── A3: ensure config tunables (PR3) ───────────────────────────────────


def test_ensure_config_defaults_when_absent(tmp_path, monkeypatch):
    """No ensure_* keys in config → built-in EnsureConfig defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = Config.load(None)
    assert cfg.ensure.degraded_poll_interval_s == 2.0
    assert cfg.ensure.degraded_poll_budget_s == 20.0
    assert cfg.ensure.breaker_cooldown_grace_s == 5.0


def test_ensure_config_loaded_from_file(tmp_path, monkeypatch):
    """ensure_* keys in a config file populate EnsureConfig."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({
        "ensure_degraded_poll_interval_s": 1.5,
        "ensure_degraded_poll_budget_s": 7.0,
        "ensure_breaker_cooldown_grace_s": 3.0,
    }))
    cfg = Config.load(str(cfg_file))
    assert cfg.ensure.degraded_poll_interval_s == 1.5
    assert cfg.ensure.degraded_poll_budget_s == 7.0
    assert cfg.ensure.breaker_cooldown_grace_s == 3.0


def test_ensure_config_env_overrides(monkeypatch, tmp_path):
    """W2A_ENSURE_* env vars override config + defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("W2A_ENSURE_DEGRADED_POLL_BUDGET_S", "9.0")
    monkeypatch.setenv("W2A_ENSURE_BREAKER_COOLDOWN_GRACE_S", "2.0")
    cfg = Config.load(None)
    assert cfg.ensure.degraded_poll_budget_s == 9.0
    assert cfg.ensure.breaker_cooldown_grace_s == 2.0


def test_ensure_config_to_dict_roundtrip():
    """to_dict serializes the ensure tunables (flat keys)."""
    cfg = Config()
    cfg.ensure.degraded_poll_budget_s = 11.0
    d = cfg.to_dict()
    assert d["ensure_degraded_poll_interval_s"] == 2.0
    assert d["ensure_degraded_poll_budget_s"] == 11.0
    assert d["ensure_breaker_cooldown_grace_s"] == 5.0


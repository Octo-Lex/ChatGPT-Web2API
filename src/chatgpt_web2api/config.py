"""Configuration for chatgpt-web2api."""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _default_chrome_path() -> str:
    """Find Chrome on the current system."""
    system = platform.system()
    candidates = []
    if system == "Windows":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", r"C:\Users\USER\AppData\Local")
        candidates = [
            f"{pf}\\Google\\Chrome\\Application\\chrome.exe",
            f"{pfx86}\\Google\\Chrome\\Application\\chrome.exe",
            f"{local}\\Google\\Chrome\\Application\\chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
    for c in candidates:
        if Path(c).exists():
            return c
    # Fallback — rely on PATH
    found = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    return found or "chrome"


def _default_user_data_dir() -> str:
    """Default Chrome profile directory dedicated to this proxy."""
    base = Path.home() / ".chatgpt-web2api"
    return str(base / "chrome-profile")


@dataclass
class ChromeConfig:
    chrome_path: str = field(default_factory=_default_chrome_path)
    user_data_dir: str = field(default_factory=_default_user_data_dir)
    cdp_port: int = 9222
    headless: bool = False
    extra_args: list[str] = field(default_factory=list)
    restart_on_crash: bool = True


@dataclass
class ServerConfig:
    port: int = 8080
    host: str = "127.0.0.1"
    api_keys: list[str] = field(default_factory=list)
    request_timeout: int = 120


@dataclass
class ChatGPTConfig:
    default_model: str = "auto"
    default_project_id: str | None = None
    # Tab isolation strategy: "owned" (default) creates a dedicated chatgpt.com
    # tab per driver/process via Target.createTarget so two simultaneous
    # sessions don't contend on the same DOM. "adopt" reuses an existing
    # chatgpt.com tab (the pre-multi-session behavior) for single-process
    # compatibility. Owned tabs are the safe default because adoption lets one
    # session navigate another's tab out from under it.
    tab_mode: str = "owned"
    # Parallel multi-tab mode (PR4/5). When True, the bundle is enforced:
    # tab_mode=owned (validated at load), per-target MutationLocks, and
    # fail-closed owned-tab requirement (no shared-tab fallback). Default False
    # reproduces the exact legacy single-tab-serialized behavior.
    parallel_tabs: bool = False


@dataclass
class LogConfig:
    level: str = "INFO"
    file: str | None = None


@dataclass
class EnsureConfig:
    """Tunables for ``chatgpt-web2api ensure`` reconcile policy.

    Narrow on purpose: only the values ensure reads. Breaker thresholds/windows
    stay hardcoded (not configurable here)."""
    degraded_poll_interval_s: float = 2.0
    degraded_poll_budget_s: float = 20.0
    breaker_cooldown_grace_s: float = 5.0


def _as_bool(v: object) -> bool:
    """Parse a config/env value as bool, robust to JSON bools and strings.

    ``bool("false")`` is ``True`` in Python (non-empty string), so a naive
    ``bool(c)`` misparses string config values. Accept JSON booleans directly
    and the usual truthy strings (``true``/``1``/``yes``); everything else
    (incl. ``"false"``, ``"0"``, ``"no"``) is False.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v)


@dataclass
class Config:
    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    chatgpt: ChatGPTConfig = field(default_factory=ChatGPTConfig)
    log: LogConfig = field(default_factory=LogConfig)
    ensure: EnsureConfig = field(default_factory=EnsureConfig)

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        """Load config from file + env overrides.

        If *path* is given, load that file. Otherwise auto-discover the
        documented default at ``~/.chatgpt-web2api/config.json`` — the
        deployment docs tell users to create it, and silently ignoring it
        (the old behavior) was a footgun where following the docs yielded
        defaults with no error. Auto-discovery is logged so it's never silent.
        """
        import logging
        log = logging.getLogger("chatgpt_web2api.config")
        cfg = cls()
        if path and Path(path).exists():
            with open(path) as f:
                data = json.load(f)
            cfg._apply_dict(data)
            log.info("Loaded config from %s", path)
        elif path is None:
            # Auto-discover the documented default config location.
            default_path = Path.home() / ".chatgpt_web2api" / "config.json"
            if default_path.exists():
                try:
                    with open(default_path) as f:
                        data = json.load(f)
                    cfg._apply_dict(data)
                    log.info("Loaded config from %s (auto-discovered)", default_path)
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("Could not load default config %s: %s", default_path, e)
            else:
                log.debug("No default config at %s; using built-in defaults", default_path)
        cfg._apply_env()
        # Validate AFTER both file + env overlays are applied — env can fix or
        # break what the file set. This is the first real validation in config;
        # keep it as a single explicit bundle check for the parallel-tabs safety
        # invariant.
        if cfg.chatgpt.parallel_tabs and cfg.chatgpt.tab_mode != "owned":
            raise ValueError(
                "parallel_tabs=true requires tab_mode=owned (got "
                f"{cfg.chatgpt.tab_mode!r}); parallel mode needs per-target "
                "owned tabs for correct locking"
            )
        return cfg

    def _apply_dict(self, data: dict) -> None:
        c = data.get("chrome_path")
        if c:
            self.chrome.chrome_path = c
        c = data.get("user_data_dir")
        if c:
            self.chrome.user_data_dir = c
        c = data.get("cdp_port")
        if c is not None:
            self.chrome.cdp_port = int(c)
        c = data.get("headless")
        if c is not None:
            self.chrome.headless = _as_bool(c)
        c = data.get("port")
        if c is not None:
            self.server.port = int(c)
        c = data.get("host")
        if c:
            self.server.host = c
        c = data.get("api_keys")
        if c:
            self.server.api_keys = list(c)
        c = data.get("default_model")
        if c:
            self.chatgpt.default_model = c
        c = data.get("default_project_id")
        if c:
            self.chatgpt.default_project_id = c
        c = data.get("tab_mode")
        if c in ("owned", "adopt"):
            self.chatgpt.tab_mode = c
        c = data.get("parallel_tabs")
        if c is not None:
            self.chatgpt.parallel_tabs = _as_bool(c)
        c = data.get("request_timeout")
        if c is not None:
            self.server.request_timeout = int(c)
        c = data.get("log_level")
        if c:
            self.log.level = c
        c = data.get("log_file")
        if c:
            self.log.file = c
        c = data.get("ensure_degraded_poll_interval_s")
        if c is not None:
            self.ensure.degraded_poll_interval_s = float(c)
        c = data.get("ensure_degraded_poll_budget_s")
        if c is not None:
            self.ensure.degraded_poll_budget_s = float(c)
        c = data.get("ensure_breaker_cooldown_grace_s")
        if c is not None:
            self.ensure.breaker_cooldown_grace_s = float(c)

    def _apply_env(self) -> None:
        _env = os.environ.get
        if v := _env("W2A_CHROME_PATH"):
            self.chrome.chrome_path = v
        if v := _env("W2A_USER_DATA_DIR"):
            self.chrome.user_data_dir = v
        if v := _env("W2A_CDP_PORT"):
            self.chrome.cdp_port = int(v)
        if v := _env("W2A_PORT"):
            self.server.port = int(v)
        if v := _env("W2A_HOST"):
            self.server.host = v
        if v := _env("W2A_API_KEYS"):
            self.server.api_keys = [k.strip() for k in v.split(",") if k.strip()]
        if v := _env("W2A_DEFAULT_MODEL"):
            self.chatgpt.default_model = v
        if v := _env("W2A_TAB_MODE"):
            if v in ("owned", "adopt"):
                self.chatgpt.tab_mode = v
        if v := _env("W2A_PARALLEL_TABS"):
            self.chatgpt.parallel_tabs = v.lower() in ("true", "1", "yes")
        if v := _env("W2A_HEADLESS"):
            self.chrome.headless = v.lower() in ("true", "1", "yes")
        if v := _env("W2A_LOG_LEVEL"):
            self.log.level = v
        if v := _env("W2A_ENSURE_DEGRADED_POLL_INTERVAL_S"):
            self.ensure.degraded_poll_interval_s = float(v)
        if v := _env("W2A_ENSURE_DEGRADED_POLL_BUDGET_S"):
            self.ensure.degraded_poll_budget_s = float(v)
        if v := _env("W2A_ENSURE_BREAKER_COOLDOWN_GRACE_S"):
            self.ensure.breaker_cooldown_grace_s = float(v)

    def to_dict(self) -> dict:
        return {
            "chrome_path": self.chrome.chrome_path,
            "user_data_dir": self.chrome.user_data_dir,
            "cdp_port": self.chrome.cdp_port,
            "headless": self.chrome.headless,
            "port": self.server.port,
            "host": self.server.host,
            "api_keys": self.server.api_keys,
            "default_model": self.chatgpt.default_model,
            "default_project_id": self.chatgpt.default_project_id,
            "tab_mode": self.chatgpt.tab_mode,
            "parallel_tabs": self.chatgpt.parallel_tabs,
            "request_timeout": self.server.request_timeout,
            "log_level": self.log.level,
            "ensure_degraded_poll_interval_s": self.ensure.degraded_poll_interval_s,
            "ensure_degraded_poll_budget_s": self.ensure.degraded_poll_budget_s,
            "ensure_breaker_cooldown_grace_s": self.ensure.breaker_cooldown_grace_s,
        }

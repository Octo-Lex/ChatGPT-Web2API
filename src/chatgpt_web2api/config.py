"""Configuration for chatgpt-web2api."""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    default_project_id: Optional[str] = None


@dataclass
class LogConfig:
    level: str = "INFO"
    file: Optional[str] = None


@dataclass
class Config:
    chrome: ChromeConfig = field(default_factory=ChromeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    chatgpt: ChatGPTConfig = field(default_factory=ChatGPTConfig)
    log: LogConfig = field(default_factory=LogConfig)

    @classmethod
    def load(cls, path: Optional[str] = None) -> Config:
        """Load config from file + env overrides."""
        cfg = cls()
        if path and Path(path).exists():
            with open(path) as f:
                data = json.load(f)
            cfg._apply_dict(data)
        cfg._apply_env()
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
            self.chrome.headless = bool(c)
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
        c = data.get("request_timeout")
        if c is not None:
            self.server.request_timeout = int(c)
        c = data.get("log_level")
        if c:
            self.log.level = c
        c = data.get("log_file")
        if c:
            self.log.file = c

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
        if v := _env("W2A_HEADLESS"):
            self.chrome.headless = v.lower() in ("true", "1", "yes")
        if v := _env("W2A_LOG_LEVEL"):
            self.log.level = v

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
            "request_timeout": self.server.request_timeout,
            "log_level": self.log.level,
        }

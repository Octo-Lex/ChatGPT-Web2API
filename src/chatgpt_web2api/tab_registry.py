"""File-backed owned-tab registry with instance-id keying and heartbeat lease.

Solves the orphan-tab problem: each fresh bridge process creates its own
ChatGPT tab via ``Target.createTarget``, but the owned ``_target_id`` is
in-memory state that doesn't survive a process restart. So a killed+restarted
process left its prior tab open and created another — tab accumulation.

This registry persists the owned tab per *logical bridge instance* so a
restarted process can reclaim its own prior tab instead of creating a new one.
It is NOT cross-session adoption (the bug that corrupted conversation routing):
two processes with different ``instance_id``s never touch each other's entries,
and a process never reclaims an entry whose owner is provably still alive.

Design (agreed in collaborative review):
  - File at ``~/.chatgpt_web2api/owned_tabs.json``, keyed by ``instance_id``.
  - ``instance_id`` derived from configured identity (chrome profile + cdp
    port + server/transport identity), overridable via ``W2A_INSTANCE_ID``.
  - Heartbeat lease: each entry carries ``owner_pid`` + ``heartbeat_at``. A
    fresh heartbeat (< TTL) means occupied — do not reclaim. A stale heartbeat
    (owner dead, or heartbeat older than TTL) means reclaimable.
  - ``reclaim()`` is an ATOMIC CLAIM: it holds the portalocker file lock across
    read + validate + write-new-owner, so two processes can't both decide to
    reclaim the same entry. No CAS/version field needed.
  - No global reaping by default. Reclaim touches only this instance's entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import portalocker

# A heartbeat older than this (seconds) means the owning process is gone or
# stuck, and the entry is reclaimable. Generous enough that a long generation
# (60-90s) with a 15-20s heartbeat cadence can't falsely expire a live owner.
LEASE_TTL_SECONDS = 60.0
HEARTBEAT_INTERVAL_SECONDS = 20.0

REGISTRY_DIR = Path.home() / ".chatgpt_web2api"
REGISTRY_PATH = REGISTRY_DIR / "owned_tabs.json"
LOCK_PATH = REGISTRY_DIR / "owned_tabs.json.lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort 'is this process still running?' check.

    On Unix, ``os.kill(pid, 0)`` returns silently if alive, raises ProcessLookupError
    if not. On Windows, there's no signal 0, so we use OpenProcess via ctypes.
    Returns True only on positive confirmation; any error → False (treat as
    gone, so a crashed process's entry becomes reclaimable).
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # No such process — definitively gone.
        return False
    except PermissionError:
        # Process exists but is owned by another user. Treat as alive — don't
        # steal a running process's tab.
        return True
    except OSError:
        return False


def _load_registry(path: Path) -> dict[str, Any]:
    """Read the registry JSON, returning an empty dict on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_registry(path: Path, data: dict[str, Any]) -> None:
    """Write the registry JSON, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)  # atomic on most filesystems


class TabRegistry:
    """Persisted owned-tab registry with an instance-id-keyed heartbeat lease.

    Each instance reclaims only its own entry. ``reclaim()`` is an atomic claim:
    it holds the file lock across read + validate + write-new-owner, so two
    processes can't both decide to reclaim the same stale entry.
    """

    def __init__(
        self,
        instance_id: str,
        registry_path: Path = REGISTRY_PATH,
        lock_path: Path = LOCK_PATH,
    ) -> None:
        self.instance_id = instance_id
        self.registry_path = registry_path
        self.lock_path = lock_path
        self._owner_pid = os.getpid()
        self._owner_started_at = time.time()

    @staticmethod
    def derive_instance_id(
        chrome_user_data_dir: str = "",
        cdp_port: int = 0,
        server_identity: str = "",
    ) -> str:
        """Stable per-configuration identity, or the W2A_INSTANCE_ID override.

        Two bridge sessions using the same Chrome profile + port but doing
        different work should get DIFFERENT instance ids — hence the
        server/transport identity in the hash. The env override is the
        recommended way to run explicitly-named sessions (e.g. ``pr-review``
        vs ``release-notes``).
        """
        override = os.environ.get("W2A_INSTANCE_ID", "").strip()
        if override:
            return override
        raw = f"{chrome_user_data_dir}|{cdp_port}|{server_identity or 'mcp'}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _entry_is_reclaimable(self, entry: dict[str, Any]) -> bool:
        """Is this entry's tab reclaimable by us right now?

        Reclaimable if: the recorded owner is dead, OR the heartbeat is stale
        (older than LEASE_TTL_SECONDS). NOT reclaimable if the owner is alive
        with a fresh heartbeat — that means a live process is using this tab.
        """
        owner_pid = entry.get("owner_pid")
        heartbeat_at = entry.get("heartbeat_at", 0.0)
        if owner_pid and _pid_alive(owner_pid):
            # Owner process exists. Reclaim only if its heartbeat is stale
            # (it crashed/hung without clearing the entry).
            return (time.time() - heartbeat_at) > LEASE_TTL_SECONDS
        # Owner process is gone → entry is reclaimable.
        return True

    def reclaim(self, live_target_ids: set[str]) -> str | None:
        """Atomically claim this instance's prior tab if it's still alive.

        Holds the file lock across read + validate + write-new-owner so two
        processes can't both reclaim the same stale entry. Returns the
        target_id to reuse, or None if no reclaimable entry exists (caller
        creates a new tab). Always writes a fresh owner record on success so
        a concurrent process sees the lease as taken.
        """
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), timeout=10):
            data = _load_registry(self.registry_path)
            entry = data.get(self.instance_id)
            if (
                entry
                and isinstance(entry, dict)
                and entry.get("target_id") in live_target_ids
                and self._entry_is_reclaimable(entry)
            ):
                target_id = entry["target_id"]
                # Atomic claim: write ourselves as the new owner before
                # releasing the lock, so a concurrent process can't also
                # reclaim.
                data[self.instance_id] = self._fresh_entry(target_id, entry.get("url", ""))
                _save_registry(self.registry_path, data)
                return target_id
        return None

    def record(self, target_id: str, url: str = "") -> None:
        """Record a newly-created owned tab for this instance."""
        with portalocker.Lock(str(self.lock_path), timeout=10):
            data = _load_registry(self.registry_path)
            data[self.instance_id] = self._fresh_entry(target_id, url)
            _save_registry(self.registry_path, data)

    def heartbeat(self, target_id: str | None = None, url: str = "") -> None:
        """Refresh this instance's lease. Called periodically + opportunistically.

        A no-op if no entry exists yet (e.g. before record()). If target_id is
        given and differs from the recorded one, updates it (tab recreated).
        """
        with portalocker.Lock(str(self.lock_path), timeout=10):
            data = _load_registry(self.registry_path)
            entry = data.get(self.instance_id)
            if not entry:
                return
            entry["heartbeat_at"] = time.time()
            entry["owner_pid"] = self._owner_pid
            if target_id:
                entry["target_id"] = target_id
            if url:
                entry["url"] = url
            _save_registry(self.registry_path, data)

    def clear(self) -> None:
        """Remove this instance's entry (on clean shutdown)."""
        with portalocker.Lock(str(self.lock_path), timeout=10):
            data = _load_registry(self.registry_path)
            if self.instance_id in data:
                del data[self.instance_id]
                _save_registry(self.registry_path, data)

    def clear_if_owner(self, target_id: str | None) -> bool:
        """Remove this instance's entry ONLY if it still points to our tab.

        Guard against the crash-reclaim race: if this driver crashed, its lease
        went stale, and another process reclaimed the entry (overwriting the
        owner_pid + target_id), clearing unconditionally would delete the NEW
        owner's entry. This checks the recorded target_id still matches ours
        before deleting. Returns True if cleared, False if left intact.
        """
        with portalocker.Lock(str(self.lock_path), timeout=10):
            data = _load_registry(self.registry_path)
            entry = data.get(self.instance_id)
            if not entry:
                return False
            # Only clear if the entry still belongs to us (same target, or
            # same owner pid). If another process reclaimed it, leave it.
            if (
                entry.get("target_id") == target_id
                and entry.get("owner_pid") == self._owner_pid
            ):
                del data[self.instance_id]
                _save_registry(self.registry_path, data)
                return True
            return False

    def status(self) -> dict[str, Any]:
        """Snapshot of this instance's registry entry (for observability/R6)."""
        data = _load_registry(self.registry_path)
        entry = data.get(self.instance_id, {})
        return {
            "instance_id": self.instance_id,
            "target_id": entry.get("target_id"),
            "owner_pid": entry.get("owner_pid"),
            "heartbeat_age_s": round(time.time() - entry.get("heartbeat_at", 0.0), 1)
            if entry.get("heartbeat_at")
            else None,
            "url": entry.get("url", ""),
            "lease_ttl_s": LEASE_TTL_SECONDS,
        }

    def _fresh_entry(self, target_id: str, url: str) -> dict[str, Any]:
        return {
            "target_id": target_id,
            "url": url,
            "owner_pid": self._owner_pid,
            "owner_started_at": self._owner_started_at,
            "heartbeat_at": time.time(),
            "cdp_port": None,
        }

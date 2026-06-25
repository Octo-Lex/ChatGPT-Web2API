"""Non-rate-limit circuit-breaker registry (ROADMAP Phase 4, PR1).

A small, pure-logic registry that tracks four failure classes the project has
no coherent story for today (rate-limit retry is already handled by
``resilience.py`` — deliberately not duplicated here):

  - ``auth_required``           — ChatGPT session expired (needs human login)
  - ``composer_send_readiness`` — composer / send-readiness repeated failures
  - ``cdp_reconnect``           — CDP websocket reconnect failures
  - ``chrome_crash_loop``       — Chrome restart loop

PR1 is infrastructure-only: the registry exists and is snapshotted into
``/health``, but **nothing records failures or trips a breaker yet**, so every
breaker always reports closed. This proves the plumbing with zero behavior
change. PR2 wires the real failure signals (and the typed exceptions the
composer/CDP paths need, since they currently raise bare ``RuntimeError``).

Design notes:
  - ``BreakerKind`` is a ``str`` enum so ``.value`` serializes straight into
    the ``/health`` JSON without an extra mapping layer.
  - Timestamps are ``time.monotonic()`` (not wall-clock) so cooldown math is
    immune to system clock changes and unit tests can drive a virtual clock.
  - Thresholds are encoded as defaults but ``record_failure`` does NOT auto-trip
    on reaching a threshold in PR1 — that policy belongs in PR2 alongside the
    signal wiring. PR1 only counts; ``trip()`` is the explicit, caller-driven
    path. This keeps PR1 genuinely behavior-free.
  - Single-process async server: no locks, matching ``APIServer``'s own
    unsynchronized counters (``_request_count``, ``_last_error``).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class BreakerKind(StrEnum):
    """The four non-rate-limit failure classes. Values are the exposure names
    used in the ``/health`` snapshot."""

    AUTH_EXPIRED = "auth_required"
    COMPOSER_SEND_READINESS = "composer_send_readiness"
    CDP_RECONNECT = "cdp_reconnect"
    CHROME_CRASH_LOOP = "chrome_crash_loop"


@dataclass
class BreakerState:
    """Mutable per-kind state. Not part of the public snapshot shape; the
    registry serializes a flat dict via ``snapshot()``."""

    tripped: bool = False
    reason: str | None = None
    tripped_at: float | None = None
    cooldown_until: float | None = None
    recent_failures: deque[float] = field(default_factory=deque)


# ROADMAP Phase 4 policies per kind. The window differs by class — notably
# ``CHROME_CRASH_LOOP`` is 3 restarts in **5 min** (300s), not the 2 min window
# the other three classes use — so the window must be per-kind, not global.
# PR1 records these but does not auto-trip on them — PR2 enforces them.
@dataclass(frozen=True)
class BreakerPolicy:
    """Per-kind failure policy: how many failures, in what window, trip a
    breaker, and how long that breaker then cools down.

    ``cooldown_s`` of ``None`` means "indefinite until external reset" and is
    used only for the auth case; the snapshot represents it as
    ``cooldown_until=None`` after a trip.
    """

    threshold: int
    window_s: float
    cooldown_s: float | None


_DEFAULT_POLICIES: dict[BreakerKind, BreakerPolicy] = {
    BreakerKind.AUTH_EXPIRED: BreakerPolicy(1, 120.0, None),
    BreakerKind.COMPOSER_SEND_READINESS: BreakerPolicy(3, 120.0, 300.0),
    BreakerKind.CDP_RECONNECT: BreakerPolicy(5, 120.0, 120.0),
    BreakerKind.CHROME_CRASH_LOOP: BreakerPolicy(3, 300.0, 300.0),
}


@dataclass
class BreakerRegistry:
    """Tracks failure history and explicit trip state for each ``BreakerKind``.

    PR1 contract: ``record_failure`` counts (no auto-trip); ``trip`` opens a
    breaker explicitly; ``is_open`` / ``snapshot`` read state. Callers in PR2
    will decide when to trip based on the thresholds below.
    """

    _policies: dict[BreakerKind, BreakerPolicy] = field(
        default_factory=lambda: dict(_DEFAULT_POLICIES)
    )
    _max_recent: int = 50  # cap deque depth (bound memory under a storm)
    _states: dict[BreakerKind, BreakerState] = field(
        default_factory=lambda: {k: BreakerState() for k in BreakerKind}
    )

    # ── recording ────────────────────────────────────────────────────────

    def record_failure(self, kind: BreakerKind) -> None:
        """Append a failure timestamp and prune the rolling window. Does NOT
        auto-trip in PR1 — threshold enforcement is a PR2 policy decision."""
        state = self._states[kind]
        state.recent_failures.append(time.monotonic())
        self._prune(kind, state)

    def record_success(self, kind: BreakerKind) -> None:
        """Record a success. In PR1 this is a no-op beyond clearing the failure
        history for the kind — a successful operation means the failure run is
        over. Does NOT auto-close an explicitly-tripped breaker (PR2 policy)."""
        state = self._states[kind]
        state.recent_failures.clear()

    def trip(self, kind: BreakerKind, reason: str, *, cooldown_s: float = 0.0) -> None:
        """Explicitly open a breaker.

        ``cooldown_s=0`` (the auth case) means the breaker stays open
        indefinitely until an external recovery calls ``reset`` — matching the
        ROADMAP's "require human browser login" intent. A positive cooldown
        sets ``cooldown_until``; ``is_open`` returns False once monotonic time
        passes it (half-open, eligible for re-trip).
        """
        now = time.monotonic()
        state = self._states[kind]
        state.tripped = True
        state.reason = reason
        state.tripped_at = now
        # cooldown_s=0 → no expiry (stays open until reset). Positive → timed.
        state.cooldown_until = now + cooldown_s if cooldown_s > 0 else None

    def reset(self, kind: BreakerKind) -> None:
        """Clear a breaker back to its untripped state. Used by PR2's recovery
        paths (e.g. after a successful human re-login for auth)."""
        self._states[kind] = BreakerState()

    # ── reading ──────────────────────────────────────────────────────────

    def is_open(self, kind: BreakerKind) -> bool:
        """True if the breaker is tripped and within its cooldown.

        A tripped breaker past its cooldown is half-open (returns False) — a
        subsequent operation may re-trip it. A ``cooldown_s=0`` trip (auth) has
        ``cooldown_until=None`` (no expiry), so it stays open until ``reset``."""
        state = self._states[kind]
        if not state.tripped:
            return False
        if state.cooldown_until is None:
            return True
        return time.monotonic() < state.cooldown_until

    def snapshot(self) -> dict[str, dict]:
        """JSON-serializable view of all breakers. Every kind is always present
        (even when untouched) so the ``/health`` shape is stable for consumers
        like ``ensure.py`` that branch on it."""
        out: dict[str, dict] = {}
        for kind in BreakerKind:
            state = self._states[kind]
            self._prune(kind, state)
            out[kind.value] = {
                "open": self.is_open(kind),
                "reason": state.reason,
                "tripped_at": state.tripped_at,
                "cooldown_until": state.cooldown_until,
                "failures_in_window": len(state.recent_failures),
            }
        return out

    # ── internal ─────────────────────────────────────────────────────────

    def _prune(self, kind: BreakerKind, state: BreakerState) -> None:
        """Drop failure timestamps older than the kind's rolling window and cap
        the deque depth as a memory guard under a sustained storm. The window is
        per-kind: ``CHROME_CRASH_LOOP`` uses 300s, the others 120s."""
        cutoff = time.monotonic() - self._policies[kind].window_s
        failures = state.recent_failures
        while failures and failures[0] < cutoff:
            failures.popleft()
        # Hard cap: if somehow more than _max_recent survived (clock skew or a
        # huge burst within the window), drop the oldest.
        while len(failures) > self._max_recent:
            failures.popleft()

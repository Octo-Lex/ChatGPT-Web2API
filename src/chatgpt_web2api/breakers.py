"""Non-rate-limit circuit-breaker registry (ROADMAP Phase 4, PR1).

A small, pure-logic registry that tracks four failure classes the project has
no coherent story for today (rate-limit retry is already handled by
``resilience.py`` — deliberately not duplicated here):

  - ``auth_required``           — ChatGPT session expired (needs human login)
  - ``composer_send_readiness`` — composer / send-readiness repeated failures
  - ``cdp_reconnect``           — CDP websocket reconnect failures
  - ``chrome_crash_loop``       — Chrome restart loop

PR1 landed the registry and its ``/health`` snapshot. PR2 (this module's
current contract) makes it live: ``record_failure`` auto-trips a breaker once
its per-kind threshold is met, ``record_success`` implements half-open
recovery (a successful trial after cooldown closes the breaker), and
``first_open`` drives the REST/MCP fail-fast surface via ``CircuitOpenError``.

The signal wiring lives at the call sites: the driver records composer/CDP
failures with typed exceptions (``SendReadinessError`` / ``CDPReconnectError``)
and trips auth explicitly (``AuthExpiredError`` → ``trip(AUTH_EXPIRED)``);
``ChromeProcess`` records crash-loop restarts. The registry itself stays
pure logic — no I/O, no locks.

Design notes:
  - ``BreakerKind`` is a ``str`` enum so ``.value`` serializes straight into
    the ``/health`` JSON without an extra mapping layer.
  - Timestamps are ``time.monotonic()`` (not wall-clock) so cooldown math is
    immune to system clock changes and unit tests can drive a virtual clock.
  - Auto-trip fires from explicit ``BreakerKind`` calls only — never from a
    catch-all ``RuntimeError``. The auth path uses explicit ``trip()`` rather
    than ``record_failure()``, because auth is a single-shot "needs human
    login" condition, not a rolling-window failure count.
  - Single-process async server: no locks, matching ``APIServer``'s own
    unsynchronized counters (``_request_count``, ``_last_error``). Each process
    (REST, MCP) owns its own registry; there is no cross-process propagation.
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


class CircuitOpenError(RuntimeError):
    """Raised by the REST/MCP fail-fast preflight when a breaker is open.

    Carries the offending ``BreakerKind`` so the error-response layer can name
    it (using ``kind.value``) without re-interrogating the registry. Lives in
    this module — not ``cdp_driver.py`` — because it is a control-plane
    fail-fast signal raised *before* the driver is touched, not a driver
    failure.
    """

    def __init__(self, kind: BreakerKind) -> None:
        self.kind = kind
        super().__init__(f"Circuit open for {kind.value}")


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
# ``record_failure`` auto-trips once ``threshold`` failures land within
# ``window_s``; ``cooldown_s`` then governs the half-open recovery window
# (``None`` = indefinite, used only for auth, which trip()s explicitly).
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
    """Tracks failure history and trip state for each ``BreakerKind``.

    ``record_failure`` counts within the per-kind rolling window and auto-trips
    once the threshold is met. ``record_success`` clears failures and, if the
    breaker is past its cooldown (half-open), closes it — so a successful
    trial recovers the breaker. ``trip`` opens a breaker explicitly (used by
    the auth path). ``first_open`` drives the REST/MCP fail-fast preflight.
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
        """Append a failure timestamp, prune the rolling window, and auto-trip
        if the per-kind threshold is met. The auth path does NOT use this — it
        calls ``trip()`` directly, because auth is a single-shot condition, not
        a rolling count."""
        state = self._states[kind]
        state.recent_failures.append(time.monotonic())
        self._prune(kind, state)
        self._maybe_auto_trip(kind, state)

    def record_success(self, kind: BreakerKind) -> None:
        """Record a success. Clears the kind's failure history, and — if the
        breaker is tripped but past its cooldown (half-open) — closes it. This
        is the recovery path: one successful trial after cooldown recovers the
        breaker. A breaker still within cooldown, or an indefinite (auth) trip,
        is NOT reset by a success (its ``tripped`` flag stays set; only the
        failure history clears)."""
        state = self._states[kind]
        state.recent_failures.clear()
        if state.tripped and state.cooldown_until is not None:
            if time.monotonic() >= state.cooldown_until:
                self._states[kind] = BreakerState()

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

    def first_open(self) -> BreakerKind | None:
        """Return the first currently-open breaker kind, or ``None`` if all are
        closed/half-open. Callers (REST/MCP preflight) raise ``CircuitOpenError``
        themselves — the registry stays read-only here."""
        for kind in BreakerKind:
            if self.is_open(kind):
                return kind
        return None

    def snapshot(self) -> dict[str, dict]:
        """JSON-serializable view of all breakers. Every kind is always present
        (even when untouched) so the ``/health`` shape is stable for consumers
        like ``ensure.py`` that branch on it.

        ``cooldown_seconds_remaining`` is a server-side-computed *duration*
        (not an opaque ``time.monotonic()`` timestamp) so a separate process
        such as ``ensure.py`` can reason about cooldown without comparing
        monotonic clocks across the process boundary:

          - ``None``    → closed breaker, OR sticky/indefinite (e.g. auth_required)
          - positive    → seconds remaining for a currently-open timed trip
          - ``0.0``     → past cooldown (half-open window)

        Because ``None`` is overloaded, callers must infer an auth condition
        from ``kind == "auth_required" and entry["open"]``, never from
        ``cooldown_seconds_remaining is None``."""
        out: dict[str, dict] = {}
        now = time.monotonic()
        for kind in BreakerKind:
            state = self._states[kind]
            self._prune(kind, state)
            out[kind.value] = {
                "open": self.is_open(kind),
                "reason": state.reason,
                "tripped_at": state.tripped_at,
                "cooldown_until": state.cooldown_until,
                "cooldown_seconds_remaining": (
                    max(0.0, state.cooldown_until - now)
                    if state.cooldown_until is not None
                    else None
                ),
                "failures_in_window": len(state.recent_failures),
            }
        return out

    # ── internal ─────────────────────────────────────────────────────────

    def _maybe_auto_trip(self, kind: BreakerKind, state: BreakerState) -> None:
        """Trip the breaker if the in-window failure count meets the per-kind
        threshold. Uses the policy's cooldown (timed for composer/CDP/crash;
        ``None``/indefinite only for auth, which trip()s explicitly anyway)."""
        policy = self._policies[kind]
        if len(state.recent_failures) < policy.threshold:
            return
        # Already-open within cooldown: refresh the trip so the cooldown
        # restarts from the latest failure burst (sustained failures extend
        # the open window rather than letting it lapse mid-storm).
        reason = f"{policy.threshold} failures in {policy.window_s:.0f}s window"
        cooldown = policy.cooldown_s if policy.cooldown_s is not None else 0.0
        self.trip(kind, reason, cooldown_s=cooldown)

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

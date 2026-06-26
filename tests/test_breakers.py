"""Tests for the non-rate-limit breaker registry (Phase 4).

Pure unit tests — the registry is pure logic (stdlib only), so no mocking of
subprocesses or drivers is needed. A virtual clock drives cooldown/window math
the same way ``tests/test_ensure.py`` does.

PR1 pinned the plumbing contract; PR2 activates auto-trip + half-open recovery.
These tests cover both the inherited PR1 invariants and the new PR2 behavior.
"""

import chatgpt_web2api.breakers as breakers_mod
from chatgpt_web2api.breakers import (
    BreakerKind,
    BreakerRegistry,
    CircuitOpenError,
)


def _install_clock(monkeypatch, start: float = 1000.0):
    """Drive time.monotonic deterministically. Returns an advance(t) helper."""
    now = [start]
    monkeypatch.setattr(breakers_mod.time, "monotonic", lambda: now[0])

    def advance(dt: float) -> None:
        now[0] += dt

    return advance


# ── snapshot shape ──────────────────────────────────────────────────────


def test_snapshot_all_kinds_present():
    """Every BreakerKind is always in the snapshot, even on a fresh registry,
    so /health consumers can rely on a stable shape."""
    reg = BreakerRegistry()
    snap = reg.snapshot()
    assert set(snap.keys()) == {k.value for k in BreakerKind}
    # exactly the four expected exposure names
    assert set(snap.keys()) == {
        "auth_required",
        "composer_send_readiness",
        "cdp_reconnect",
        "chrome_crash_loop",
    }


def test_snapshot_closed_initially():
    """Fresh registry: nothing tripped, nulls, zero failures."""
    reg = BreakerRegistry()
    snap = reg.snapshot()
    for kind in BreakerKind:
        entry = snap[kind.value]
        assert entry["open"] is False
        assert entry["reason"] is None
        assert entry["tripped_at"] is None
        assert entry["cooldown_until"] is None
        assert entry["failures_in_window"] == 0


# ── failure counting & pruning ─────────────────────────────────────────


def test_record_failure_counts_in_window():
    """N failures within the window are all counted."""
    reg = BreakerRegistry()
    for _ in range(3):
        reg.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
    snap = reg.snapshot()["composer_send_readiness"]
    assert snap["failures_in_window"] == 3


def test_record_failure_prunes_old_entries(monkeypatch):
    """Failures older than the rolling window are dropped on the next record."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.record_failure(BreakerKind.CDP_RECONNECT)  # at t=1000
    advance(200.0)  # t=1200 — first failure is now outside the 120s window
    reg.record_failure(BreakerKind.CDP_RECONNECT)  # prunes the old one

    snap = reg.snapshot()["cdp_reconnect"]
    assert snap["failures_in_window"] == 1  # only the recent one survives


def test_chrome_crash_loop_uses_300s_window(monkeypatch):
    """CHROME_CRASH_LOOP is 3 restarts in **5 min** (ROADMAP Phase 4), not the
    2 min window the other three classes use. The window is per-kind, so:

      - failures at t=0, t=150, t=250 all count (3) for chrome_crash_loop
        (300s window), because t=250 − t=0 = 250s ≤ 300s;
      - the same spacing must NOT count as 3 for a 120s-window breaker
        (CDP_RECONNECT here), because t=250 − t=0 = 250s > 120s.

    This pins the policy mismatch the single global window used to hide."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    # t=1000
    reg.record_failure(BreakerKind.CHROME_CRASH_LOOP)
    reg.record_failure(BreakerKind.CDP_RECONNECT)
    advance(150.0)  # t=1150 — +150s from the first failure
    reg.record_failure(BreakerKind.CHROME_CRASH_LOOP)
    reg.record_failure(BreakerKind.CDP_RECONNECT)
    advance(100.0)  # t=1250 — +250s total from the first failure
    reg.record_failure(BreakerKind.CHROME_CRASH_LOOP)
    reg.record_failure(BreakerKind.CDP_RECONNECT)

    snap = reg.snapshot()
    # 300s window: all three (0s, 150s, 250s) survive → 3
    assert snap["chrome_crash_loop"]["failures_in_window"] == 3
    # 120s window: only the two at +150s and +250s survive (the t=1000 one is
    # 250s old by snapshot time) → 2
    assert snap["cdp_reconnect"]["failures_in_window"] == 2


def test_record_failure_auto_trips_at_threshold():
    """PR2 contract: record_failure auto-trips once the per-kind threshold is
    met within the rolling window. Below the threshold it stays closed."""
    reg = BreakerRegistry()
    # CDP threshold is 5; 4 stays closed.
    for _ in range(4):
        reg.record_failure(BreakerKind.CDP_RECONNECT)
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is False
    # The 5th trips it.
    reg.record_failure(BreakerKind.CDP_RECONNECT)
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is True


def test_record_success_clears_failure_history():
    """A success clears the failure run for that kind (but does not close a
    breaker still within its cooldown — see test_record_success_matrix)."""
    reg = BreakerRegistry()
    # 2 failures: below the chrome_crash_loop threshold of 3, so still closed.
    for _ in range(2):
        reg.record_failure(BreakerKind.CHROME_CRASH_LOOP)
    assert reg.snapshot()["chrome_crash_loop"]["failures_in_window"] == 2

    reg.record_success(BreakerKind.CHROME_CRASH_LOOP)
    assert reg.snapshot()["chrome_crash_loop"]["failures_in_window"] == 0


# ── trip / cooldown / reset ────────────────────────────────────────────


def test_trip_with_cooldown_open_then_half_open(monkeypatch):
    """A timed trip is open immediately and becomes half-open (closed) once the
    cooldown elapses."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.CDP_RECONNECT, "ws closed", cooldown_s=60.0)
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is True

    advance(59.0)  # still within cooldown
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is True

    advance(2.0)  # now at t=1061, past the 60s cooldown
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is False


def test_trip_immediate_auth_stays_open(monkeypatch):
    """Auth trips with cooldown_s=0 — it must stay open indefinitely until an
    external recovery (reset) clears it. This is the ROADMAP's 'require human
    browser login' intent: no automatic half-open."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True
    assert reg.snapshot()["auth_required"]["cooldown_until"] is None

    advance(99999.0)  # arbitrarily far into the future
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True  # still open


def test_trip_records_reason_and_tripped_at(monkeypatch):
    """The trip reason and timestamp are surfaced in the snapshot."""
    _install_clock(monkeypatch, start=500.0)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.COMPOSER_SEND_READINESS, "no composer", cooldown_s=30.0)
    snap = reg.snapshot()["composer_send_readiness"]
    assert snap["reason"] == "no composer"
    assert snap["tripped_at"] == 500.0
    assert snap["cooldown_until"] == 530.0
    assert snap["open"] is True


def test_reset_clears_a_tripped_breaker():
    """reset() returns a breaker to its untouched state."""
    reg = BreakerRegistry()
    reg.trip(BreakerKind.AUTH_EXPIRED, "expired", cooldown_s=0)
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True

    reg.reset(BreakerKind.AUTH_EXPIRED)
    snap = reg.snapshot()["auth_required"]
    assert snap["open"] is False
    assert snap["reason"] is None
    assert snap["tripped_at"] is None


# ── PR2: auto-trip, half-open recovery, fail-fast ─────────────────────


def test_record_failure_auto_trips_per_kind_threshold(monkeypatch):
    """Each kind trips at its own threshold; kind isolation holds — failures of
    one kind never trip another."""
    _install_clock(monkeypatch)
    reg = BreakerRegistry()

    # COMPOSER threshold is 3; 2 stays closed.
    reg.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
    reg.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
    assert reg.is_open(BreakerKind.COMPOSER_SEND_READINESS) is False
    # 3rd trips it.
    reg.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
    assert reg.is_open(BreakerKind.COMPOSER_SEND_READINESS) is True

    # CDP threshold is 5 — composer failures did not bleed into CDP.
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is False


def test_auth_trip_is_immediate_and_indefinite(monkeypatch):
    """Auth uses explicit trip() (not record_failure) and stays open
    indefinitely — no rolling window, no half-open recovery."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.AUTH_EXPIRED, "HTTP 401")
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True

    advance(99999.0)
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True  # never half-opens


# ── record_success 4-case matrix ──────────────────────────────────────


def test_record_success_closed_clears_failures_only(monkeypatch):
    """Case 1: a CLOSED breaker + success → clears failures, stays closed."""
    _install_clock(monkeypatch)
    reg = BreakerRegistry()
    # 2 failures (below threshold 3), still closed.
    reg.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
    reg.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
    assert reg.snapshot()["composer_send_readiness"]["failures_in_window"] == 2

    reg.record_success(BreakerKind.COMPOSER_SEND_READINESS)

    snap = reg.snapshot()["composer_send_readiness"]
    assert snap["failures_in_window"] == 0
    assert snap["open"] is False
    assert snap["reason"] is None  # never tripped


def test_record_success_within_cooldown_does_not_reset(monkeypatch):
    """Case 2: open TIMED breaker still within cooldown + success → does NOT
    reset. The breaker stays tripped; only failures clear."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.COMPOSER_SEND_READINESS, "no composer", cooldown_s=300.0)
    assert reg.is_open(BreakerKind.COMPOSER_SEND_READINESS) is True

    advance(100.0)  # within the 300s cooldown
    reg.record_success(BreakerKind.COMPOSER_SEND_READINESS)

    assert reg.is_open(BreakerKind.COMPOSER_SEND_READINESS) is True
    snap = reg.snapshot()["composer_send_readiness"]
    assert snap["open"] is True
    assert snap["reason"] == "no composer"  # trip state preserved


def test_record_success_after_cooldown_resets_half_open(monkeypatch):
    """Case 3: open TIMED breaker past cooldown (half-open) + success → RESETS
    to closed. This is the recovery path: one successful trial closes it."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.CDP_RECONNECT, "ws closed", cooldown_s=120.0)
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is True

    advance(121.0)  # past the 120s cooldown → half-open
    assert reg.is_open(BreakerKind.CDP_RECONNECT) is False  # half-open = not open

    reg.record_success(BreakerKind.CDP_RECONNECT)

    snap = reg.snapshot()["cdp_reconnect"]
    assert snap["open"] is False
    assert snap["reason"] is None  # fully reset
    assert snap["tripped_at"] is None


def test_record_success_indefinite_auth_does_not_reset(monkeypatch):
    """Case 4: open INDEFINITE (auth) breaker + success → does NOT reset. Auth
    only recovers via explicit reset(), never via record_success."""
    advance = _install_clock(monkeypatch)
    reg = BreakerRegistry()

    reg.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True

    advance(99999.0)
    reg.record_success(BreakerKind.AUTH_EXPIRED)

    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True
    snap = reg.snapshot()["auth_required"]
    assert snap["open"] is True
    assert snap["reason"] == "401"


# ── first_open + CircuitOpenError ─────────────────────────────────────


def test_first_open_returns_none_when_all_closed():
    """No open breakers → first_open returns None."""
    reg = BreakerRegistry()
    assert reg.first_open() is None


def test_first_open_returns_first_open_kind(monkeypatch):
    """first_open returns the first open kind in BreakerKind order; callers
    raise CircuitOpenError themselves (registry stays read-only)."""
    _install_clock(monkeypatch)
    reg = BreakerRegistry()
    reg.trip(BreakerKind.CDP_RECONNECT, "ws closed", cooldown_s=60.0)

    kind = reg.first_open()
    assert kind is BreakerKind.CDP_RECONNECT


def test_circuit_open_error_carries_kind():
    """CircuitOpenError carries the offending kind and renders kind.value."""
    err = CircuitOpenError(BreakerKind.COMPOSER_SEND_READINESS)
    assert err.kind is BreakerKind.COMPOSER_SEND_READINESS
    assert "composer_send_readiness" in str(err)
    assert isinstance(err, RuntimeError)


def test_is_open_false_for_never_tripped():
    """An untouched breaker is never open."""
    reg = BreakerRegistry()
    for kind in BreakerKind:
        assert reg.is_open(kind) is False


# ── isolation between kinds ────────────────────────────────────────────


def test_kinds_are_independent():
    """Tripping one breaker does not affect the others."""
    reg = BreakerRegistry()
    reg.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)

    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is True
    for kind in (
        BreakerKind.COMPOSER_SEND_READINESS,
        BreakerKind.CDP_RECONNECT,
        BreakerKind.CHROME_CRASH_LOOP,
    ):
        assert reg.is_open(kind) is False

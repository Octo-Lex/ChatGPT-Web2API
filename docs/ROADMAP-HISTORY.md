# ChatGPT-Web2API Roadmap History

> **Status:** Historical archive. Do not use this file to sequence new work.
>
> The active execution plan lives in [ROADMAP.md](ROADMAP.md). This file records
> the stabilization roadmap that shaped the repository from June through July
> 2026 and was superseded on 2026-09-01 after a fresh deep-dive of `master`.
>
> For the complete pre-archive roadmap text and its detailed rationale, inspect
> `docs/ROADMAP.md` at master commit
> `497527dceabfa3f95961e23c291e618c5570f1ac`.

## Why this was archived

The previous roadmap had become mostly a historical record. Its major phases
had already shipped, while the actual next work had moved to runtime contracts,
session management, attachment safety, compatibility testing, and hardened
operation. Keeping completed phases in the active plan made sequencing less
clear and encouraged new planning material to accumulate beside stale goals.

The architectural decisions below remain important; only their status changed
from "roadmap" to "shipped baseline."

---

## Phase 0 — Broad stabilization / PR #9

**Outcome:** shipped.

The composer redesign, Phase-2 completion work, and owned-tab isolation landed.
`owned` became the safe default tab mode, with adoption kept behind explicit
configuration.

Key invariant established:

```text
browser ownership and mutation isolation must fail closed rather than silently
fall back to a shared tab
```

---

## Phase 1 — Observability gaps

**Outcome:** substantially shipped; residual observability work was folded into
later reliability work.

The repository established an honest `/health` model with live Chrome/CDP state
and explicit `starting`, `healthy`, `degraded`, and `broken` states. Silent
best-effort paths were progressively instrumented rather than treated as
success.

---

## Phase 2 — SSE as the recommended persistent MCP transport

**Outcome:** shipped.

SSE replaced stdio-per-session as the recommended persistent deployment mode.
Integration tests exercised real MCP initialization, tool listing, reads, and
chat. The fresh-chat completion deadlock discovered during this phase was fixed
by resolving the conversation ID during completion observation rather than
waiting for a stale pre-loop value.

---

## Phase 3 — `chatgpt-web2api ensure`

**Outcome:** shipped.

`ensure` became the repository-owned point-in-time reconciler for REST + MCP/SSE.
It intentionally remained a reconciler rather than a long-running supervisor.
The degraded-REST policy was designed to avoid destructive browser restarts
while CDP might still recover.

Key lifecycle invariant:

```text
REST owns Chrome.
MCP/SSE attaches.
Hooks and supervisors invoke repo-owned lifecycle logic rather than reimplement it.
```

---

## Phase 4 — Non-rate-limit circuit breakers

**Outcome:** shipped.

The project added distinct breaker policies for:

- authentication expiry;
- composer/send-readiness failures;
- CDP reconnect failures;
- Chrome crash loops.

Breaker state became visible in health and error surfaces, and recovery was made
failure-class-aware instead of treating every degraded state as a restart
signal.

Historical follow-ups identified during this work included:

- transient backend 404 handling (later resolved);
- clearer `requests_served` semantics;
- non-401 backend-error observability;
- MCP `/messages` trailing-slash behavior;
- deferring configurable breaker thresholds until field data justifies them.

---

## Phase 5 — `cdp_driver.py` decomposition

**Outcome:** shipped.

The original driver monolith was split into focused collaborators:

```text
backend_client.py
cdp_transport.py
chatgpt_dom.py
completion_detector.py
```

`CDPDriver` intentionally remained the orchestration/interception facade and
monkeypatch seam used by the collaborators and tests. The post-extraction audit
found that further line-count reduction would require moving lifecycle/tab
ownership and reconnect policy, which was explicitly deferred as a separate,
high-risk decision rather than pursued for cosmetic size reduction.

That conclusion still stands: further extraction is not a roadmap priority by
itself.

---

## Phase 6 — Operational documentation

**Outcome:** shipped.

The repository added OS-supervision guidance, a production runbook, and a
documentation index. The operating model distinguishes point-in-time reconcile
from continuous supervision and documents safe restart behavior.

---

## Phase 7 — Parallel multi-tab operation

**Outcome:** shipped, with later session-pool work building on it.

The repository generalized cross-process locking to per-target mutation locks,
added explicit Chrome ownership, enforced owned-tab requirements in parallel
mode, and documented the one-Chrome/many-tabs deployment model.

Key safety invariant:

```text
parallel mode requires owned tabs + per-target locking + fail-closed ownership
checks as one bundle
```

Subsequent work added the experimental MCP session-affine driver pool with lazy
owned-tab materialization, bounded capacity, TTL cleanup, lease accounting, and
account-level throttling. That newer pool is the starting point for the active
roadmap's transport-neutral session runtime; it should be generalized rather
than reimplemented.

---

## What carried forward into the active roadmap

The historical roadmap established several principles that remain binding:

1. Stabilize and instrument behavior before large structural changes.
2. Keep browser lifecycle ownership explicit.
3. Fail closed when session/turn identity is uncertain.
4. Retry observation/reconciliation when safe; never blindly resend a mutation.
5. Keep concurrency bounded and model shared account/browser constraints.
6. Treat upstream ChatGPT Web behavior as volatile and diagnosable, not stable.
7. Avoid refactors whose only measurable outcome is lower line count.

The active plan begins at **Phase 8** in [ROADMAP.md](ROADMAP.md).
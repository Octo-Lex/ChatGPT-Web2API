# ChatGPT-Web2API Roadmap

> **Status:** Active. Authored 2026-06-25 after a scope-correction review that
> removed work already shipped (honest `/health`, rate-limit retry) and moved
> `ensure` earlier because `/health` is already trustworthy.
>
> This document is the single source of truth for sequencing. Do not start work
> out of order without updating this file first.

## Guiding principles

- **Stabilize behavior before moving code.** The `cdp_driver.py` split (Phase 5)
  comes *after* the breaker policy (Phase 4). We instrument the monolith, prove
  the behavior is stable, then move it.
- **Lifecycle logic lives in the repo-owned CLI, not in hooks.** ZCode hooks are
  thin one-liners. Testable, idempotent logic belongs in `chatgpt-web2api ensure`.
- **REST owns Chrome. SSE attaches.** This invariant holds across every phase.
  Nothing else launches Chrome.
- **Don't re-scope shipped work.** If a phase's deliverable already exists in
  code, the phase is reduced to only its genuine gaps (see Phase 1 and Phase 4).

---

## Phase 0 — Merge PR #9  ✅ MERGED 2026-06-25

**Goal:** close the broad stabilization branch.

PR #9 (`fix/phase2-nontext`) — composer redesign, Phase-2 completion, tab
isolation — merged as squash commit `9ebb236`. All 9 CI checks green
(lint, secret-scan, build, 6× test matrix across ubuntu/macos/windows ×
3.11/3.12). The blocking tab-isolation concern raised in review was
**already resolved** in the branch: `owned` is the default tab mode, the
`adopt` path is gated behind explicit opt-in (`config.py`, `cdp_driver.py`).

### Pre-merge gates (all passed)

```text
pytest -m "not e2e"               ✅
ruff check .                      ✅
gitleaks / static gates           ✅
clean working tree                ✅
CI: 9/9 jobs SUCCESS              ✅
```

**Constraint honored:** no lifecycle / SSE / bootstrap work in PR #9. Those
start in Phase 2+.

---

## Phase 1 — Finish observability gaps

**Scope correction:** honest `/health` is **already shipped**
(`api_server.py:_handle_health`). It computes `chrome_running` and
`driver_connected` fresh on each call, returns all 8 fields, and distinguishes
`starting` / `healthy` / `degraded` / `broken`. Do not rebuild it.

### Remaining work only

1. **Regression tests for zombie states** — ✅ largely done. The pinned cases
   already exist in `tests/test_health.py`:
   - listener alive, Chrome alive, driver disconnected → `/health` returns
     `degraded` (`test_health_degraded_when_chrome_alive_but_driver_disconnected`)
   - listener alive, Chrome dead → `/health` returns `broken`
     (`test_health_broken_when_chrome_dead`)
   - PR3 added further coverage: an open breaker never forces `broken`, a
     disconnect-degraded is not worsened, and `starting`/`healthy` downgrade
     correctly. Audit remaining gaps before adding more; most are covered.

2. **Targeted debug logging for meaningful silent failures.** Many
   `except Exception: pass` exist (~20, mostly `cdp_driver.py`). Most are
   defensible best-effort cleanup, but several swallow errors that mask real
   failures. Add `logger.debug` (not warning — these are best-effort paths) at:
   - token refresh swallowed exceptions
   - heartbeat failure
   - CDP reader task exit
   - reconnect classification / skip decisions
   - tab registry reclaim / record / clear failures
   - best-effort cleanup failures (debug level only)

**Home for the silent-exception work is here, not split across Phase 5.** Phase 5
may move the code, but Phase 1 makes failures visible first.

---

## Phase 2 — Make SSE the recommended ZCode transport  ✅ DONE

**Goal:** replace stdio-per-session as the recommended mode. Eliminate the
process multiplier (N ZCode sessions × stdio = N MCP children × N tabs).

### Deliverables — all shipped

1. ✅ **Document SSE config** (recommended) — README now presents SSE first
   with the `chatgpt-web2api-sse` snippet and launch command.
2. ✅ **Document stdio** as compatibility / dev-debug mode only — README
   repositions stdio under "Alternative," noting one MCP child per session.
3. ✅ **Integration tests** for SSE (`tests/test_e2e_sse.py`, e2e-gated) —
   real `sse_client` + uvicorn over a non-8090 port:
   - initialize handshake
   - list tools
   - list models
   - one chat call (also the live regression for the #10/#11 deadlock)
   - repeated fresh connections (asserts no per-connection CDP target growth)

### Reframed constraint

```text
Recommended ZCode mode is SSE-only:
  one persistent MCP server
  no per-session MCP child
  no per-session Chrome/tab spawning
```

The vague "detect many stdio processes and warn" item is **dropped from v1** —
under-specified, cross-cutting. Optional later.

### Known follow-up — ✅ RESOLVED (was discovered Phase 0, 2026-06-25)

```text
MCP/SSE chat_completion can complete server-side but timeout client-side on
response delivery. Short SSE tools work.
```

**Resolved by #11** (fix `70f014a`): root cause was a completion-detection
deadlock — on a new chat, `conv_id_for_check` was empty for the whole poll
loop, disabling the backend `end_turn` fallback. With the DOM action-button
selector drifted (3rd time), no completion signal fired and the loop ran to
the 120s deadline. Fix resolves `conv_id_for_check` mid-loop from the live
URL. Live SSE `chat_completion` now completes in 2–12s (was 120s+/timeout).

Diagnosis details: [issue #10 comment](https://github.com/Octo-Lex/ChatGPT-Web2API/issues/10#issuecomment-4796158081).
Remaining follow-up: the DOM `has_action` selector is still dead — tracked in #12.

---

## Phase 3 — Add `chatgpt-web2api ensure`  ✅ DONE

**Goal:** let ZCode hooks bootstrap the full stack with a thin one-liner.

Shipped as `chatgpt-web2api ensure` in `src/chatgpt_web2api/ensure.py`, wired
into the `__main__.py` subcommand dispatch. Point-in-time reconcile: checks
REST + SSE, starts whichever is missing, verifies SSE via real MCP handshake,
exits 0 when ready. Lock-protected (SSE-port-keyed startup lock, bounded
contention). Degraded-REST policy honored (20s poll before restart). No
watchdog loop. 30 unit tests in `tests/test_ensure.py`.

Restart hardening (#16): Unix listener discovery uses a `lsof` → `ss` → `fuser`
fallback chain (no single-tool dependency); `_stop_listener` returns False (and
logs an error) when a port is occupied but no PID can be found, so the caller
aborts the restart instead of launching into an occupied port. SSE
handshake-failed path stops the existing listener before relaunch.

### Command

```text
chatgpt-web2api ensure [--rest-port 8080] [--mcp-sse-port 8090]
```

Slots into the existing `{"start", "inject-cookies", "doctor"}` subcommand
dispatch in `__main__.py`.

### Contract

1. Check REST `/health`.
2. Reconcile REST per the **degraded-REST policy** below.
3. Wait until REST is ready (`healthy`, or `starting` + Chrome/CDP/driver
   all connected — a cold bootstrap that hasn't served a chat yet is ready
   enough for SSE to attach).
4. Check MCP/SSE on `:8090`.
5. If missing, start MCP/SSE.
6. Verify MCP/SSE: initialize succeeds + list tools succeeds.
7. Exit: `0` when ready, nonzero with clear diagnostic when not.

### Degraded-REST policy  *(PINNED — do not restart REST immediately)*

REST owns Chrome. Restarting REST runs `taskkill /F /T` on Chrome
(`chrome.py`) — visible window flash, 10–15s cold restart, may lose an
in-progress page. A naive restart on `degraded` is destructive flap.

```text
status=missing    → start REST
status=broken     → restart REST (Chrome is down; REST will relaunch Chrome)
status=degraded   → WAIT + re-poll first; restart only after repeated failed polls
status=healthy    → no-op
```

**v1 degraded policy:**

```text
degraded:
  poll every 2s for up to 20s
    if becomes healthy → continue
    if still degraded after 20s → restart REST
```

Reason: `degraded` (Chrome alive, driver disconnected) may be a transient CDP
reconnect. REST has reconnect logic (`dbc7985`); give it room before bouncing
the browser.

### SSE watchdog scope  *(PINNED — point-in-time, not continuous)*

`ensure` is a **point-in-time reconcile**, not a supervisor.

```text
ZCode hook runs ensure
  → ensure makes REST + SSE healthy NOW
  → ensure exits
if SSE dies later → next hook / next session reruns ensure
```

- Do **not** add a Python watchdog loop in v1.
- The SSE/MCP path currently has **no** crash-recovery (unlike REST's
  `start_monitor` for Chrome). This is acceptable for v1: SSE crash is rare and
  the hook re-runs on next session.
- Continuous supervision belongs in Phase 6 (optional OS-service docs), not here.

### Design constraints

```text
idempotent
lock-protected (lock files prevent duplicate starts)
safe to run repeatedly from ZCode hooks
no complex lifecycle logic in hook config
REST remains Chrome owner
MCP/SSE remains one persistent attaching process
```

---

## Phase 4 — Non-rate-limit breaker policy  ✅ DONE

**Scope correction:** rate-limit retry/backoff is **already substantially
implemented** in `resilience.py` (transparent retry, `Retry-After` respected,
jitter, `max_attempts=3`, dismiss-popup, persistent-limit escape → parseable
`RateLimitError`). Do not rebuild it.

### Breaker classes — all shipped

1. **Auth expired** (`auth_required`)
   - trips immediately on `AuthExpiredError`; no retry storm
   - sticky (no half-open) — requires human browser login
   - exposed as `auth_required` in `/health`
2. **Composer / send-readiness** (`composer_send_readiness`)
   - trip after 3 failures in a 120s window; 300s cooldown
3. **CDP reconnect failures** (`cdp_reconnect`)
   - 5 failures in a 120s window → 120s cooldown
4. **Chrome crash loop** (`chrome_crash_loop`)
   - 3 restarts in a 300s window → 300s cooldown

Thresholds/windows/cooldowns are hardcoded in `_DEFAULT_POLICIES`
(`breakers.py`); see the deferred follow-up E below before making them
configurable.

### Exposure

```text
/health          status downgrade + open_breakers + per-breaker snapshot
REST errors      503 circuit_open (with kind)
MCP errors       isError result (circuit_open, kind=...)
logs
```

**Kept before the big refactor (Phase 5).** Behavior is now instrumented and
proven stable; Phase 5 may move the code.

### Sequencing — three PRs (all merged)

Phase 4 was split to keep review tight: ship the infrastructure with no
behavior change, then wire the real failure signals, then add operator-facing
status policy.

- ✅ **PR1 (#18) — breaker registry + `/health` exposure.** Shipped
  `src/chatgpt_web2api/breakers.py`: a `BreakerRegistry` keyed by `BreakerKind`
  with `record_failure` / `record_success` / `trip` / `is_open` / `snapshot`.
  `/health` gained a `breakers` snapshot field. Zero behavior change at the
  time: `/health` always reported all breakers closed. Proved the plumbing
  risk-free.
- ✅ **PR2 (#19) — wire failure signals + fail-fast + half-open recovery.** Added
  the typed exceptions the composer/CDP paths need (`SendReadinessError`,
  `CDPReconnectError`), wired `record_failure`/`trip` at the detection sites,
  enforced thresholds, added REST fail-fast (`_error_response` 503
  `circuit_open`) + MCP `(circuit_open, kind=...)` result, and wired half-open
  recovery (`record_success` after a confirmed send / successful reconnect; auth
  stays indefinite until explicit `reset()` via `recover_auth()`). Includes the
  post-lock re-check and AUTH_EXPIRED recovery probe in REST/MCP preflight. The
  registry is per-process (driver-owned via DI). `CircuitOpenError` lives in
  `breakers.py`. Runtime-validated on `dd97f91`.
- ✅ **PR3 (#20) — `/health` status policy + breaker-aware ensure.** An open
  breaker now **downgrades** `starting`/`healthy` → `degraded` (never `broken`;
  a disconnect-degraded stays degraded). New `/health` fields: a top-level
  `open_breakers` current-state list (distinct from the historical/latching
  `last_error`) and a per-breaker `cooldown_seconds_remaining` duration (not an
  opaque monotonic timestamp) so `ensure.py` can reason about cooldown across
  the process boundary. `ensure` is breaker-aware: degraded + open
  `auth_required` → **exit 2** (login needed, no REST restart, no SSE
  reconcile); degraded + open timed breaker → wait cooldown+grace then
  recover/restart (with a cooldown-boundary re-fetch); degraded without breaker
  info → legacy 20s-poll-then-restart. Adds narrow **ensure-only** config
  tunables (`EnsureConfig`: poll interval/budget, cooldown grace) via
  `ensure_*` flat keys + `W2A_ENSURE_*` env; config `port`/`cdp_port` never
  override explicit `run_ensure` args. Exit codes: `0` ready, `1` generic
  failure, `2` auth/login needed. Runtime-validated on `2668243`.

```
PR1 (#18) →  registry + snapshot (no behavior change)
PR2 (#19) →  signals + typed exceptions + fail-fast + half-open recovery
PR3 (#20) →  status policy + breaker-aware ensure + ensure-only tunables
```

> **Tunables scope note:** PR3 shipped **ensure-only** tunables, NOT
> `BreakerPolicy` threshold/window/cooldown config. `W2A_BREAKER_*` keys do not
> exist. Threshold constants stay in `breakers.py` until real-world tuning data
> shows the defaults need adjustment (deferred follow-up E).

### Known follow-ups (not yet roadmap phases)

Discovered during Phase 4 runtime validation. Each is a separate, small PR — do
not bundle them into the Phase 5 refactor.

```text
A. _fetch_text transient 404 retry   ✅ RESOLVED (#23)
   cdp_driver._fetch_text can hit a backend 404 immediately after send, before
   the just-created conversation is persisted server-side. Treated as a bounded
   transient retry (NOT a breaker). Landed in backend_client.py (#22 extraction)
   via #23 — the recommended sequence (extract first, then fix in the new module)
   was followed exactly.

B. requests_served semantics
   /health.requests_served currently counts requests accepted/handled, not
   successful responses. Either document it as "accepted/handled" or add a
   distinct successful_requests counter. Do NOT change the existing counter's
   meaning silently.

C. Non-401 backend-error observability
   Only HTTP 401 trips a breaker (AUTH_EXPIRED). Other backend errors (404/5xx)
   raise a bare RuntimeError and never fail-fast. Decide whether persistent
   backend 5xx/404 should stay observability-only, become a breaker signal, or
   get a distinct /health field. Keep transient errors out of breakers unless
   persistence is proven.

D. MCP /messages trailing-slash redirect
   The SSE-announced endpoint is /messages?session_id=... but the server 307-
   redirects to /messages/?... (trailing slash); a 307 can drop the JSON body
   depending on the client. Harden (announce the canonical URL) or document the
   redirect. Real MCP clients add the slash today.

E. BreakerPolicy threshold config
   Still defer W2A_BREAKER_* threshold/window/cooldown tunables unless
   production data shows the defaults need tuning. Do NOT add them speculatively.
```

---

## Phase 5 — Split `cdp_driver.py`  ✅ COMPLETE 2026-06-27

**Goal:** reduce bug density *after* behavior is stable. `cdp_driver.py` was
~2986 lines mixing CDP transport, ChatGPT DOM logic, completion detection, and
token/session/conversation fetch. **Reduced to 1558 lines** (nearly halved);
the rest was extracted into focused, singly-responsible modules with **no
behavior change**. Each extraction was verified by a full test pass, a green CI
matrix, and a live `"ok"` send against a real ChatGPT account.

### Landed PRs

```text
#22 backend_client.py   token / session / conversation fetch (+ project/memory CRUD)
#23 _fetch_text 404 retry   bounded transient-404 retry (follow-up A, in backend_client)
#24 cdp_transport.py     CDP websocket / session / reconnect primitives
#25 chatgpt_dom.py       composer / selectors / send-readiness / rate-limit dismiss
#26 completion_detector.py   Phase-1 appear loop + Phase-2 stream/completion loop
```

### Final architecture — hub-and-spoke interception

`CDPDriver` is now an **orchestration facade + interception hub** plus the
lifecycle/tab-ownership core. The extracted modules (`backend_client`,
`cdp_transport`, `chatgpt_dom`, `completion_detector`) hold a back-reference
to the driver and route every transport/state/peer call through
`self._driver.<method>` — *not* their own implementations. This is deliberate:
`CDPDriver` remains the single monkeypatch seam, so test patches on
`driver.X` propagate into the collaborators. This contract is asserted by
`tests/test_chatgpt_dom.py` and `tests/test_completion_detector.py` and
documented in each module's docstring.

### Post-extraction audit — remaining delegators are load-bearing

A full import-site audit (every method on `CDPDriver`, across `src/` and
`tests/`) found **zero delete-safe facade methods**. The remaining
driver-facing methods are the intentional compatibility/interception seams used
by collaborator modules and the test suite — they are **not** technical debt in
this architecture. Keeping them is what made #22–#26 safe. `cdp_driver.py` is
at its natural floor for the hub-and-spoke design; further line reduction is
not available without restructuring the interception contract.

### Deferred — Group C lifecycle / tab-ownership (separate initiative)

The only remaining extraction target is the **lifecycle core**: `connect` /
`reconnect` / `close`, heartbeat (`_heartbeat_loop`, `_live_target_ids`), tab
ownership (`_create_owned_tab`, `_find_owned_tab_ws`, `_adopt_existing_chatgpt_tab`,
`_find_page_ws`, `_browser_cdp`), token refresh, and the breaker-policy touch
points. This is **deferred as a separate high-risk initiative**, not a Phase 5
continuation. It is the first extraction that simultaneously crosses lifecycle
ownership, reconnect semantics, heartbeat, the target registry, browser-domain
CDP, and breaker policy. It should not be started on the momentum of the
facade-split work; it deserves a fresh "should we do this at all?" decision
after Phase 5 is closed.

```text
Phase 5 complete. The driver has been reduced to an orchestration/interception
hub plus lifecycle/tab-ownership core. A post-extraction audit found no
delete-safe facade methods: the remaining driver-facing methods are intentional
compatibility and monkeypatch seams used by collaborator modules and tests.
Further extraction would require moving lifecycle/tab ownership and reconnect
policy, which is deferred as a separate high-risk initiative.
```

### Rules (honored)

```text
no behavior changes in any split PR             ✅
tests moved/added with modules                  ✅
public CDPDriver facade kept stable (no caller breakage)  ✅
```

---

## Phase 6 — Optional OS-level supervision docs

**Goal:** support always-on deployments outside ZCode. Comes last because ZCode
hooks are the primary path.

✅ **PR1 (#28) — OS supervision guide.** Added `docs/os-supervision.md` covering
systemd (Linux), launchd (macOS), and Task Scheduler / NSSM (Windows), with two
styles: `ensure` on a timer (mirrors the ZCode hook) and `start` as a long-lived
service. Documents the recommended reconcile command
(`chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222`),
the `/health`-gated restart policy (restart on process exit, not on `degraded`),
log capture, and the env-var reference. **Docs only** — no supervisor scripts
installed, no daemonization code, no package entrypoint changes. Linked from
`docs/deployment.md` (Option 4).

✅ **PR2 (#29) — Production runbook.** Added `docs/runbook.md`: startup
checklist, `/health` field reference with exact `status` conditions, common
failure modes mapped to symptoms/fixes, the four breakers with thresholds and
cooldowns (source-cited, hardcoded — no env override), the auth-recovery flow,
the safe-restart rule (restart on process exit, not on `degraded`), log
collection, and post-deploy validation (incl. the exact-output `"Reply with
exactly: ok"` sanity send). Linked from `docs/deployment.md` (Option 5). All
field names/state values/thresholds verified against source.

✅ **PR3 (#30) — Documentation index.** Added `docs/INDEX.md`: a "which doc
should I read?" routing table plus grouped listings (operating guides,
architecture/decisions, reverse-engineering/internals) with one-line purposes
for every doc in `docs/`. Linked from `README.md` (Documentation section
refreshed) and `docs/deployment.md` (top pointer). Makes the Phase 6 ops docs
(os-supervision, runbook) discoverable without adding operational surface.

Docs only — no code:

```text
Windows Scheduled Task
NSSM / service wrapper
Linux systemd
macOS launchd
```

### Positioning

```text
ZCode users       → use the ensure hook (Phase 3)
always-on / server users → use OS supervision (this phase)
```

---

## Final sequencing

```text
0. Merge PR #9                              ✅
1. Observability gaps: zombie regression tests + silent-failure logging   (partial — see Phase 1)
2. SSE recommended transport (docs + integration tests)  ✅
3. ensure command + ZCode hook docs                        ✅
4. non-rate-limit breaker policy                           ✅ (PR1 #18 / PR2 #19 / PR3 #20)
5. split cdp_driver.py                                     ✅ (#22–#26; complete 2026-06-27)
6. optional OS-supervision docs                            (last, docs-only)
```

Known follow-ups (A–E, listed under Phase 4) slot in around Phase 5 as small
standalone PRs — do not bundle them into the refactor. The recommended
post-Phase-4 order: extract `backend_client.py` (Phase 5 PR1, no behavior
change) → fix `_fetch_text` 404 (follow-up A) in the new module.

This version removes the stale health work, avoids duplicate rate-limit work,
drops the under-specified stdio-process warning, moves `ensure` earlier
(`/health` is trustworthy), and pins the two Phase 3 ambiguities (degraded
restart, SSE watchdog) before any `ensure` code is written.

# ChatGPT-Web2API Roadmap

> **Status:** Active. Refreshed 2026-09-01 after a deep-dive of current
> `master`, the reliability/session-pool work, active branches, and PR #49.
>
> This document is the **single source of truth for forward sequencing**.
> Completed Phases 0–7 and their detailed rationale are archived in
> [ROADMAP-HISTORY.md](ROADMAP-HISTORY.md).
>
> Do not start major work out of order without updating this file first.

## North star

ChatGPT-Web2API is a **deterministic, capability-aware, fail-closed session
runtime over ChatGPT Web**. REST and MCP are adapters around that runtime; they
are not the architecture itself.

The project should optimize for correctness, isolation, diagnosability, bounded
resource use, and safe degradation before expanding feature breadth.

---

## Work classes

Every roadmap item should be classified as one of:

| Class | Meaning |
|---|---|
| **BLOCKER** | Correctness or security work that must be resolved before dependent features merge. |
| **FOUNDATION** | Shared runtime infrastructure that later features should build on. |
| **OPERATIONS** | Release, health, recovery, deployment, observability, or security hardening. |
| **FEATURE** | User-facing capability that should not bypass required blockers/foundations. |

A feature may carry more than one class.

---

## Guiding principles

1. **Correctness and security before feature breadth.** A feature that can
   return the wrong turn, leak host data, corrupt session state, or consume
   unbounded resources is not merge-ready.
2. **Fail closed rather than guess.** Uncertain tab ownership, turn identity,
   attachment state, or capability state must produce a typed failure rather
   than silent fallback.
3. **Observe effects, not attempted actions.** A CDP click or navigation call is
   not success. Confirm browser/backend state and use the existing send
   acknowledgment + turn-reconciliation machinery.
4. **Retry observation when safe; never blindly resend a mutation.** Recovery
   may repeat reads, probes, or reconciliation. A send must not be replayed just
   because observation timed out.
5. **Browser/session resources are bounded.** Tabs, leases, queues, upload bytes,
   and recovery attempts require explicit limits and backpressure.
6. **Local host authority is privileged.** Server-local filesystem access,
   browser profiles, cookies, and CDP are credentials/capabilities, not ordinary
   chat inputs.
7. **Treat ChatGPT Web drift as normal.** External selectors, routes, response
   shapes, and timing assumptions need named contracts, probes, and evidence.
8. **Do not refactor for line count alone.** The current `CDPDriver` facade is an
   intentional orchestration/interception seam. Further extraction needs a
   concrete reliability or ownership benefit.
9. **One active roadmap.** Historical planning is archived; overlapping forward
   plans should be folded into this document or retired.

---

# Shipped baseline — Phases 0–7

The following capabilities are already part of the repository baseline and are
not future roadmap work:

- real-Chrome/CDP browser automation;
- honest four-state REST `/health`;
- persistent MCP/SSE transport;
- `chatgpt-web2api ensure` reconciliation;
- typed circuit breakers for auth, composer readiness, CDP reconnect, and
  Chrome crash loops;
- `CDPDriver` decomposition into backend, transport, DOM, and completion
  collaborators while preserving the driver facade;
- OS-supervision and production-runbook documentation;
- owned-tab isolation and per-target parallel locking;
- IdentityListener capture of the client-generated user-message UUID;
- TurnAnchor-based exact-turn reconciliation with conservative fallbacks;
- model-aware first-content / stream-idle completion budgets;
- experimental MCP session-affine driver pooling with bounded leases, TTL,
  capacity accounting, and account-level throttling;
- reactive drift diagnostics and redacted evidence capture.

See [ROADMAP-HISTORY.md](ROADMAP-HISTORY.md) for the historical phase record.

---

# Current known gaps

These are not a second roadmap; they are the concrete facts driving Phase 8.

1. **`master` must be made green again.** The latest inspected CI run had all
   six OS/Python functional test jobs and secret scan passing, but lint failed
   and the build was skipped.
2. **Repository truth has drifted.** Version/release metadata and several docs
   lag the actual architecture/test surface.
3. **Fresh-chat HTTP 500 needs a current live canary.** A regression was observed
   in August; no source fix has landed on `master` since the July baseline, so
   current runtime behavior must be re-certified rather than assumed fixed.
4. **MCP pool health work already exists on
   `feat/sse-pool-status-endpoint`.** Review that implementation before
   designing another observability path.
5. **PR #49 is not merge-ready as currently designed.** Local path attachments
   can expose readable host files through an always-visible chat capability;
   multipart size enforcement happens after spooling; cleanup/state handling
   and the attachment E2E also need correction.

---

# Phase 8 — Stabilize current reality

**Class:** BLOCKER + OPERATIONS

**Goal:** restore a trustworthy baseline before adding more architecture or
features.

## Deliverables

### 8.1 Re-certify fresh-chat behavior

Run the minimal live fresh-chat canary against current `master`:

```text
new chat
→ exact known prompt
→ send acknowledgment
→ conversation-id resolution
→ exact-turn reconciliation
→ successful REST/MCP result
```

If the August HTTP 500 reproduces, treat it as the highest-priority runtime
regression and fix it before roadmap foundation work. If it no longer
reproduces, record the result as an upstream-drift incident and retain a
regression canary.

### 8.2 Restore green `master`

- fix current lint failure(s);
- ensure all normal CI jobs complete successfully;
- keep E2E opt-in and separate from ordinary CI;
- do not cut the baseline release from a red branch.

### 8.3 Review existing MCP pool health branch

Review `feat/sse-pool-status-endpoint` rather than rebuilding it. Its existing
SSE `/health` surface reports pool capacity, slot state, leases, account
breaker, and shutdown state.

Before landing, decide:

- which fields are operator-safe to expose remotely;
- whether session keys need redaction/hashing;
- how this independent MCP health surface will later participate in aggregate
  runtime health.

### 8.4 Resolve repository truth drift

Update documentation and metadata to match current `master`, including at least:

- README test/status claims;
- architecture/module descriptions;
- MCP session-pool status and limitations;
- changelog/release status;
- roadmap references;
- stale automated policy assumptions (for example branch-name expectations).

### 8.5 Cut a representative baseline release

After green CI and doc reconciliation, cut the next non-1.0 release representing
what the repository actually ships. Release engineering details may be improved
incrementally, but versioning must stop lagging the codebase by multiple major
capability generations.

## Exit criteria

```text
fresh-chat canary status known and recorded
master normal CI green
existing MCP health branch reviewed/dispositioned
docs/version describe current architecture accurately
new baseline release cut from a green commit
```

---

# Phase 9 — Canonical runtime model

**Class:** FOUNDATION

**Goal:** make browser/session semantics independent of REST/MCP transport
objects.

External adapters should translate into a small internal domain model instead
of reaching directly into browser mechanics.

## Core types

Names are illustrative; keep the model narrow and behavior-driven.

```text
ChatTurnRequest
ChatMessage / ContentPart
ConversationRef
AttachmentSpec
TurnContext
TurnResult
CapabilitySet
BridgeError
```

The model must represent facts the runtime actually needs:

- message/system context;
- requested conversation/project/GPT context;
- model selection intent;
- attachments and their provenance;
- session/affinity identity;
- streaming/progress preference where relevant;
- the final correlated conversation/turn identity;
- typed failure information.

## Stable error taxonomy

Subsystem boundaries should use typed errors/codes rather than bare
`RuntimeError` where practical. Initial vocabulary:

```text
AUTH_REQUIRED
UPSTREAM_RATE_LIMIT
UPSTREAM_SCHEMA_CHANGED
DOM_CONTRACT_BROKEN
CDP_DISCONNECTED
OWNED_TAB_REQUIRED
TAB_LOST
SESSION_CAPACITY_EXHAUSTED
GENERATION_TIMEOUT
TURN_RECONCILIATION_FAILED
CAPABILITY_UNSUPPORTED
UPLOAD_REJECTED
UPLOAD_TIMEOUT
LOCAL_FILE_FORBIDDEN
RESOURCE_LIMIT_EXCEEDED
```

Do not create a giant hierarchy speculatively. Add codes when a caller or
operator can take a meaningfully different action.

## Boundary rule

```text
REST adapter ─┐
              ├─> canonical runtime request ─> browser/session runtime
MCP adapter ──┘
```

The browser/session runtime must not need to know which adapter originated the
turn.

## Exit criteria

- chat send paths for REST and MCP translate into the shared request model;
- browser send/reconciliation consumes shared turn context;
- new attachment/session work has one internal representation;
- typed error mapping exists at adapter boundaries;
- no behavior regression in existing tests/live canary.

---

# Phase 10 — ChatGPT Web compatibility contract

**Class:** FOUNDATION

**Goal:** centralize and continuously validate assumptions about the volatile
ChatGPT Web surface.

## 10.1 Contract registry

Create a focused compatibility layer for observed upstream behavior:

```text
chatgpt_contract/
  selectors.py
  routes.py
  shapes.py
  capabilities.py
  probes.py
  fingerprints.py
```

The exact module layout can differ; the important rule is that external
assumptions become named contracts rather than scattered literals.

## 10.2 Selector contracts

Prefer three tiers:

```text
1. stable attributes/test IDs
2. accessibility semantics
3. structural/behavioral inference
```

Avoid accumulating arbitrary fallback selectors. Each contract should be able
to report what strategies were attempted and what matched.

Examples:

```text
composer
send_button
stop_button
action_button
attachment_button
attachment_input
attachment_pending_state
conversation_route
```

## 10.3 Startup / preflight probes

Before declaring mutation capability healthy, probe facts such as:

```text
session/auth available
owned target valid
composer discoverable
composer accepts text
send affordance discoverable
conversation route recognizable
identity listener ready
backend conversation projection readable
```

Do not perform destructive sends as a startup probe.

## 10.4 Per-capability health

Represent partial drift explicitly:

```text
chat: healthy
auth: healthy
projects: degraded
memories: healthy
attachments: unavailable
turn_reconciliation: healthy
```

A broken optional capability must not automatically take down unrelated chat
operations.

## 10.5 Observed contract version

Expose a bridge-owned certification identifier such as:

```text
2026-09-a
```

This is **not** an upstream ChatGPT version. It means “the observed web contract
against which this build/fixture set was last certified.”

## Exit criteria

- critical selectors/routes/shapes have named contracts;
- capability probes produce structured diagnostics;
- optional feature drift can degrade independently;
- contract fixtures can be tagged to an observed-contract identifier.

---

# Phase 11 — Transport-neutral session runtime

**Class:** FOUNDATION

**Goal:** generalize the existing MCP session-affine pool into shared browser
session infrastructure rather than building a second pool.

## Starting point

Reuse the proven MCP pool concepts already in `master`:

```text
PENDING → ACTIVE → CLOSING → DISOWNED
bounded capacity
owned tabs
per-session call lock
lease accounting
TTL cleanup
account-level throttle breaker
```

The next abstraction should be transport-neutral, for example:

```text
BrowserSessionManager
or
SessionRouter
```

Naming matters less than ownership semantics.

## Responsibilities

- allocate/reuse owned tabs/drivers;
- preserve conversation/session affinity;
- issue bounded leases;
- serialize operations that share one session;
- allow independent sessions to run in parallel when safe;
- count pending/active/closing capacity correctly;
- expire idle sessions;
- drain gracefully on shutdown;
- expose lease/capacity diagnostics;
- enforce account-wide throttling independently from per-session locks.

## Non-goals

- do not equate tab count with account quota;
- do not create unlimited sessions because Chrome can create more tabs;
- do not duplicate pool implementations for REST and MCP;
- do not weaken owned-tab fail-closed behavior.

## Exit criteria

- REST and MCP can acquire browser/session context from the same runtime layer;
- session affinity survives adapter differences;
- capacity and lease lifecycle are observable;
- shutdown cannot silently abandon owned tabs/leases;
- singleton mode remains available and behavior-compatible.

---

# Phase 12 — Recovery coordinator and backpressure

**Class:** FOUNDATION + OPERATIONS

**Goal:** make retries, recovery, capacity, and overload behavior one coherent
policy rather than independent local loops.

## 12.1 Recovery coordinator

Coordinate:

```text
breaker state
CDP reconnect
owned-tab reacquisition
Chrome restart ownership
session rematerialization
auth-required state
turn observation/reconciliation
```

Possible high-level states:

```text
HEALTHY
DEGRADED
RECOVERING
HUMAN_ACTION_REQUIRED
FAILED
```

Do not force all existing breakers into one monolithic breaker. The coordinator
orchestrates recovery decisions while preserving failure-class-specific state.

## 12.2 Recovery budgets

Bound repeated repair attempts. Examples of limits that may be configurable:

```text
reconnect attempts / window
session rematerializations / window
Chrome restarts / window
total recovery wall time
```

When the budget is exhausted, fail clearly instead of entering a
restart/reconnect loop.

## 12.3 Backpressure

All session/worker capacity must be bounded:

```text
max active sessions
max pending acquisitions
acquire timeout
optional bounded request queue
```

When capacity is exhausted, surface an explicit retryable failure. Do not let
requests accumulate invisibly.

## 12.4 Idempotency rule

This remains non-negotiable:

```text
safe: retry reads / probes / reconciliation
unsafe: automatically resend a user mutation because observation timed out
```

## Exit criteria

- overload produces bounded, explicit backpressure;
- recovery loops have finite budgets;
- send retries cannot duplicate turns;
- recovery/capacity state appears in diagnostics/health;
- failure codes tell callers whether retry, wait, or human action is appropriate.

---

# Phase 13 — Attachment subsystem

**Class:** BLOCKER + FOUNDATION + FEATURE

**Goal:** land file attachments as a first-class turn capability without
introducing host-file disclosure, unbounded spooling, cross-request state, or
ambiguous send behavior.

PR #49 is useful implementation research but should not merge unchanged.

## 13.1 Separate attachment provenance

Two fundamentally different capabilities must remain distinct:

### Client-provided attachment

```text
client bytes
→ bridge-owned bounded staging file/object
→ ChatGPT composer
```

This is ordinary attachment input.

### Server-local file

```text
caller names host pathname
→ bridge reads server filesystem
→ ChatGPT composer
```

This is privileged host authority.

Server-local paths must be **disabled by default**. If supported, require an
explicit gate (for example `W2A_ENABLE_LOCAL_FILES=1`) plus configured allowed
roots. Resolve real paths and enforce containment after symlink resolution.

## 13.2 Resource limits during ingestion

Enforce limits while bytes are being received/written, not after the complete
part is on disk:

```text
per-file bytes
aggregate request bytes
file count
field/body limits where appropriate
```

Abort and clean the partial object immediately on limit violation.

## 13.3 Deterministic ownership and cleanup

Temporary attachment resources need one lifecycle owner and a structural
`try/finally` covering success, errors, early validation returns, client
disconnects, and task cancellation.

## 13.4 Per-turn attachment state

Do not use a persistent driver-global `_file_upload_active` flag.

Attachment state belongs to the current turn/session context, with an explicit
state machine such as:

```text
ATTACHING
→ ATTACHED
→ UPLOADING
→ READY_TO_SEND
→ SUBMITTED
```

Timeout while pending must fail; it must not trigger a speculative second send.

## 13.5 Browser evidence

Observe concrete UI state where possible:

- requested attachment input accepted;
- expected attachment chips/cards appeared;
- upload/progress state settled;
- send control is ready;
- existing send acknowledgment confirms submission.

English body text such as `File upload pending` may remain a fallback signal,
not the sole state machine.

## 13.6 E2E validity

The live attachment E2E must prove file **contents** reached the model. Generate
an unpredictable token only inside the attachment, ask for the token contained
in the file, and assert that token in the response. Never put the expected token
in the prompt itself.

## Exit criteria

```text
no host-path access by default
allowed-root containment for privileged local files
streaming/count/aggregate resource bounds
cleanup on cancellation and every terminal path
per-turn attachment state (no driver-global flag)
no second-click on pending timeout
content-dependent live E2E
attachment-specific typed errors
```

---

# Phase 14 — Compatibility laboratory

**Class:** FOUNDATION + OPERATIONS

**Goal:** detect upstream drift before it becomes an opaque production failure.

## Four test layers

### Layer 1 — unit/state-machine tests

Continue strong deterministic coverage for:

```text
selectors/contracts
turn anchors
completion states
breakers
locks
leases
recovery policy
attachment lifecycle
```

### Layer 2 — recorded/replay fixtures

Capture sanitized evidence such as:

```text
CDP events
DOM compatibility snapshots
backend projection shapes
conversation graphs
navigation/readiness states
```

Store fixtures by observed-contract version.

### Layer 3 — named contract tests

Examples:

```text
test_contract_composer
test_contract_send_button
test_contract_conversation_route
test_contract_identity_capture
test_contract_conversation_projection
test_contract_attachment_input
```

A drift failure should say which external assumption moved.

### Layer 4 — minimal live certification

Keep the live suite deliberately small and high-signal:

```text
session/auth
fresh chat
streaming completion
conversation continuation
exact-turn correlation
project-scoped navigation
parallel/session isolation
attachment content verification
```

Do not replace deterministic tests with a large live-prompt suite.

## Primary correctness metric

The most important metric is not raw availability. It is:

```text
silent wrong-turn response rate = 0
```

If exact causal correlation cannot be established, fail instead of returning a
plausible stale answer.

## Exit criteria

- contract fixtures exist for critical upstream assumptions;
- live certification can be run as a small explicit canary;
- drift failures identify the moved contract;
- recorded fixtures support regression testing without a live account.

---

# Phase 15 — Hardened operation

**Class:** OPERATIONS

**Goal:** make controlled network/server deployments explicit about authority,
identity, and resource boundaries.

## Required areas

### Network exposure

- preserve loopback-safe defaults;
- bring MCP network exposure closer to REST's fail-closed posture;
- require deliberate authentication/authorization for hardened remote mode;
- keep Chrome CDP private to localhost/private runtime boundaries.

### Scoped authority

Evolve beyond an all-or-nothing reachable MCP client. Preserve the existing
READ / WRITE / DESTRUCTIVE distinction and add separate treatment for host-local
capabilities such as local files.

### Auditability

Record structured security-relevant events without logging sensitive content:

```text
principal/session
operation class
conversation/project target where safe
local-file capability use
result/error code
resource/capacity decisions
```

### Browser credentials

Treat the Chrome profile, cookies/session data, and CDP access as credentials:

- dedicated OS user/profile;
- restrictive filesystem permissions;
- no unrelated-user profile sharing;
- documented revoke/cleanup procedure.

### Resource limits

Document and enforce safe bounds for:

```text
sessions/tabs
queues
uploads
diagnostics artifacts
recovery attempts
browser/worker resources
```

### Observability

Define how REST health, MCP/SSE pool health, breakers, session leases, and
compatibility status are viewed together. This may be aggregation rather than a
single process owning every health fact.

## Exit criteria

- remote operation requires an explicit hardened security posture;
- host-local capabilities have independent authorization/gating;
- CDP is never the public control surface;
- security-relevant operations are auditable;
- resource limits and health semantics are documented and testable.

---

# Phase 16 — Feature expansion

**Class:** FEATURE

**Goal:** resume broad ChatGPT Web feature work only after the runtime can absorb
upstream drift and new browser state safely.

Candidate work includes:

- web search mode;
- image generation workflows;
- richer project/file operations;
- additional Custom GPT capabilities;
- other ChatGPT Web features discovered through the compatibility-contract
  process.

Every new browser feature must ship with:

```text
capability contract
startup/runtime probe where appropriate
typed errors
resource/security classification
deterministic tests
minimal live certification
independent degradation behavior
```

Do not add a feature merely by inserting another selector or retry loop into
`CDPDriver`.

---

# Sequencing and gates

```text
Phase 8  Stabilize current reality
   ↓
Phase 9  Canonical runtime model
   ↓
Phase 10 ChatGPT Web compatibility contract
   ↓
Phase 11 Transport-neutral session runtime
   ↓
Phase 12 Recovery coordinator + backpressure
   ↓
Phase 13 Attachment subsystem
   ↓
Phase 14 Compatibility laboratory
   ↓
Phase 15 Hardened operation
   ↓
Phase 16 Feature expansion
```

Some test-fixture work from Phase 14 may begin earlier when it directly protects
Phases 9–13, but Phase 14 remains the point where the compatibility laboratory
becomes a supported subsystem.

Security fixes for an active feature PR do **not** wait for their numbered
phase. For example, PR #49 must not merge with unrestricted host-local path
access simply because the full attachment subsystem is Phase 13.

---

# Merge gates for roadmap work

## General

Every non-doc roadmap PR should answer:

1. What runtime invariant does this change establish or preserve?
2. What failure mode is now typed/observable instead of guessed?
3. What resource/state does this code own, and where is cleanup guaranteed?
4. Can this change duplicate a user mutation during retry/recovery?
5. What deterministic regression test proves the invariant?
6. What live certification is necessary, if any?

## Browser-facing changes

Require:

```text
named compatibility contract
structured diagnostic on mismatch
fail-closed behavior
recorded fixture where practical
live canary for high-risk send/navigation/attachment changes
```

## Concurrency changes

Require:

```text
explicit ownership
lock-order documentation
bounded capacity
cancellation-safe lease cleanup
shutdown/drain behavior
no shared-tab fallback in parallel mode
```

## Attachment/local-file changes

Require:

```text
proven provenance model
no unrestricted server-local paths
streaming resource enforcement
deterministic cleanup
per-turn state
content-dependent E2E
```

---

# Not priorities right now

The following should not displace Phases 8–15 without new evidence:

- another large `CDPDriver` line-count refactor;
- more ad-hoc selector fallbacks without compatibility contracts;
- speculative breaker tunables without field data;
- unbounded browser/tab scaling;
- broad new UI features before attachments/session/recovery foundations are
  correct;
- a second session pool rather than generalizing the one already shipped.

---

# Success criteria for the next architecture cycle

The cycle is successful when all of the following are true:

```text
master is green and released from a truthful baseline
fresh-chat behavior has a current certification result
REST and MCP share canonical turn/session semantics
critical ChatGPT Web assumptions are named and probed
session capacity/leases are transport-neutral and bounded
recovery has finite budgets and cannot auto-resend mutations
local host files are inaccessible by default
attachments have deterministic cleanup and per-turn state
upstream drift fails by capability instead of taking down unrelated features
record/replay + minimal live certification exist
silent wrong-turn responses remain a hard zero-tolerance failure class
hardened remote operation has explicit auth/authority/resource boundaries
```

That is the point at which expanding the ChatGPT Web feature surface becomes the
safe next optimization target.
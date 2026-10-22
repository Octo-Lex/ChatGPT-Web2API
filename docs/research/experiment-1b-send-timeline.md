# Experiment 1B — Normal-Send Causal Timeline (2026-09-21)

Controlled mutation experiment under the Phase 0 authorization: exactly one
sentinel turn (`PHASE0-1B-d69d36b8`) through the real bridge pipeline of an
isolated second instance, instrumented end-to-end. Outcome class: **A —
outgoing-UUID evidence is the earliest causal acknowledgment signal, ahead
of every DOM signal.**

## Setup

- Experiment bridge: second instance, REST 8081, config
  `captures/phase0/config-1b.json` (owned tabs, parallel per-target locks,
  pool disabled). Live bridge on 8080 untouched throughout.
- Browser: **shared Chrome/CDP 9222 with the live bridge** — recorded
  environmental limitation (the 9223 listener is Edge/MSN, not an
  authenticated ChatGPT Chrome; an independent profile cannot be logged in
  without user credentials). Observers attached only to the experiment
  bridge's owned tab (`34372C4A…`, diffed against a pre-start snapshot).
- Disposable project: `Web2API Phase0 Controlled Recon`
  (`g-p-6ab0df421d288191b97e2781eb7e16a1`, memory_scope `project_v2`).
- Code exercised: working tree `feat/sse-pool-status-endpoint` (master +
  `/health` telemetry) — not literally `master`.
- Instrumentation: two CDP observer sessions (Network recorder; DOM sampler
  at ~100 ms), bridge log (ms-precision), post-hoc authed projection fetch.
  Harness: `scripts/phase0_experiment_1b.py`; raw:
  `captures/phase0/exp1b-20260921-104735.json`; bridge log:
  `captures/phase0/exp1b-bridge.log`.
- No failure injected; model/effort not changed (observed: effort chip
  "High", runtime slug `gpt-5-6-thinking`, reply "ACK", REST 200 in
  15.4 s).

## Timeline (offsets from the send mutation: POST `/f/conversation`)

| Offset | Event |
| ---: | --- |
| −8460 ms | REST request fired at bridge (includes navigation) |
| −2180 ms | speculative POST `/conversation/init` (project-page conversation shell, before typing) |
| −578 ms | POST `/f/conversation/prepare` |
| −530 ms | composer filled (first sample >50 chars) |
| −55 ms | typing completed (bridge log) |
| **0 ms** | **POST `/backend-api/f/conversation` — message dispatch** |
| +139 ms | IdentityListener captures outgoing user UUID |
| +141 ms | click `Runtime.evaluate` returns ("Message sent") |
| −39 ms | backend user-node `create_time` (server clock; skew ≈ −40 ms, not the ~+5.1 s the June `SKEW_TOLERANCE` note assumed) |
| +437 ms | composer cleared; stop-button appears |
| +437 ms | DOM user message + `data-message-id`; DOM assistant message (same 100 ms tick) |
| +758 ms | bridge detector: "Assistant message appeared" |
| +1879 ms | POST `/sentinel/req` |
| +3830 ms | `data-message-model-slug` = `gpt-5-6-thinking` visible |
| +3832 ms | conversation id resolved |
| +6910 ms | REST response returned |

## Evidence table

| Evidence | Observed? | Offset | What it proves |
| --- | --- | ---: | --- |
| JS click returned | yes | +141 ms | local JS execution returned |
| Composer cleared | yes | +437 ms | UI reacted |
| DOM user count +1 | yes | +437 ms | mounted DOM changed |
| Outgoing UUID captured | yes | **+139 ms** | submission carried causal identity |
| Backend contains UUID | yes | post-hoc | remote user node exists — **node id == captured UUID** |
| Assistant generation started | yes | +437/+758 ms | model turn began (stop-button; detector) |

The four previously conflated claims separate cleanly here: CDP command
executed (click returned, +141), React accepted the submit (composer
cleared, +437), backend created the USER turn (UUID in projection),
assistant generation started (stop/detector, +437–758).

## Key contract findings

1. **The send path is now `/backend-api/f/conversation` (+
   `/f/conversation/prepare`), wrapped in a live sentinel flow**
   (`chat-requirements/prepare` → `ping` → `finalize` → `sentinel/req`).
   The plain conversation POST is not what carries the message.
   `/conversation/init` fires after the send (+3.66 s), and a speculative
   shell init precedes typing. The July-era IdentityListener still captured
   the client-message UUID from this path unchanged.
2. **Identity chain is exact**: outgoing client-message-id == backend
   mapping node id == message id (`b2de19c3-f641-47cd-a4f8-1d7b2ecc473b`).
   UUID capture (+139 ms) precedes composer clearing and DOM-count change
   (+437 ms) by ~300 ms — the A-outcome. IdentityListener is the strongest
   *and* earliest acknowledgment signal; DOM count is strictly slower.
3. **Backend clock skew ≈ −40 ms** on this run — the June-era
   `~+5.1 s lead` assumption baked into `turn_anchor.py`'s degraded-mode
   freshness floor is stale and should be re-derived (it is env-overridable
   but the default encodes the old measurement).
4. **Generation-state classes absent**: `.result-thinking` and
   `.result-streaming` never appeared at 100 ms sampling during a
   `gpt-5-6-thinking` generation. Generation was observable via stop-button,
   assistant-node appearance, and `data-message-model-slug` (+3.8 s) instead.
   One controlled run; the generation-lifecycle experiment remains queued,
   now with a concrete negative data point.
5. **User and assistant DOM nodes appear in the same 100 ms tick** (+437 ms)
   — the assistant placeholder mounts with the user turn, before any text.

## Scope and exclusions

- One run, one sentinel, shared Chrome (timing co-tenancy), single
  account/plan, working tree ≠ master, 100 ms DOM sampling (sub-100 ms
  class toggles could be missed — though stop-button and text were caught).
- No failure injection (by design — ambiguity tests come later, offline
  first). Nothing here tests reconnect/rate-limit replay.
- The disposable project now contains one conversation (`6ab0dff4…`) with
  the sentinel turn; retained for the remaining controlled experiments.

## Consequence for #51

The A-outcome supports the recorded design direction directly: primary
acknowledgment = outgoing UUID captured → backend UUID reconciliation;
`SEND_CONFIRMED` on match; DOM count/composer-clearing demoted to
diagnostics. The deterministic transport replay fixtures remain the next
step before implementation.

# ADR 0001: Automation backend — stay on CDP, defer Chrome Extension

Status: Accepted (2026-06-23)

## Context

The bridge drives `chatgpt.com` via Chrome DevTools Protocol (CDP): a Python
process connects to Chrome's `--remote-debugging-port`, evaluates JS in the
page via `Runtime.evaluate`, types into the composer, and polls the DOM.

Two pressures prompted a re-evaluation of this choice:

1. **Multi-session interference.** Two simultaneous bridge sessions sharing one
   Chrome tab corrupt each other — when one navigates the shared tab, the
   other's in-memory conversation id goes stale and its next send lands in the
   wrong conversation. A Chrome Extension solves this *structurally*: each
   content-script instance is bound to its `tab.id` by the framework, so
   cross-tab operation is impossible by construction. We engineered the same
   guarantee in CDP (owned-tab-per-process default, `tab_mode`, `_owns_target`),
   which raised the question of whether the extension model is fundamentally
   cleaner.

2. **Anti-detection.** The project's core thesis (per the README) is to "run
   inside a real browser to dodge the bot path." `--remote-debugging-port` is
   itself a known automation fingerprint. A user-installed extension driving
   the composer is plausibly a more "human-shaped" automation surface than a
   debug protocol — closer to the thesis.

## Decision

**Stay on CDP for the current architecture. Treat the Chrome Extension (MV3)
as a separate backend research track, not current implementation scope.**

Concretely:
- Keep the CDP driver as the only automation backend.
- Keep the owned-tab-per-process isolation that fixed the interference bug.
- Introduce a `DriverBackend` abstraction *only if/when* a second backend is
  prototyped — do not pre-build the abstraction speculatively.

## Rationale

**The interference bug is already fixed.** Owned-tab-per-process (Commit B)
makes two drivers get two DOMs by construction within the CDP model. The
extension's structural isolation benefit is now a nice-to-have, not a fix for a
live bug. Pivoting would re-solve an already-solved problem at high cost.

**The migration cost is larger than the call count suggests.** ~69 CDP
touch-points exist in `cdp_driver.py`, but the real cost is preserving
semantics across a message boundary:
- Extension content scripts run in an isolated world; MAIN-world injection via
  `chrome.scripting.executeScript` requires a *file or self-contained
  function*, not arbitrary runtime strings like our `_js(...)` helper.
- Native messaging requires a host-registration layer; content scripts can't
  call it directly and must relay through the service worker.
- The hard parts — `_js_strict` typed errors, Phase-2 streaming/progress
  polling, diagnostic capture, session lifecycle — must all survive that
  boundary.
- This is a `CDPDriver → ExtensionDriver → protocol → service worker → tab
  registry → execution shim → serialization → error taxonomy → diagnostics`
  rewrite, not a port.

**Anti-detection is a hypothesis, not an engineering fact.** No public
evidence shows ChatGPT's bot path specifically keys on
`--remote-debugging-port`. Extensions carry their own fingerprints
(permissions, content-script artifacts, injected MAIN-world code, messaging
behavior). The safe claim is narrower: an extension is a more
product-aligned *shape*, not provably less detectable. It deserves a spike,
not a platform rewrite.

**Hybrid is worst-of-both-worlds.** Splitting writes (extension) from reads
(CDP) reintroduces the cross-plane state mismatch the owned-tab fix just
eliminated. The only acceptable hybrid is a `DriverBackend` interface with
`CDPBackend` now and a future `ExtensionBackend` — i.e. a pluggable backend,
not mixed behavior in one driver.

## Triggers to revisit

Pivot only if one of these becomes true:

1. **Observed CDP-specific failures** — the same account/session succeeds
   manually or via an extension spike but consistently fails through CDP.
2. **Remote debugging becomes operationally painful** — users reject the
   `--remote-debugging-port` flag, or Chrome starts warning/blocking it.
3. **Extension spike proves low complexity** — a prototype supports
   create/continue chat, Phase-2 polling, diagnostics, and REST/MCP
   round-trip with reliability comparable to CDP.
4. **Write-path evidence** — extension-driven composer events demonstrably
   succeed where CDP-driven events fail, under identical account/browser
   conditions.

## Consequences

- The CDP driver remains the sole backend; no abstraction debt introduced.
- The owned-tab isolation (`tab_mode`) is the supported multi-session model.
- An MV3 extension backend is a documented future epic with clear entry
  criteria, not an abandoned idea.

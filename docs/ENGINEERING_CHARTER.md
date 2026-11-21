# Engineering Charter — ChatGPT-Web2API

**Status:** proposed for maintainer review; not yet adopted. 2026-11-21.
**Basis:** [Capability Register](CAPABILITY_REGISTER.md), pinned master 497527dceabfa3f95961e23c291e618c5570f1ac. This charter defines desired behavior, not a claim that every guarantee is already implemented or qualified.

## 1. Mission and identity

Provide the broadest practical, supported programmatic access to ChatGPT Web through an authenticated real browser session, using stable and simple REST and MCP interfaces, while preserving the value of the user's existing account, projects, conversations, memory and Custom GPTs. Maximize safe successful work, resilience to upstream changes and ease of operation with the least necessary complexity.

This remains a stateful, agent-oriented browser bridge, not a replacement for the official OpenAI API, a reimplementation of ChatGPT, or an autonomous account administrator. Respect upstream entitlement, account access, security controls and usage limits. Do not promise exact model instruction-following, unlimited service, full API parity, or provider availability.

The original proposition (real Chrome/CDP, no separate upstream API key/manual token extraction, straightforward setup, REST/SSE/MCP, conversation continuity, projects, memory, GPTs and account management within authorized scope) is retained. No research or architectural pattern automatically changes this product scope.

## 2. Protected capabilities

The Capability Register is the baseline of existing user-visible and operational behavior. It distinguishes implementation from qualification and documents gaps.

**G00 — Capability non-regression.** Keep existing legitimate capabilities, default usability, public contracts and compatibility unless a deliberate maintainer decision authorizes a migration, temporary limitation or retirement. Improving safety may require replacing an unsafe mechanism, but not casually dropping the capability it served. Preserve tests of user-visible behavior, and retain negative evidence even when replacing obsolete internal fixtures. Where upstream no longer provides a capability, surface the limitation honestly, preserve the contract where possible and record the evidence; never fabricate success.

The charter sets policy; the register identifies what must be protected; the roadmap sequences approved work. An Architecture Pattern Register (APR), when established, records how and why a pattern is considered/adopted/implemented/verified. Keep these authority boundaries separate. Follow the APR doctrine of explicit promotion, evidence lineage and scoped qualification; no pattern is adopted here merely by naming it.

## 3. Priority and design rules

| ID | Principle | Requirement |
|---|---|---|
| G01 | Correct context and ownership | Verify intended account/session, project and conversation route before mutation; ensure the active target and lease are valid. Never operate on a foreign tab or Stop as a Send fallback. |
| G02 | Effect integrity | Do not blindly replay a possibly accepted external mutation. Distinguish dispatch, acceptance, verified result and unknown outcome. At-most-one in a process is not exactly-once or durable idempotency. |
| G03 | Result integrity | Return only the correct turn's content, with accurate terminal state; no silent stale output, truncation, cross-turn mixing or false success. |
| G04 | Safe recovery first | Recover or continue when sufficient trusted evidence exists. Fail closed where safety cannot be established; a missing optional signal alone is not grounds to reject a valid operation. |
| G05 | Proportional guards | Enforce necessary invariants, not universal availability/agreement of every observer. Guard reviews must compare false accept and false reject cases and recovery cost. |
| G06 | Observation is not authority | Separate route and effect proof, progress, completion, result identity, and delivery. Counts, UI controls, URLs and timestamps have limited and distinct evidentiary roles. |
| G07 | Delivery semantics | Distinguish replaceable buffered content from irrevocable emitted text. Preserve actual incremental SSE while allowing canonical corrections before delivery. |
| G08 | Compatibility and drift | Keep existing supported REST/MCP contracts, model-aware wait budgets, legacy/current UI compatibility when feasible, diagnostics, and validated operational controls. |
| G09 | Evidence before expansion | Keep source, tests, exact-head CI, operator-reported runs, reproducible live tests and operational acceptance separate. Negative findings remain visible. |
| G10 | Minimum justified complexity | Stabilize behavior before moving code. Prefer targeted changes and bounded resource use over speculative general frameworks. |

P0: safety, data integrity, privacy and authorization are non-negotiable. P1: successful legitimate requests, functional breadth, interoperability, recoverability and compatibility are core product outcomes, not expendable optimizations. P2: latency, throughput, additional supported capabilities and maintenance cost. P3: cosmetic refactoring. Priorities guide tradeoffs; they do not authorize feature removal or lower proof requirements for sensitive effects.

## 4. Operation contract and proportional evidence

Conceptual lifecycle (not a required new class hierarchy):

RECEIVED → ROUTE_VERIFIED → COMPOSER_READY → SEND_ATTEMPTED → ACKNOWLEDGED → GENERATING → COMPLETION_CONFIRMED → RESULT_RECONCILED → DELIVERED.

Record terminal error, timeout, cancellation and unresolved effect where applicable. Capture the request's intended route, lease and correlation before acting; invalidate stale continuation on loss of route authority. Account/profile persistence and a healthy CDP connection do not establish request ownership. Keep REST owner/SSE attacher and owned-session boundaries intact.

| Boundary | Sufficient evidence / action | What must not justify success |
|---|---|---|
| Before mutation | Valid route and ownership/lease, safe explicit Send target, composer readiness, a viable method to correlate the turn | Old cached route alone, broad submit selector, missing proof treated as zero |
| Acceptance | Captured causal user identity or qualified same-turn observation; classify uncertainty | A dispatched JS event, a cleared editor alone, hydration of prior controls |
| Progress | Scoped DOM semantic text, attributable UI/network state or verified backend evidence | Generic progress on another turn |
| Completion | Anchored terminal result; or an explicitly qualified same-turn fallback with sufficient finality and content evidence | Copy/Stop absence alone, global timestamp recency, a foreign branch |
| Final result | Verified turn/branch identity, reliable content, consistent completion semantics | Latest-by-time selection without correlation; ambiguity; empty response masquerading as success |
| Delivery | Transport-specific commitment and truthful terminal/error state | HTTP 200, partial text, an internal generator ending, or a path/ID alone |

Evidence is scoped to the invariant it can prove, not assigned an unconditional global ranking. Backend selectors may be wrong or stale; DOM may drift; URL may lag. Contradictions that invalidate ownership or attribution must not be ignored. Transient absence of an auxiliary observer can be recoverable. Independent count vectors fix max-masking, but count growth cannot by itself distinguish hydration of old controls from a new logical turn. Never turn an unobserved count into measured zero. Require a sufficient trusted baseline or qualified alternative; retain fail-closed when neither is available.

## 5. Text, non-text and transport contract

- REST non-streaming and accumulated MCP tool results: keep internal observations provisional; when a complete correctly anchored authoritative result is available, deliver it as a whole, not an incompatible DOM prefix plus suffix. Do not substitute an unproven canonical answer. Any implementation must retain existing response fields and MCP progress notification behavior.
- REST SSE: only publish attributable content and preserve genuine incremental delivery; before any emission, replace provisional text freely using a verified result. After emission, append only compatible verified continuations. If the final verified result contradicts previously delivered text, report an explicit stream failure (not stop/success); preserve HTTP/SSE compatibility limitations. Do not claim previous bytes can be retracted.
- UI labels, toolbar text and reasoning chrome are not answer content. Prefer semantic answer containers and scoped fallback; do not strip legitimate content without evidence.
- Keep non-text handling as a capability: distinguish intermediate tool/reasoning nodes from verified terminal non-text, preserve existing retrieval/placeholder semantics until a supported richer contract is designed; never claim an image/file was delivered when only text or metadata was returned.
- Define truth at the bridge boundary as faithful supported representation of ChatGPT's actual result, not enforcement of the prompt's requested wording. Exact sentinel outputs are a test oracle with model-compliance caveats, not a universal product rule.

## 6. Recoverability and bounded resource policy

| Effect state | Recovery rule |
|---|---|
| Confirmed not dispatched | Safe read/preflight retry and re-establish route if allowed. |
| Proven rejected / no effect | Retry only within authorization, rate-limit and time budgets. |
| Dispatched, acceptance unknown | Preserve causal correlation; do not replay blindly. Read/reconcile if possible; report unresolved effect and retry semantics honestly when not resolved. |
| Accepted | Recover response/operation status without resending. |
| Verified result | Deliver if attribution is valid; separately recover or invalidate route for future requests. A changed URL is not automatically evidence of a wrong answer, nor permission for further mutation. |
| Cancelled or timed out | Preserve what is known of the effect. Release request-owned resources safely; no unapproved global stop, tab takeover, or replay. |

Maintain agent-friendly 429/backoff and safe read/reconnect resilience. PR #52 is pending and does not establish durable idempotency. External client retries, process restarts and shared account effects are distinct boundaries. Any plan to change single_send defaults needs explicit operational and compatibility review.

Use bounded model/phase-aware wait budgets (first content, idle progress, hard deadline), bounded retries/projections/resource ownership, and honest timeout outcomes. Do not shorten useful thinking time or repeatedly poll large histories without evidence. Recovery must not create a new uncontrolled queue, repeated writes or unbounded resource consumption.

## 7. Session, security and privacy

Preserve explicit continuation, fresh chat, project scope, convenient session-scoped auto-continue, opt-in parallel tabs and B1 pooled MCP isolation. Distinguish session isolation from account isolation. Retain safe singleton and stdio modes. No implicit cross-client context reuse where independence is required. Respect explicit/default project selection and document capability-specific limitations.

Keep existing risk-tier gates at discovery AND call time; protect authenticated profile/session credentials and user content. Default loopback; distinguish local API auth, transport access policy, account permissions and tool authorization. Review endpoints that expose prompts, conversation bodies or secrets even when credentials aren't exported. Diagnostics default to minimal necessary, redacted evidence, not raw payloads. Explicit operator authorization for account-affecting live tests, especially destructive tools; no autonomous deletion or foreign generation cancellation.

## 8. Outcomes and acceptance criteria

Track metrics by exact head, account entitlement/model class, route and transport. Baseline first; no invented SLO. Targets: zero wrong-turn/cross-session results, silent corruption, duplicate effects and foreign writes in tested scenarios. Improve successful valid-request rate, recovery without resend, false rejection rate, first-token and total P50/P95 latency, backend reads/turn and diagnostic classification. Separate upstream outage/throttle/model behavior from bridge-induced failure. False rejection requires evidence that a safe alternative actually existed; do not classify every timeout as preventable.

Every behavioral PR must document:
1. Changed capability IDs and guard/invariant IDs, affected endpoints/modes and migration impact.
2. Before/after expected behavior and evidence for each new or tightened guard; counterexample for false accept, false reject and safe recovery, including cancellation and mixed UI where relevant.
3. Preservation of existing functional tests or explicit rationale for changed fixtures/assertions. A stale mock is not a reason to weaken production; a new guard is not a reason to erase a valid contract test.
4. Own first-pass review and frozen findings before independent reviewer; exact-head CI and review disposition. New head invalidates prior-head claims unless genuinely unchanged scope is separately justified.
5. Targeted, operator-authorized live validation where upstream boundaries change. Include correct turn and text, no duplicate send/foreign mutation, negative cases where safe, progress/finality, and capability parity. Record head, conditions, result and limitations. Do not silently equate CI with production qualification.
6. Bounded rollback/recovery plan, release limitations and open risks. Do not label ready/merge on red CI or unresolved blockers; no forced merges.

A guard-proportionality review evaluates both safety and legitimate availability. A successful change must preserve the required capability and invariant with the simplest sufficient proof.

## 9. Non-goals and evolution

No full OpenAI API emulation, guaranteed model obedience or unlimited upstream capacity. No compulsory backend rewrite, new browser engine, full artifact service, speculative pattern adoption, or feature retirement in the name of architecture. Research may expand knowledge but not committed scope. New capabilities enter the register and roadmap after explicit scope/forcing-function decision and implementation/verification; no undocumented authority creep.

This charter proposal changes no production code, settings or release defaults. It becomes normative only by maintainer adoption; existing contracts and roadmaps remain in force until that decision.

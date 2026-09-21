# Issue #51 — No-Replay Send Mutations: Red Tests, Implementation, Verification (2026-09-21)

Branch: `fix/51-no-replay-send-mutations` (off `master` = `1711491`, which
contains `497527d`).

## Defect evidence (red tests, preserved)

- `captures/phase0/51-red-evidence-part1.txt` (local, gitignored) — the
  contract test fails against current master: no mutation primitive exists.
- `captures/phase0/51-red-evidence-part2-replay-characterization.txt`
  (local, gitignored) — the mechanical proof: through `click_send`'s real
  path (plain `_js` → `_cdp`), an ambiguous socket death sends the mutating
  `Runtime.evaluate` **twice** (frame ids [1, 2]) after one reconnect. The
  duplicate-send hazard is real and mechanically reproducible offline. The
  reproduction logic is preserved as the (now-green) contract tests in
  `tests/test_no_replay_send.py`, so the defect stays demonstrable in-repo.

Outer replay: `retry_on_rate_limit` re-invokes `factory()` fresh on
`RateLimitError` (default 3 attempts), and both the REST non-stream chat
path (`api_server.py`) and the MCP chat tools (`mcp_server.py`, two
dispatch sites) wrapped their full mutating operation in it.

## Implementation (the narrow seam)

1. `cdp_transport.CDPTransport._js_mutation(expr)` — mutating
   `Runtime.evaluate` sent with `_retry=False`: never reconnected-and-replayed.
   Reconnect-class send failures and post-dispatch response timeouts convert
   to `SendOutcomeUnknownError`; other errors propagate unchanged.
   `_js` / `_js_strict` remain replayable observation primitives.
2. `cdp_driver.CDPDriver._js_mutation` — explicit driver-level delegation
   (observation `_js` vs effect `_js_mutation` reads correctly at call sites).
3. `SendOutcomeUnknownError` (cdp_driver) — carries `code`,
   `stage`, `effect_state="unknown"`, `captured_user_id`, `retry_safe=False`,
   `reconciliation_safe=True`, so surfaces can decide without parsing text.
4. `chatgpt_dom.click_send` — the click now dispatches through
   `_js_mutation`; the readiness poll stays on `_js`.
5. Breaker boundary moved: `click_send` no longer records
   `COMPOSER_SEND_READINESS` success (JS return proves only that synthetic
   events ran — 1B: click returned +141 ms, React acceptance +437 ms).
   `send_and_stream` records success at submission evidence: captured UUID
   (primary), or the DOM acknowledgment fallback returning True.
6. `single_send` opt-in: `resilience.chat_retry_attempts(args)` (1 vs 3);
   REST resolves it from body top-level or `metadata` (`_resolve_single_send`,
   passed into `_full_response`); MCP reads it from tool arguments at both
   chat dispatch sites. Declared in the `chat_completion` and `chat_with_gpt`
   input schemas. **Scoped**: `_SINGLE_SEND_TOOLS` =
   {chat_completion, chat_with_gpt} via `_chat_tool_attempts` —
   `create_memory` (also a `_CHAT_TOOLS` member for rate-limit retry) does
   NOT inherit send semantics; if memory mutations ever need at-most-once
   behavior they get their own effect analysis.
7. UUID preservation on the ambiguous path: `send_and_stream` catches
   `SendOutcomeUnknownError` from `click_send` and, before the `finally`
   closes the capture scope and clears listener state, waits one bounded
   window (`AMBIGUOUS_CAPTURE_WINDOW_S = 1.0`; 1B measured healthy capture
   at +139 ms) for the in-flight IdentityListener capture and attaches it
   as `exc.captured_user_id`. Evidence preservation only — the outcome
   stays UNKNOWN; this is not reconciliation and never resends.
8. Surface mappings preserve the UNKNOWN distinction (pre-push review):
   REST non-stream maps `SendOutcomeUnknownError` to **HTTP 409** with
   `code=send_outcome_unknown`, `retry_safe=false`, and
   `captured_user_id` when preserved — not a bare 500. REST streaming
   (status locked at 200 once SSE starts) emits an inline
   `[Error: send_outcome_unknown …]` marker chunk with the same evidence,
   mirroring the established rate-limit marker precedent. MCP
   `_map_tool_exception` returns a structured isError result carrying the
   code, the UUID when preserved, and do-not-resend guidance. Caveat,
   stated plainly: some OpenAI SDKs retry 409/5xx by default — client
   retry is client policy; the `code` field is the stop signal a
   single_send caller must honor.

## Claim boundary (for the PR description)

Claim: **the bridge now provides an opt-in at-most-one automatic USER-send
mutation attempt across both transport reconnect ambiguity and outer
rate-limit retry; ambiguous outcomes surface explicitly rather than being
replayed.** Do NOT claim exactly-once delivery or automatic reconciliation
of ambiguous sends — UNKNOWN→CONFIRMED backend lookup remains future work.

Default-mode status, stated as legacy rather than safe: with
`single_send` absent (the default), the transport fix applies (the send
click is never replayed), but the outer wrapper retains its legacy
three-attempt budget — a `RateLimitError` surfacing after a dispatched
send may therefore re-run the whole turn, including the USER send. That
behavior is unchanged from master and is precisely the policy decision
(`single_send` opt-in vs default) that remains open after certification.

## Verification

- `tests/test_no_replay_send.py` — the four contract tests: mutation not
  replayed (1 frame, 0 reconnects, `SendOutcomeUnknownError`), read retry
  preserved (2 frames, 1 reconnect), single-shot wrapper semantics, default
  backward compatibility. 4/4 green.
- `tests/test_single_send_propagation.py` — seam tests for the helper, REST
  body/metadata resolution, MCP arguments mapping, and wrapper combinations
  (one invocation on post-mutation 429 with opt-in; default still retries).
  8/8 green.
- `tests/test_send_outcome_unknown.py` — drives the real `send_and_stream`
  orchestration with mocked collaborators: (A) an ambiguous click preserves
  the in-flight captured UUID on the error (bounded window, scope closed
  after) and records no breaker success; no-capture leaves the field None;
  (B) breaker success records exactly once on captured UUID (DOM probe
  skipped) and exactly once on the DOM-ack fallback; no success on UNKNOWN;
  (C) `single_send` honored by chat_completion/chat_with_gpt only;
  create_memory keeps the default budget even with the flag present.
  6/6 green.
- Updated to the new contract: `test_chatgpt_dom.py`
  (click records NO breaker success; poll/click split across `_js` /
  `_js_mutation`), `test_send_readiness.py`, `test_composer_selectors.py`.
- Full suite: **709 passed, 1 failed** — the failure
  (`test_parallel_tabs_pr4.py::test_config_default_parallel_tabs_false`)
  fails identically on clean master (verified via `git stash`); pre-existing,
  not addressed here.
- `ruff check src tests`: 10 errors — one BELOW the 11-error master
  baseline (an unsorted import block in a new test file was fixed). Zero
  new.

## Deliberately not done (gaps, stated)

- The `EFFECT_UNKNOWN → CONFIRMED` reconciliation upgrade (backend lookup by
  exact captured UUID, per 1B's UUID == backend node id) is designed, not
  implemented. The error now carries `captured_user_id` from the preserved
  in-flight capture, so the lookup has its key when capture succeeded.
- `type_message` still dispatches through replayable `_js` (a replayed type
  re-sets composer text; narrower blast radius than a replayed click, and
  #51's scope was the send mutation). Deliberate scope boundary.
- No live ambiguous-write certification yet — gated behind commit + PR
  review, per the agreed sequence.

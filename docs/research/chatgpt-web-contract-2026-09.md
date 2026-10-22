# ChatGPT Web Contract — Delta Report, 2026-09-20

Phase 0 of the upstream reconnaissance gate. Compares the upstream behavior
assumed at `master` (`497527d`, 2026-07-12) against live observation of
ChatGPT Web on 2026-09-20.

## Method and environment

- Evidence: passive capture only. No tab was created, closed, or navigated;
  no message was sent; no UI control was clicked. The bridge was running
  throughout (port 8080) and was left untouched.
- Environment: Chrome 153.0.8010.48 via CDP on 127.0.0.1:9222, logged-in
  session, 26 `chatgpt.com` tabs discovered (several frozen by Chrome's
  memory saver; those are excluded from DOM evidence).
- Backend reads: same-origin GETs from within a live tab, replicating the
  repo's own fetch shape (`Authorization: Bearer` token from
  `/api/auth/session`). No token values, message text, or credentials are
  retained in the capture files.
- Raw evidence: `captures/phase0/passive-20260920-224425.json`,
  `captures/phase0/probe2-projections.json`,
  `captures/phase0/probe3-dom-details.json`,
  `captures/phase0/probe4-ui-census.json`,
  `captures/phase0/probe5-model-sidebar.json` (local only; gitignored).
- User-provided screenshots (2026-09-20): composer "+" menu open; sidebar
  with the More flyout (Images/Health/GPTs); the effort chip hover tooltip
  ("Thinking effort — Ctrl+Shift+M"); the effort menu open (slider, "High >",
  a locked position beyond); and the model list showing "GPT-5.6 Sol ✓" and
  "GPT-5.5 — Leaving on October 14". Used as evidence for open-menu
  contents, which cannot be captured passively (verified: no menu markup
  exists in the closed state — probe6).
- Probe tool: `scripts/phase0_passive_recon.py` (re-runnable).

## Contract matrix

| Contract | July assumption (master) | Current observation (2026-09-20) | Class | Impact |
| --- | --- | --- | --- | --- |
| Composer | `div[role="textbox"]#prompt-textarea` / `.ProseMirror` (`chatgpt_dom.py:76`) | Both hit on every live tab; `div#prompt-textarea.ProseMirror[contenteditable][role=textbox]`; textarea fallback absent | UNCHANGED | type/verify |
| Send control | `button[aria-label*="Send" i]` + testid fallback + `form:has(#prompt-textarea) button[type=submit]` broad (`chatgpt_dom.py:85-94`) | With empty composer, no send button exists in DOM at all — consistent with July comments ("no data-testid=send-button; affordances are composer-plus-btn and dictation"). `composer-plus-btn` observed. Send-time resolution not yet re-observed | UNCERTAIN (provisionally unchanged) | send safety |
| Attachments entry | UI upload workflow (PR #49 open) | `composer-plus-btn` opens a merged tools/connectors/files menu (user screenshot; menu closed at capture time): Add photos & files (Upload from computer), Add from library, Create image, Web search, Deep research, Sketch, plus connectors GitHub, OpenAI Platform, ClickUp; footer "Type to search plugins, files, folders & skills" | CHANGED (menu scope) | PR #49 = first entry; more UI states to avoid |
| Message containers | `[data-message-author-role=user|assistant]` (`cdp_driver.py:1500-1509`) | Present; counts readable; `[data-testid^="conversation-turn"]` matches mounted turns | UNCHANGED | counting, TurnAnchor DOM guard |
| Message attributes | `data-message-id` on message nodes | Present, **plus new**: `data-message-model-slug` (observed `gpt-5-6-thinking` on a tab whose effort selector reads "High" — consistent with the runtime slug encoding reasoning mode; confirmation pending a controlled send), `data-turn-start-message` | NEW | model identity — new stable signal |
| Generating state | `.result-thinking` / `.result-streaming` / `.markdown` lifecycle (docs/reverse-engineering-notes.md) | `.markdown` present on settled messages; thinking/streaming classes require an active generation — not observed passively | PENDING-CONTROLLED | completion detector |
| Fresh-chat route | `/` | `https://chatgpt.com/` tab live, composer present | UNCHANGED (form only; navigation behavior untested) | navigation |
| Existing chat | `/c/{id}` | Live tabs use it; id extraction (`/c/` split, `backend_client.py:233-235`) still parses | UNCHANGED (form only) | navigation |
| Project chat | `/g/g-p-…/c/{id}` | Live tabs use it, incl. `/g/…/project?tab=sources` | UNCHANGED (form only) | navigation |
| Auth session | `/api/auth/session` → keys incl. `accessToken` | Status 200, same key set. **Cookies-only fetches to `/backend-api/conversation/{id}` return 404 (masked); Bearer required** — repo already always sends it | UNCHANGED (bearer requirement confirmed) | token refresh |
| Conversation projection | GET `/backend-api/conversation/{id}` → `mapping`, `current_node`, rich metadata (`backend_projection.py`) | 200 with Bearer on plain + project conversations; `current_node` present; top-level keys incl. `gizmo_id`, `default_model_slug`, `conversation_template_id`, `atlas_mode_enabled` | UNCHANGED (structurally) | TurnAnchor primary path |
| Long-chat DOM | All historical turns mounted; `querySelectorAll` counts reflect conversation length | **Sectioned loading confirmed**: Web2API project conversation `6a8149d3` has 314 backend mapping nodes (5 user, 129 assistant, 179 tool) but only 5 turns mounted (2 user + 3 assistant) in an idle, loaded tab. Plain conversation `6aaa4c07`: 142 backend nodes vs 1+1 mounted in run-1 sample | **CHANGED / highest priority** | count baselines, stale-return race |
| Models | `MODEL_MAP` (`api_server.py:36`): gpt-5.5…gpt-5 + legacy aliases | Account `/backend-api/models` returns 6: `auto`, `gpt-5-3-mini`, `gpt-5-5-mini`, `gpt-5-5`, `gpt-5-6-mini` ("GPT-5.6 Luna"), `gpt-5-6` ("GPT-5.6 Luna"). `gpt-5-2/5-1/5` absent from list. Runtime slug `gpt-5-6-thinking` observed in DOM but not in models list. Picker (user screenshot) titles the active model **"GPT-5.6 Sol"** while /models titles `gpt-5-6` "Luna" — naming split unresolved, do not hardcode either. **"GPT-5.5 — Leaving on October 14"**: `gpt-5-5` family has a dated deprecation (2026-10-14) | CHANGED | model selection; `gpt-5.5*` aliases expire 2026-10-14 |
| Model picker | `#model-selector-btn` / `button[aria-label*="Model"]` / `[data-testid*="model"]` (`cdp_driver.py:1122`) | **RESTRUCTURED (screenshots + probe6)**: composer chip = `button.__composer-pill[aria-haspopup="menu"]`, text shows current effort ("High"); hover tooltip "Thinking effort — Ctrl+Shift+M". Open, it becomes a Radix menu: effort **slider** ("High >") with a **locked position beyond** (plan-gated tier, unidentified), and a model list nested behind the effort level: "GPT-5.6 Sol ✓" selected, "GPT-5.5 — Leaving on October 14". Separate per-turn `button[aria-label="Switch model"]` hover buttons also exist on conversation tabs. July's selector matches none of the composer chip | CHANGED | `select_model` finds no legacy picker (non-fatal False on all surfaces today); model+effort selection is nested behind one chip |
| Model identity | one-dimensional slug (`MODEL_MAP` → picker) | **Two-dimensional**: (model slug, reasoning effort). Live pairing: effort "High" + account model gpt-5-6 ↔ runtime slug `gpt-5-6-thinking` in `data-message-model-slug`. Effort slider positions not yet enumerated (open-menu capture pending) | CHANGED | selection, per-turn model verification |
| Large paste | composer text at any length | Threshold behavior (≥~5,000 chars → attachment per third-party reporting) requires an active paste test | PENDING-CONTROLLED | type_message, send-ack, TurnAnchor fallback |
| Custom GPTs | active capability (`list_gpts`, `chat_with_gpt`) | Retirement claim found only in unofficial, mutually contradictory sources; no official confirmation located. Sidebar census (live DOM + user screenshot): top-level items are New chat, Library, Scheduled, **Plugins**, **Sites (NEW badge)**, More flyout → Images / Health / **GPTs**. GPTs and Plugins coexist today; GPTs reachable only via the More flyout | UNVERIFIED RUMOR (retirement); surface: GPTs demoted to flyout | MCP roadmap only |
| User-message identity | outgoing conversation request carries client message id (IdentityListener) | Requires a send to observe | PENDING-CONTROLLED | TurnAnchor primary |

## Key findings

1. **Sectioned loading is real and changes the DOM-count contract.** The
   backend graph is the only complete representation of a conversation; the
   DOM now holds only the tail. The delta-based send baseline
   (`cdp_driver.py:1484`) still functions if mounting is stable when the
   baseline is read, but (a) any code treating DOM counts as conversation
   length is now wrong, and (b) the baseline read can race progressive
   mounting right after navigation — the retry/fail-closed logic guards JS
   failure, not a cleanly-read-but-still-rising count. The 50/100/200-turn
   fixture test exists to size this.
2. **A new stable model-identity signal exists: `data-message-model-slug`
   on assistant message containers.** Observed `gpt-5-6-thinking`. This is
   exactly the "better semantic signal" the audit was looking for — it can
   verify model selection per-turn without trusting the picker UI, and it
   reveals runtime slugs (`gpt-5-6-thinking`) absent from `/backend-api/models`.
   The live evidence pairs `gpt-5-6-thinking` with an effort selector
   reading "High", suggesting that the runtime slug may encode reasoning
   mode; a controlled model/effort send matrix is required before defining
   the mapping.
3. **Model selection is now two-dimensional, effort-first, and nested.**
   One composer chip ("Thinking effort", Ctrl+Shift+M) opens a slider with
   the model list attached behind the current effort level. A correction to
   an earlier pass of this report: the passive probes concluded "the picker
   is gone" because no picker markup exists in the closed DOM — the user's
   screenshots show it does exist behind interaction. This is exactly the
   open-menu blind spot flagged in the method notes. Consequences for the
   bridge: `select_model` finds no legacy picker on any surface (non-fatal
   False — best-effort design tolerates it), model choice rides on the
   effort chip, and per-turn "Switch model" buttons exist separately on
   conversations. Any future model control should target the
   `__composer-pill` chip + `data-message-model-slug` verification rather
   than the legacy picker selectors.
4. **Dated deprecation: the gpt-5-5 family leaves on 2026-10-14.** The
   picker advertises "GPT-5.5 — Leaving on October 14" while `MODEL_MAP`
   still exposes `gpt-5.5` / `gpt-5.5-thinking` and `/models` still lists
   `gpt-5-5` / `gpt-5-5-mini`. Requests mapped to gpt-5-5 after that date
   will fall to best-effort default. Alias migration (gpt-5.5 → gpt-5-6 or
   removal with a clear error) belongs on the roadmap with the date
   attached.
5. **The models surface moved past `MODEL_MAP`.** This account serves
   GPT-5.6 and 5.6-mini; `MODEL_MAP` has no 5.6 entries, and its
   `gpt-5-2/5-1/5` targets no longer appear in the account's list.
   Both 5.6 names now appear on this same account — the picker shows
   "GPT-5.6 Sol" as active while `/models` titles `gpt-5-6` "Luna" —
   so neither name can be treated as the universal one.
6. **Projection and auth contracts held.** The TurnAnchor primary path's
   backend shape is unchanged; bearer-authenticated fetches behave as in
   July. Unauthenticated (cookie-only) fetches 404 rather than 401 —
   relevant only if auth-expiry detection ever relies on status codes from
   unauthenticated calls (the repo does not).
7. **Composer and message-container selectors held.** No reactive patching
   needed there today. Sidebar/branding surfaces moved instead: Plugins and
   Sites are first-class nav items, GPTs lives under the More flyout, and
   the composer "+" menu now merges files, tools, and connectors.

## Pending controlled-capture checklist (next session)

Blocking for issue #51 design:

1. Send boundary with content in the composer: does
   `button[aria-label*="Send" i]` resolve; does the `form:has()` fallback;
   Send→Stop transition during generation; click-then-CDP-interrupt
   behavior.
2. Generation-state class cycle on a live response (`.result-thinking`,
   `.result-streaming`, `.markdown` fill, copy-button appearance).
3. Paste threshold: >5,000-char paste — attachment conversion, composer
   text state, send-ack implications, TurnAnchor fallback text-match
   implications.
4. Model selection (revised scope): the model list and slider shape are now
   screenshot-known; remaining — enumerate the effort slider positions and
   the locked tier, capture the open menu's DOM structure (Radix menu off
   the `__composer-pill` chip), enumerate a per-turn "Switch model" menu,
   and confirm the effort→runtime-slug mapping (`gpt-5-6` + High →
   `gpt-5-6-thinking`?) with a controlled send reading
   `data-message-model-slug`.

Long-chat and navigation suites:

5. 50/100/200-turn fixtures in a disposable project: baseline-race test
   (read count immediately after navigation, watch it rise), tail-stability
   test, projection-vs-DOM at each size.
6. Navigation recertification: `/`, `/c/{id}`, project routes, back/forward,
   SPA normalization (PR #50's requirement).

Attachments (blocks PR #49 rehabilitation only): file select, upload states,
removal, multi-file, failures.

## Explicitly not tested in this pass

- Nothing requiring interaction: no send, no click, no paste, no picker
  open, no navigation. All send-boundary, generation-state, paste, and
  picker rows above are pending, not negative, findings.
- No long-conversation fixture was created; the sectioned-loading numbers
  come from existing conversations (314-node and 142-node samples).
- Frozen tabs (~several of 26) were excluded; their DOM state is unknown.
- Single account, single plan, one capture session — rollout variance
  unaddressed. Re-run `scripts/phase0_passive_recon.py` before each phase
  that depends on this contract.

## Baseline freeze and execution order (post-freeze addendum, 2026-09-21)

The passive baseline above is frozen as the Phase 0 passive snapshot; this
addendum records decisions made after the freeze and does not modify the
body. Classification: passive reconnaissance COMPLETE; current contract
snapshot FROZEN; controlled reconnaissance NOT COMPLETE.

Provisional invariant promoted from the evidence (binding until the
controlled model/effort matrix tests it):

> Configured/catalog model identity (`/backend-api/models`, `MODEL_MAP`)
> ≠ selected UI identity (picker label)
> ≠ observed turn execution identity (`data-message-model-slug`).

The pending controlled-capture queue is reordered: the confirmed
sectioned-loading delta gates the send-acknowledgment evidence model, so
the baseline-race experiment runs first.

1. Long-chat baseline-race experiment (50/100/200 turns; after navigation,
   sample URL, readyState, composer presence, user/assistant counts, and
   mounted turn IDs at 100–250 ms until stable; determine whether
   composer-ready precedes mounted-turn-set-stable).
2. Send boundary / issue #51 mutation experiment, with four independent
   evidence questions: did Runtime.evaluate reach Chrome; did the JS event
   sequence execute; did React accept the submission; did the backend
   create the intended user node. Outcome-unknown states reconcile only,
   never resend.
3. Generation-state lifecycle (thinking/streaming/markdown/copy-button).
4. Model × effort send matrix (records picker label, effort state, /models
   entry, outgoing request fields, DOM slug, backend metadata per turn;
   defines ModelSelection(base_model, reasoning_effort) only after
   evidence).
5. Large-paste conversion threshold.
6. Navigation recertification.
7. Attachment lifecycle.

Design direction recorded for #51 (not implemented): evolve send
acknowledgment from DOM-count delta to turn-identity evidence — outgoing
request UUID observed, matching user message ID, backend graph contains the
submitted user node — with the DOM-count delta retained only as a secondary
hint. Sectioned loading makes DOM population unsuitable as
conversation-history authority, so waiting for DOM-count stability is
explicitly rejected as the fix.

Experiment 1A executed 2026-09-21 under this authorization: the mounting
race was NOT observed (0/5 cold-load runs — atomic hydration commit brings
composer + mounted window up together; ~6 s readyState-to-hydration dead
zone documented). Results and scope:
`experiment-1a-mounting-race.md` (same directory). Experiment 1B (one
normal controlled send, disposable project) is next authorized and had not
been executed at the time of this addendum.

Experiment 1B executed 2026-09-21 (after the paragraph above was written):
outcome class A — outgoing-UUID capture (+139 ms) precedes every DOM
acknowledgment signal (+437 ms), and the captured UUID equals the backend
node id exactly. The send path is now POST `/backend-api/f/conversation`
with prepare + sentinel wrapping; backend clock skew measured at ≈ −40 ms
(June's +5.1 s constant stale); `.result-thinking`/`.result-streaming`
absent during a thinking-model generation. Details:
`experiment-1b-send-timeline.md`. Matrix-row deltas recorded here, frozen
body unchanged: Generating state → PENDING-CONTROLLED; negative evidence:
`.result-thinking` / `.result-streaming` absent in 1/1 controlled
`gpt-5-6-thinking` run. Send-path endpoints → CHANGED (`f/conversation`).
User-message identity → CONFIRMED for this path by 1B: outgoing
client-message ID = backend mapping node ID = backend message ID; UUID
capture preceded DOM acknowledgment by ~300 ms. Next in queue: #51 deterministic
transport-level replay reproduction (offline), then the generation
lifecycle capture.

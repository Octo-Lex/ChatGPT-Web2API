# Experiment 1A — Long-Chat Mounting Race (2026-09-21)

Controlled read-only experiment, executed under the authorization recorded
in the Phase 0 report addendum. Question: does the composer become ready
before the mounted DOM of a sectioned long conversation is stable — the
ordering the bridge's pre-send DOM-count baseline implicitly assumes?

## Verdict

**The race was not observed: 0 of 5 runs.** In every run the mounted DOM was
completely empty through document-complete, then the composer and the entire
mounted conversation window appeared together in a single atomic commit. The
claim is scoped exactly as the acceptance criterion requires: no
post-readiness mounting change was observed in *this* environment,
conversation, Chrome build, account, and capture runs. This falsifies
nothing globally; it fails to confirm the hazard on the path tested.

## Method

- One positively owned tab, created via `PUT /json/new?about:blank` and
  closed afterward. No existing tab was touched; no content was mutated.
- `Page.navigate` cold load — the same mechanism the bridge uses
  (`cdp_driver.py:1301`) — to the natural 314-node specimen conversation
  (`6a8149d3`, web2api project; read-only).
- Sampling every ~150 ms from navigation: URL path, `readyState`, composer
  presence, user/assistant counts, mounted `conversation-turn` ids, mounted
  `data-message-id`s. After 8 s of unchanged count+identity, a 10 s extended
  watch at 250 ms looked for late flaps and identity churn at constant
  count. Five cold-load runs.
- Harness: `scripts/phase0_experiment_1a.py`; raw data:
  `captures/phase0/exp1a-20260921-092848.json`.

## Results

All five runs showed the same shape (times from navigate):

| Milestone | Range across runs |
| --- | --- |
| `readyState` → loading | ~0.97–1.08 s |
| `readyState` → interactive | ~2.55–2.66 s |
| `readyState` → complete (DOM still empty: 0 composer, 0 messages) | ~2.84–2.94 s |
| Atomic appearance: composer + full window (3 user + 3 assistant, 6 message ids) | ~8.91–9.55 s |
| Post-appearance identity churn (10 s extended watch) | 0 events in 5/5 runs |
| Intermediate count values before the final window | none in 5/5 runs |
| Sample errors | 0 |

Run-to-run timing was tight (composer within a 0.64 s band), and the
ordering was deterministic: composer-ready and mounted-identity-stable
coincide, because they are the same React commit.

## Findings

1. **Atomic mount, not progressive mounting, on this path.** The hydration
   commit brings the composer and the conversation tail window up together.
   Under this model, "composer ready" implies "DOM baseline readable" —
   the bridge's existing sequencing (composer readiness poll, then
   `_read_assistant_count_baseline`) lands after the commit in practice.
2. **Sectioned loading re-confirmed from a second angle.** The stable
   mounted window is 6 turns (3+3) against 314 backend mapping nodes. The
   DOM is a tail window, and it is small even when loading is fully
   complete.
3. **A ~6 s hydration dead zone exists after `readyState=complete`.**
   Between ~2.9 s and ~9 s the document is complete but React has rendered
   nothing. Any logic gating on `readyState` alone would act ~6 s before
   content exists. The bridge is not exposed today because its readiness
   polls the composer, but this explains the historical
   `send_baseline_unavailable` failure class if a baseline read is ever
   reached from a readyState-only gate.
4. **The mounted window is not a fixed size across sessions.** The 2026-09-20
   passive snapshot saw 5 mounted turns for this conversation; today's runs
   saw 6, with no intervening sends. Window size varies; only the backend
   graph is stable. (Consistent with the provisional invariant that DOM
   counts are diagnostics, not authority.)

## Scope and exclusions

- One account, one Chrome build (153), one network condition, one
  conversation, five runs, cold-load path only.
- Not covered: memory-saver/discarded-tab revival, heavy CPU or network
  stress during hydration, conversations whose mounted window is larger
  than 6 turns (progressive rendering may appear with bigger windows), and
  any non-`Page.navigate` navigation path. The bridge's
  `navigate_conversation` uses `Page.navigate`, so the tested path is the
  bridge's real path shape — but revival and stress remain open.
- Per the authorization: this experiment made no writes. Experiment 1B
  (one normal controlled send in a disposable project) is the next
  authorized step and had not been executed at the time of writing.

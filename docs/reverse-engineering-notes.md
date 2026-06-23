# ChatGPT Reverse-Engineering Notes

Findings captured from live DOM/API probing during the Phase-2 and
composer-redesign work (Jun 2026). These describe ChatGPT's internal
structure as observed at that time; ChatGPT changes frequently, so treat
these as a starting map, not a contract. The `doctor` command
(`W2A_DIAGNOSE=1`) is the supported way to re-derive this kind of
evidence going forward — this document captures the *knowledge* the
one-off probes produced, not the probes themselves.

## 1. Thinking-model DOM lifecycle (probe_thinking, observe_generation)

A reasoning-model turn moves through distinct DOM states. Polled at
0.5s granularity from send-click to settle:

| Signal | During reasoning | During answer stream | After settle |
|---|---|---|---|
| `.result-thinking` on msg | **present** | present (lingers) | present (collapsed "Thought process") |
| `.result-streaming` cursor | absent | **present** | absent |
| `.markdown` textContent | empty | **empty** (lags) | **fills** at settle |
| `innerText` | "Thinking..." / reasoning text | answer text + label | full answer + "Thought for N seconds" |
| action button (`[data-testid*="copy"]` etc.) | absent | absent | **appears** |
| `child_count` / `html_len` | grows | grows | stable |

**Two traps these findings exposed (both now fixed in `cdp_driver.py`):**

1. **`is_thinking` pinned forever.** The naive checks
   `!!last.querySelector('.result-thinking') || /thinking/i.test(innerText)`
   stay true after completion: `.result-thinking` lingers as a collapsed
   section, and `/thinking/i` matches the persistent "Thought for N
   seconds" summary label. The fix: `is_thinking = !has_action &&
   !!last.querySelector('.result-thinking')` — the action button marks a
   finished turn.
2. **Empty `.markdown` during streaming.** `.markdown` textContent is
   empty until the turn settles; reading it as the stream source produces
   empty responses. The fix: stream from `innerText` (populated during
   streaming, with the reasoning label stripped), preferring `.markdown`
   only when non-empty.

## 2. Completion detection: the action button is the robust signal

Earlier heuristics each failed:
- `!stopBtn && hasContent` — Stop button absent at first poll but
  `html_len > 50` (wrapper) → false "done" immediately.
- `generation_started && !is_generating` — Stop button **flickers off**
  between token batches → premature truncation.
- text stability alone — `.markdown` empty during streaming → never
  "stable" → stall fires.

The robust signal: the per-turn **action row** (`data-testid` containing
`copy`, `turn-action`, or `response-turn`) renders only on a completed
message. It is immune to Stop-button flicker and the empty-`.markdown`
quirk. This is what `send_and_stream` Phase-2 now keys on.

## 3. Conversation API structure (time_api, dump_mapping, compare_endpoints)

`GET /backend-api/conversation/{id}` returns a tree under `mapping`,
keyed by node id. Each node carries a `message` with:

- `author.role`: `"user"` / `"assistant"` / `"system"`
- `content.content_type`: **`"text"`** (the answer), `"reasoning_recap"`
  (the reasoning summary — **empty text**), `"code"`, `"tether_browsing"`
- `content.parts`: array; `parts[0]` is the text (string) or a structured
  object (for non-text content types)
- `status`: `"in_progress"` → `"finished_successfully"`
- `end_turn`: boolean — **the reliable completion marker**
- `create_time`: float, monotonic within a conversation

**Key gotcha:** the newest-by-`create_time` assistant node is often a
`reasoning_recap` with **empty text**. The real answer is a sibling node
with `content_type: "text"`. Code that grabs "newest assistant" blindly
gets empty text. `_fetch_text` filters for content.

**offset/limit params** on the conversation endpoint do **not** change
which message versions are returned — same mapping either way.

## 4. Composer redesign (post-2026)

ChatGPT replaced the `<textarea id="prompt-textarea">` with a
contenteditable **ProseMirror** div. The old `#prompt-textarea` survives
as a **hidden fallback overlay** (class `wcDTda_fallbackTextarea`) that
only works with JS off — typing into it does not reach the composer.

| Element | New selector | Legacy fallback |
|---|---|---|
| Input | `div[role="textbox"]#prompt-textarea, div[role="textbox"].ProseMirror` | `textarea#prompt-textarea` |
| Send | `button[aria-label*="Send" i]:not([data-testid="stop-button"])` | `button[data-testid="send-button"]` |

Typing into the ProseMirror div requires `document.execCommand('insertText', …)`
(or an InputEvent dispatch); setting `.value` does nothing (it's not a
form control).

## 5. Why the bridge drives the DOM, not the API

An earlier experiment (removed `scripts/curl_cffi_test.py`) tried sending
messages by POSTing directly to `/backend-api/f/conversation` with the
sentinel prepare/finalize tokens. Even with a fully-correct body, headers,
PoW, and a Chrome-impersonated TLS fingerprint (via `curl_cffi`), the
direct send **403'd with "Unusual activity"**. The sentinel flow
(prepare → finalize → token) succeeds via plain urllib, but the message
send itself is gated behind TLS/behavioral fingerprinting that the
in-page `fetch()` (the bridge's approach) passes naturally because it
runs in the real browser context. This is *why* the driver types into the
composer and polls the DOM rather than calling the conversation API
directly for sends — the reads (GET) work over plain HTTP, but the
write path requires the browser's full fingerprint.

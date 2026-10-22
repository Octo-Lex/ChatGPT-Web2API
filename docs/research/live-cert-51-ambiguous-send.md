# Live Ambiguous-Write Certification — PR #52 / Issue #51 (2026-09-21)

One instrumented injection, executed under the authorized protocol after
delta re-review passed. This is the executed-but-response-lost branch,
live, against the real ChatGPT Web account.

## Protocol

- Live bridge (8080) untouched throughout; experiment bridge ran the PR
  #52 tree (`162c80c` = `346411c` + comment-only fix) in-process on 8081.
- Chrome 9222 shared, strict owned-tab isolation (the experiment tab was
  the driver's own owned tab, closed on shutdown: log-verified).
- Disposable project `Web2API Phase0 Controlled Recon`; non-stream REST;
  `single_send=true`; sentinel `PHASE0-LC51-3ac2ae77`.
- Fault injection per protocol — the socket was NOT killed: the
  experiment instance's websocket `send()` was wrapped so the ONE
  send-click `Runtime.evaluate` frame was **forwarded to real Chrome**
  (the mutation executed remotely) and the local caller then raised a
  reconnect-class `ConnectionClosedError`. `recv()` always delegated
  normally, so the IdentityListener kept working on the live socket.
- Harness: `scripts/phase0_live_cert_51.py`; raw counters:
  `captures/phase0/live-cert-51-20260921-181705.json` (+ post-hoc
  backend confirmation merged); run log:
  `captures/phase0/live-cert-51-run3.log`.

## Result — every counter at the strongest-success target

| Counter | Target | Observed |
| --- | --- | --- |
| mutating `Runtime.evaluate` frames | 1 | **1** |
| driver reconnect calls | 0 | **0** (log-verified: no reconnect line; the 4 total `websockets.connect` calls were the driver's page socket, browser-level startup sockets, and the harness observer) |
| `/backend-api/f/conversation` POSTs | 1 | **1** |
| REST result | 409 `send_outcome_unknown` | **409 `send_outcome_unknown`** (8.5 s) |
| `x-should-retry` header | false | **false** |
| captured UUID | UUID X | **`8f471fb2-e658-4313-b8c7-26ee966a19f5`** (listener capture at +~200 ms after the fault; preserved onto the error before scope close; identical in the 409 body) |
| backend USER nodes containing sentinel | 1 | **1** (conversation "Live ambiguity certification", generation ran to completion ~14 s after the 409 — the remote effect was real) |
| backend node/message id vs `captured_user_id` | equal | **equal — both are `8f471fb2-e658-4313-b8c7-26ee966a19f5`** |
| automatic resend | 0 | **0** |

## What this certifies

The exact scenario #51 exists for, live:

```text
remote effect occurred (turn created, generation completed)
        +
local command outcome was ambiguous (fault injected after delivery)
        ↓
UNKNOWN surfaced (409, x-should-retry:false, retry_safe=false)
        ↓
evidence preserved (captured_user_id == backend node id == message id)
        ↓
NO replay (one mutation frame, zero reconnects, one submission POST)
```

Per the authorizing review: this is the strongest possible live
certification short of implementing automatic UNKNOWN→CONFIRMED
reconciliation — the backend lookup key on the error is proven exact
against the live backend.

## Scope and notes

- One run, one sentinel, single account, shared Chrome (recorded
  limitation). Repetition belongs to later compatibility certification.
- Two launcher attempts were discarded before the successful one (a
  buffered-output run killed before any request; a run blocked on
  `service.start()`'s shutdown wait — launcher bugs, not bridge bugs).
  The killed first attempt's registered owned tab was reclaimed by the
  successful run and closed with it; no user tab was touched at any point.
- `backend_node_ids: []` in the frozen JSON's original counters is
  superseded by the merged `posthoc_backend_confirmation` block (the
  in-run lookup could not resolve the conversation id; the post-hoc
  lookup did).

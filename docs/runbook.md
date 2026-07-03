# Production Runbook

> **Scope:** operating a running `chatgpt-web2api` deployment — interpreting
> health, diagnosing failure modes, understanding breaker states, recovering
> auth, collecting logs, and restarting safely. For installation see
> [deployment.md](deployment.md); for OS-supervisor wiring see
> [os-supervision.md](os-supervision.md).

All facts below are verified against the source (cited as `file:line` in the
appendix). Thresholds and field names are exact, not paraphrased — an incident
is the worst time to discover a guessed state name.

---

## 1. Startup checklist

Before declaring a deployment live, confirm each row:

| # | Check | Command / where | Expected |
|---|-------|-----------------|----------|
| 1 | Chrome is reachable on the CDP port | `curl -sf http://127.0.0.1:9222/json/version` | 200 + JSON (browser version) |
| 2 | REST `/health` returns `healthy` or `starting` | `curl -s http://127.0.0.1:8080/health` | `status` ∈ {`healthy`,`starting`}, `chrome_running=true`, `driver_connected=true` |
| 3 | No breakers open | `/health` → `open_breakers` | `[]` |
| 4 | `last_error` is null or explained | `/health` → `last_error` | `null` in steady state |
| 5 | MCP/SSE handshake succeeds (if MCP used) | `ensure` exits 0; or an MCP client `initialize` + `list_tools` | tools returned, no error |
| 6 | One real chat send succeeds | POST `/v1/chat/completions` non-stream `"Reply with exactly: ok"` | HTTP 200, content `ok`, `finish_reason=stop` |

Row 6 is the only end-to-end signal — the rest are necessary preconditions.
A deployment that passes 1–5 but fails 6 is "up but not serving" and must be
treated as down. See [§3 failure modes](#3-common-failure-modes).

### The reconcile command

```bash
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222
```

`ensure` is **point-in-time**: it reconciles REST + MCP/SSE to a healthy state
and exits. It is not a daemon. Exit codes:

| Code | Meaning | Action |
|------|---------|--------|
| `0` | REST + SSE ready | nothing |
| `1` | reconcile failure (REST or SSE could not be made ready) | inspect logs, re-run after fix |
| `2` | **auth/login needed** (`auth_required` breaker open) | human login required (see [§6](#6-auth-recovery-flow)) |

> ⚠️ `python -m chatgpt_web2api.ensure` does **not** work — the `ensure`
> submodule has no `__main__` block. Use the console-script subcommand only.

`ensure` launches REST and MCP/SSE as **detached child processes** (POSIX
`start_new_session=True`; Windows `DETACHED_PROCESS`) so they outlive the
`ensure` invocation. There is no continuous in-process supervisor — later
failures require re-running `ensure` or an OS supervisor (see
[os-supervision.md](os-supervision.md)).

### Startup order & Chrome ownership

- **REST owns Chrome.** REST is launched first; it manages the Chrome process.
- **MCP/SSE attaches.** MCP/SSE is a separate process that connects to the
  already-running Chrome over the shared CDP port — it never launches Chrome.
- `ensure` reconciles REST first; SSE is never reconciled until REST is ready,
  and if REST needs auth (exit 2) SSE is skipped entirely.

---

## 2. Health interpretation

`GET http://localhost:8080/health` (also served at `/`). No auth required.

### Top-level fields

| Field | Type | Meaning |
|-------|------|---------|
| `status` | string | Derived state — see states table |
| `chrome_running` | bool | Live probe to `http://127.0.0.1:<cdp_port>/json/version` returned 200 |
| `cdp_connected` | bool | Alias of `driver_connected` |
| `driver_connected` | bool | CDP driver is connected |
| `requests_served` | int | **Accepted** chat requests since REST start. Incremented at the start of request handling (after auth, before JSON parsing) — a malformed/failed request still counts. This is **not** a count of successful sends; for that see `last_successful_send_at`. |
| `started_at` | float | REST process start time (epoch seconds; compute uptime as `now - started_at`) |
| `last_successful_send_at` | float \| null | Epoch of last successful chat send; `null` until first success |
| `last_error` | string \| null | Latching last error `"<ExcType>: <msg>"` |
| `open_breakers` | list[string] | `.value` strings of currently-open breakers; `[]` = none open |
| `breakers` | object | Full per-kind snapshot — see [§5](#5-breaker-states-and-cooldowns) |

> There is **no `uptime` field**. Compute it from `started_at`.

### The `status` field — exact conditions

Status is computed sequentially; earlier branches win. An open breaker can
only **downgrade** `starting`/`healthy` → `degraded` — it can never upgrade to
`broken` and never overrides an existing `broken` (Chrome-down is the harder
failure, and `broken` invites a destructive supervisor restart).

| `status` | Exact condition | Meaning |
|----------|-----------------|---------|
| `broken` | `chrome_running == false` | Chrome is down (probe to `/json/version` failed). REST will be restarted by `ensure`. |
| `degraded` | Chrome up **but** `driver_connected == false`, **OR** a breaker is open overlaying `starting`/`healthy` | Up but refusing some/all traffic. Do **not** restart-loop — see [§7](#7-safe-restart-sequence). |
| `starting` | Chrome up, driver connected, but zero chat requests accepted (`request_count == 0`) and no send recorded (`last_successful_send_at is null`) | Cold bootstrap. Give it room; do not restart. |
| `healthy` | Chrome up, driver connected, and **not** (`last_successful_send_at is null` AND `request_count == 0`) | REST/Chrome/driver are up and the server has seen traffic or a send has succeeded. **Caveat:** does not guarantee a send succeeded — `request_count` increments before validation, so one malformed request flips `starting`→`healthy`. True read-readiness is `last_successful_send_at != null` or a passing post-deploy send ([§8](#8-post-deploy--post-restart-validation)). |

### Triage matrix

| `status` | `chrome_running` | `driver_connected` | breaker open? | `ensure` action |
|----------|------------------|--------------------|---------------|-----------------|
| `broken` | false | any | n/a | immediate REST restart |
| `degraded` (driver dead) | true | false | — | poll up to budget, then restart (unless auth) |
| `degraded` (breaker trip) | true | true | yes | wait cooldown+grace (timed) / exit 2 (auth) |
| `starting` | true | true | no | wait up to 30s for connectivity |
| `healthy` | true | true | no | none |

---

## 3. Common failure modes

Ordered roughly by frequency in practice.

### 3.1 `auth_required` breaker — login expired (exit code 2)
- **Symptom:** `/health` `open_breakers` contains `"auth_required"`; `ensure`
  exits **2**; chat requests return HTTP 503 `circuit_open`.
- **Cause:** ChatGPT session token expired or was invalidated. The REST
  preflight attempts a token refresh first (`recover_auth`); only if that fails
  does the breaker stay open.
- **Fix:** human login — see [§6](#6-auth-recovery-flow). This breaker is
  **indefinite** (never auto-recovers); only a successful token refresh closes
  it. `ensure` will **not** restart REST for this — a restart can't fix a
  login problem.

### 3.2 `chrome_crash_loop` breaker — Chrome won't stay up
- **Symptom:** `open_breakers` contains `"chrome_crash_loop"`; Chrome restarts
  repeatedly.
- **Cause:** 3 Chrome restarts within a 300s window. Usually a resource issue
  (out of memory, display/Xvfb missing on a headless server), a Chrome profile
  lock, or an OS update breaking the Chrome binary.
- **Fix:** check Chrome can launch manually; check display/headless config;
  clear the Chrome profile lock if stale. Cooldown is 300s — it half-opens
  after that, and `ensure` restarts REST if still open past `cooldown + grace`.

### 3.3 `cdp_reconnect` breaker — driver lost Chrome
- **Symptom:** `open_breakers` contains `"cdp_reconnect"`; transient.
- **Cause:** 5 reconnect failures in 120s. Often a transient CDP socket drop
  (Chrome update, tab crash, system suspend/resume). Usually self-heals.
- **Fix:** usually none — cooldown 120s, then a successful reconnect closes it.
  If it persists, Chrome may be wedged; a `broken` status will follow and
  `ensure` will restart REST.

### 3.4 `composer_send_readiness` breaker — can't type/send
- **Symptom:** `open_breakers` contains `"composer_send_readiness"`; sends fail
  preflight.
- **Cause:** 3 send-readiness failures in 120s. Typically a ChatGPT DOM change
  (selector drift) or a transient page state where the composer isn't ready.
- **Fix:** cooldown 300s; a confirmed successful send closes it. Persistent
  trips on a stable ChatGPT suggest selector drift — check release notes / DOM.

### 3.5 `status=degraded` with empty `open_breakers`
- **Symptom:** Chrome up, driver disconnected, no breaker tripped.
- **Cause:** CDP driver disconnected but not yet tripped (below threshold), or
  recovering.
- **Fix:** let `ensure`'s degraded policy run (poll up to ~20s, then restart).
  **Do not** race it with a faster supervisor restart — see [§7](#7-safe-restart-sequence).

### 3.6 `status=broken`
- **Symptom:** Chrome not reachable on the CDP port.
- **Cause:** Chrome crashed, was killed, or the port probe is blocked.
- **Fix:** `ensure` restarts REST (which relaunches Chrome) immediately. If it
  loops, you'll land in `chrome_crash_loop` (3.2).

### 3.7 Requests return 503 `circuit_open` but `/health` looks fine
- **Symptom:** chat 503s with `code: circuit_open`, but REST `status=healthy`.
- **Two possible causes:**
  1. **REST breaker opened between reads** — re-read `/health`; `open_breakers`
     will now be non-empty. Normal eventual consistency between the snapshot
     and a tripped REST breaker.
  2. **The request was MCP, and the breaker is in MCP's local registry** —
     REST `/health` stays healthy because MCP has a separate registry with no
     cross-process propagation (see [§4](#4-the-503-circuit_open-response)).
     Inspect MCP logs for `Circuit open for <kind>`; do not rely on `/health`.

---

## 4. The 503 `circuit_open` response

### Error shape (REST and MCP share this)

**REST (HTTP):**
```json
HTTP 503
{
  "error": {
    "message": "Circuit open for <kind> — cooling down. Retry later.",
    "type": "server_error",
    "param": null,
    "code": "circuit_open"
  }
}
```
`<kind>` is the breaker's `.value` string (e.g. `auth_required`,
`composer_send_readiness`).

**MCP (tool result):** `isError: true`, text
`"Circuit open for <kind> — cooling down. Retry later. (circuit_open, kind=<kind>)"`.

### Finding the open breaker — REST vs MCP differ

> ⚠️ **MCP has its own breaker registry.** The MCP process creates a separate
> local `BreakerRegistry` (`mcp_server.py:1891`) with **no cross-process
> propagation** to/from REST (`mcp_server.py:1890,664`). REST's `/health`
> serializes **REST's** registry only — it does **not** reflect MCP-local
> breaker state. An MCP `circuit_open` can occur while REST `/health` still
> shows `open_breakers: []` and `status: healthy`.

**REST `circuit_open`:** check REST `/health` for the authoritative list:
```
GET http://localhost:8080/health  →  open_breakers, breakers{}
```
This is reliable for REST because the failing request and the health snapshot
share the same registry.

**MCP `circuit_open`:** `/health` is **not** a reliable source — the open
breaker is in MCP's local registry, not REST's. Inspect the **MCP process logs**
(stderr via the supervisor) for the `Circuit open for <kind>` line, then act on
that `<kind>` per [§3 failure modes](#3-common-failure-modes) and
[§5 breakers](#5-breaker-states-and-cooldowns). Restart or wait the MCP process
according to your supervisor policy (see [os-supervision.md](os-supervision.md)).
(Exception: the `auth_required` breaker is effectively shared in practice —
both processes trip it on the same expired session — but the registry state is
still per-process; confirm via MCP logs.)

### Only the first open breaker is reported

`first_open()` returns breakers in enum order (`auth_required`,
`composer_send_readiness`, `cdp_reconnect`, `chrome_crash_loop`). An error
naming one breaker does **not** mean others aren't also open — within REST,
check `/health` `open_breakers` for the full list; within MCP, grep the logs for
all `Circuit open` lines.

---

## 5. Breaker states and cooldowns

The system tracks exactly **four** breakers. All thresholds/cooldowns are
**hardcoded** in `breakers.py` — there is **no env override** (do not look for
`W2A_BREAKER_*`; it does not exist). The only breaker-adjacent env tunable is
`W2A_ENSURE_BREAKER_COOLDOWN_GRACE_S` (default 5s), which is an `ensure`
supervisor grace budget, not a breaker cooldown.

### The four breakers

| Kind (`.value`) | Trips on | Threshold / window | Cooldown | Resets on |
|-----------------|----------|--------------------|----------|-----------|
| `auth_required` | explicit `trip()` — login page instead of data, or HTTP 401 | single-shot | **indefinite** (`None`) | successful `recover_auth()` token refresh (explicit `reset()`) — **never auto-recovers** |
| `composer_send_readiness` | send-readiness / send failures | 3 / 120s | 300s | a confirmed successful send (`record_success`) |
| `cdp_reconnect` | failed CDP reconnects | 5 / 120s | 120s | successful reconnect + token refresh |
| `chrome_crash_loop` | Chrome restarts | 3 / **300s** | 300s | cooldown half-open only (no `record_success` path) |

### Conceptual states (the code does not use closed/open/half-open as literal names)

- **tripped** — the persistent latch flag is set.
- **open** — tripped AND still within cooldown (or `cooldown_until is None` for
  auth). `is_open()` returns true; requests fail-fast.
- **half-open** — tripped but past cooldown. `is_open()` returns **false**; a
  successful trial (`record_success`) closes it, a fresh failure burst re-trips.
- **closed** — fresh state (after `reset()` or successful recovery).

```
[closed] ──failures≥threshold──► [open] ──cooldown elapses──► [half-open]
                                   │                              │
                                   │                          success → [closed]
                                   │                          failure → [open]
                                   │
                          (auth: cooldown_until=None, indefinite)
                                   │
                          reset() via recover_auth ──► [closed]
```

### Reading breakers in `/health`

`open_breakers`: `[]` = nothing currently blocking; non-empty = listed kinds
are open. The nested `breakers` object gives per-kind detail:

| Field | Type | Meaning |
|-------|------|---------|
| `open` | bool | `is_open()` result |
| `reason` | string \| null | why tripped; auto-trips read `"<threshold> failures in <window>s window"` |
| `tripped_at` | float \| null | **monotonic** timestamp (only comparable within the process) |
| `cooldown_until` | float \| null | monotonic expiry; `None` for closed or sticky-auth |
| `cooldown_seconds_remaining` | float \| null | seconds left; **`None` is overloaded** = closed **OR** sticky-auth; `0.0` = half-open |
| `failures_in_window` | int | failures in the current rolling window |

> **Gotcha:** `cooldown_seconds_remaining == null` does not distinguish "closed"
> from "sticky auth". Distinguish auth by `kind == "auth_required" && open == true`.

---

## 6. Auth recovery flow

`auth_required` is the one breaker that needs a human. It is indefinite and
does not self-heal — only a successful token refresh closes it.

### Detection
- `ensure` exits **2**.
- `/health` `open_breakers` contains `"auth_required"`.
- Chat requests return 503 `circuit_open` with kind `auth_required`.
- The breaker `reason` is `"login page returned instead of data"` or
  `"HTTP 401 from backend-api"`.

### Automatic recovery attempt (always tried first)
Every chat-request preflight, on REST and MCP, calls `driver.recover_auth()`
before raising `CircuitOpenError`. `recover_auth()` probes `/api/auth/session`
for a fresh token; if it succeeds, it resets the breaker and the request
proceeds. So a transient token lapse can self-clear on the next request. If the
session itself is dead, recovery fails and the breaker stays open.

### Manual recovery (when auto-recovery fails)
1. **Log in to ChatGPT in the Chrome window REST owns.** On a headed
   deployment, open the visible Chrome and complete login at chatgpt.com. On a
   headless/remote deployment, use cookie export (see [deployment.md](deployment.md)
   Option 2) or a headed session to re-establish the session.
2. **Trigger a token refresh.** Any chat request will invoke `recover_auth()`
   in preflight; a successful `/api/auth/session` refresh resets the breaker.
   Or re-run `ensure` — if the session is now valid, it exits 0.
3. **Confirm** `/health` shows `auth_required` no longer in `open_breakers`.

### What does NOT work
- **Restarting REST.** `ensure` deliberately does **not** restart REST on
  `auth_required` (a restart can't fix a login problem; it just relaunches
  Chrome into the same logged-out state).
- **Waiting.** The cooldown is `None` (indefinite). It will not time out.

---

## 7. Safe restart sequence

Restarting REST relaunches Chrome, which is destructive: in-flight requests
are lost, the visible Chrome window flashes closed/reopened, and any unsaved
page state is gone. Restart only when necessary, and let `ensure` do it.

### Golden rule: restart on process exit, NOT on `degraded`

`ensure` already encodes the correct degraded policy:
- `broken` → restart immediately.
- `degraded` (driver dead, no breaker) → poll up to ~20s, then restart.
- `degraded` (timed breaker) → wait `cooldown + grace`, then restart.
- `degraded` (`auth_required`) → **do not restart** (exit 2).

A supervisor that restarts faster than `ensure`'s degraded budget will **race**
it and cause destructive browser flapping. See [os-supervision.md](os-supervision.md)
"Restart policy".

### When YOU should restart
1. **`status=broken` that doesn't self-heal** after one `ensure` cycle.
2. **Chrome is wedged** (CDP port probe fails, `chrome_crash_loop` tripping).
3. **After a code/config change** that requires a fresh process.

### Code/package updates — restart the processes, do not just re-`ensure`

> **`ensure` is a readiness reconciler, not a hot-reload or deploy mechanism.**
> It reconciles REST + MCP/SSE to a healthy state and will cold-start a missing
> process, but it will **not** restart an already-healthy process merely because
> the source code changed. A post-merge process running stale code is healthy by
> `ensure`'s definition, so it is left alone.

After pulling a code/package update (e.g. `pip install -U`, a merged fix), the
long-lived processes must be **restarted explicitly** to load the new code.
**Do not restart REST and MCP back-to-back** — MCP exits during startup if it
cannot reach Chrome/CDP (`run_mcp()` logs `"Cannot connect to Chrome"`, cleans
up, and returns without starting the SSE listener), so a blind sequence leaves
no MCP listener. Restart REST **first**, confirm REST/Chrome/CDP is ready,
**then** restart MCP.

**Split-service style** (the recommended always-on layout from
[os-supervision.md](os-supervision.md)) — ordered, not back-to-back:

```bash
# 1. Restart REST (it owns Chrome, so this relaunches Chrome too).
systemctl --user restart chatgpt-web2api.service

# 2. Wait until REST/Chrome/CDP is actually ready. `ensure` reconciles and
#    exits 0 only when REST is ready — or poll /health until
#    chrome_running=true and driver_connected=true.
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222

# 3. NOW restart MCP/SSE. It connects to the already-ready Chrome over CDP.
systemctl --user restart chatgpt-web2api-mcp.service

# 4. Re-run ensure (or an MCP tool call) to confirm the SSE listener came up.
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222
```

Why the ordering matters: MCP/SSE attaches to REST-owned Chrome over CDP and
has **no Chrome of its own**. Restarting MCP while REST is mid-restart (Chrome
not yet listening on the CDP port) causes MCP to exit with "Cannot connect to
Chrome" and leaves no SSE listener — a silent failure under `Type=simple`.

- **`ensure`-timer style** — there is no long-lived supervisor to restart;
  kill the existing REST and MCP processes, then re-run `ensure` to cold-start
  them in the correct order (REST first; `ensure` reconciles SSE only after
  REST is ready, so the ordering is built in).
- **General rule:** restart **both** REST and MCP/SSE after a code change, even
  if you think only one surface was touched. The safer default avoids the
  "REST looks healthy but MCP runs stale code" state (which silently broke
  MCP-only fixes — see the #31 `list_conversations` schema fix, where the merged
  code did not take effect until the MCP process was restarted).

After the restart, run `ensure` once more to confirm readiness, then validate
per [§8](#8-post-deploy--post-restart-validation).

### The safe restart
```bash
# 1. Stop gracefully if you can (sends SIGTERM; REST drains).
#    On systemd:  systemctl --user stop chatgpt-web2api.service
# 2. Let ensure reconcile (it will cold-start REST + MCP if they're gone).
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222
# 3. Verify — see §8.
```

If `ensure` exits 2, **stop and do auth recovery (§6)** — do not loop `ensure`.

---

## 7a. Parallel mode: process death & Chrome ownership

> Applies only when `parallel_tabs=true`. In the default (`parallel_tabs=false`)
> single-worker mode there is exactly one process and this section does not apply.

When parallel mode is enabled, only the **owner** process (the one that launched
Chrome) manages the Chrome lifecycle. Other processes are **attachers**. This
split is structural (`_owns_chrome`), not lock-based — see
[deployment.md → Parallel mode](deployment.md#parallel-mode-one-chrome-many-tabs).

**Attacher process dies** → Chrome should remain running. The owner's health
monitor is the only one that restarts; an attacher's monitor no-ops on restart
by design. No action required beyond restarting the attacher process itself
(its owned tab is reclaimable on restart only if it uses a stable
`W2A_INSTANCE_ID`; a stdio MCP process whose identity was PID-derived starts a
fresh tab).

**Owner process dies** → Chrome may be **orphaned**: it keeps running but no live
process owns its lifecycle, so no monitor restarts it and attachers will not
self-promote. This is **intentional in the current design** — runtime owner
failover / an ownership lease is deferred future work, not an incident. Recover
with the normal [safe restart sequence (§7)](#7-safe-restart-sequence): restart
the REST/owner process first, wait for Chrome/CDP readiness, then restart
MCP/SSE or attachers. A freshly started process finds the CDP port and elects
owner (or attaches) per `ChromeProcess.ensure_running`.

If an operator depends on restart-on-crash surviving owner death, supervise the
owner process with OS-level restart (see [os-supervision.md](os-supervision.md))
so a new owner process starts automatically.

---

## 8. Post-deploy / post-restart validation

> **After a code/package update, restart in dependency order.** `ensure` does
> not hot-reload code (see [§7](#7-safe-restart-sequence)), and REST/MCP must
> not be restarted back-to-back (MCP exits if Chrome/CDP isn't ready when it
> starts). The correct code-deploy order is:
>
> 1. restart **REST** (owns Chrome)
> 2. reconcile/wait for **REST readiness** (`ensure`, or poll `/health` until
>    `chrome_running=true` and `driver_connected=true`)
> 3. restart **MCP/SSE** (now that Chrome is listening)
> 4. run `ensure` to confirm both came up
> 5. validate below
>
> The steps below assume the processes are running the code you intend to
> validate. For a non-code restart (e.g. recovering from a crash), skip to
> step 1 and let `ensure` reconcile.

Run in order; stop at the first failure.

```bash
# 1. ensure exited 0 (REST + SSE ready) — or re-run it:
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222
# expect: exit 0, "REST + SSE ready"

# 2. Health
curl -s http://localhost:8080/health | python -m json.tool
# expect: status healthy|starting, chrome_running true, driver_connected true,
#         open_breakers [], last_error null

# 3. One real chat send (end-to-end; exercises the full send + completion path)
curl -s -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","stream":false,"messages":[{"role":"user","content":"Reply with exactly: ok"}]}'
# expect: HTTP 200, content "ok", finish_reason "stop"
```

> Use **`Reply with exactly: ok`**, not bare `"ok"`. Plain `"ok"` is often
> interpreted as an acknowledgement cue and returns `"Acknowledged."` — still a
> valid 200, but not an exact-output sanity check. For runtime validation, the
> hard pass is HTTP 200 + OpenAI-compatible shape + `finish_reason: stop`; the
> exact content is a best-effort cue unless you add a stronger system instruction.

Step 3 is the only signal that the composer DOM path, completion detection, and
backend fetch all work end-to-end. Steps 1–2 are preconditions.

### 4. (If MCP/SSE is used) One MCP tool call

The REST send does **not** exercise MCP output-schema validation. After a
change touching MCP tool schemas or the MCP server, call an affected MCP tool
and confirm it returns `isError: false`. For example, `list_conversations`
(which returns `update_time` as an ISO string and `gizmo_id` as null — the
shape that broke structured-output validation in #31 before the fix):

```python
# python -m pip install mcp   (if not already installed)
import asyncio
from mcp import ClientSession
from mcp.client.sse import sse_client

async def main():
    async with sse_client("http://localhost:8090/sse") as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("list_conversations", {"limit": 3})
            print("isError:", res.isError)          # expect: False
            print("count:", len((res.structuredContent or {}).get("conversations", [])))

asyncio.run(main())
```

`isError: False` with conversations returned = the MCP path is healthy. Omit
this step only if your deployment does not use MCP/SSE.

---

## 9. Log collection

`chatgpt-web2api` logs to **stderr** at the configured level. There is no
built-in log file or rotation — capture via the supervisor's stdout/stderr
redirection (see [os-supervision.md](os-supervision.md) per-OS sections).

Control verbosity with `W2A_LOG_LEVEL` (`DEBUG`/`INFO`/`WARNING`/`ERROR`;
default `INFO`):

```bash
W2A_LOG_LEVEL=DEBUG  # most verbose; use for incident diagnosis
```

### What to look for in logs during an incident
- **`Circuit open for <kind>`** — a breaker tripped; cross-reference
  `/health` `open_breakers`.
- **`auth_required` / "login page returned instead of data" / "HTTP 401"** —
  auth expired; see [§6](#6-auth-recovery-flow).
- **repeated Chrome restart lines** — heading toward `chrome_crash_loop`.
- **`phase_1_appear` / `phase_2_stream` GenerationStuckError** — completion
  detection stalled; usually transient, persistent cases suggest DOM drift.
- **`end_turn fetch failed`** — transient backend fetch error (the DOM
  action-button fallback covers it; not fatal on its own).

---

## Appendix: source citations

All field names, state values, thresholds, and exit codes above are verified
against:

- `/health` schema & `status` conditions — `src/chatgpt_web2api/api_server.py:108-186`
- Breaker kinds, policies, states — `src/chatgpt_web2api/breakers.py:46-109, 133-252`
- Breaker trip/reset sites — `backend_client.py:187,210-211,344`; `chatgpt_dom.py:173,206,277,418,427`; `cdp_driver.py:592,599`; `chrome.py:40-68`
- 503 `circuit_open` mapping — `api_server.py:377-397,467-480`; MCP `mcp_server.py:1519-1576`
- `ensure` exit codes & flow — `ensure.py:14-19,725-790`
- Degraded polling policy & tunables — `ensure.py:540-689`; `config.py:89-96,181-187,214-219`
- Detached child launch — `ensure.py:305-316`; REST cmd `:260-280`; SSE cmd `:283-302`
- Chrome ownership — `ensure.py:8-9` ("REST owns Chrome; SSE attaches and never launches Chrome")

Thresholds are hardcoded in `breakers.py:104-109`; there is no env override.

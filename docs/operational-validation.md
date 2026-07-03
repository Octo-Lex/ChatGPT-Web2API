# Operational Validation — Parallel Multi-Tab Mode

> Status: **`parallel_tabs` is merged (PR #33) and opt-in available. NOT yet
> operationally accepted.** This checklist tracks the live validation required
> to promote the wording to "operationally accepted." Until then, treat the
> feature as "available, not production-proven."

This is the operational acceptance gate for the parallel multi-tab feature
(`parallel_tabs=true`; see [deployment.md → Parallel mode](deployment.md#parallel-mode-one-chrome-many-tabs)).
It complements — does **not** replace — the unit/integration suites
(`test_lock_resolver`, `test_parallel_tabs_pr4`, `test_chrome_lifecycle`),
which are authoritative for the locking/serialization/fail-closed invariants.
The checks below exercise those invariants against **real Chrome, real ChatGPT
DOM, and a real shared account/session**.

## Preflight gate (must be true before starting)

- [ ] **No mixed workers on the CDP port.** Every worker targeting this Chrome
      instance is on the new `parallel_tabs=true` code, OR every worker is on
      default-off legacy mode. Old port-lock-only and new per-target-lock
      workers do **not** exclude each other; mixing them reintroduces
      split-brain. (See deployment.md rollout warning.)
- [ ] **Distinct `W2A_INSTANCE_ID` per worker** (or rely on the transport-aware
      default: `rest:{port}` / `mcp:sse:{host}:{port}` / `mcp:stdio:{pid}`).
      Do NOT reuse one `W2A_INSTANCE_ID` across live workers — they will collide
      on one tab-registry entry.

## 1. Default-off smoke

Deploy current `master` with `parallel_tabs=false`.

- [ ] Legacy single-worker behavior is unchanged (one request at a time per tab).
- [ ] No `owned_tab_required` error leaks into legacy mode.
- [ ] Restart in dependency order: REST/owner first → wait for Chrome/CDP
      readiness → MCP/SSE/attachers. (See [runbook §7](runbook.md#7-safe-restart-sequence).)

## 2. Parallel canary (2+ workers, one Chrome)

`parallel_tabs=true`, `tab_mode=owned`, two REST workers on distinct REST ports
sharing one CDP port. Run `scripts/parallel_canary.py --ports 8081,8082 --cdp-port 9222`.

- [ ] Each worker obtains a **distinct owned tab** (CDP `/json/list` shows ≥2
      chatgpt.com page targets; canary JSON `cdp_targets.chatgpt_tabs_after ≥ 2`).
- [ ] Different-tab sends proceed **concurrently** (canary `concurrent.total_window_s`
      meaningfully less than the sum of individuals).
- [ ] All concurrent requests return OpenAI-compatible 200s.
- [ ] No unexpected `circuit_open` / `auth_required` breakers tripped solely by
      parallel pressure (rate-limit backoff is expected; a *tripped* breaker is
      the signal to investigate).

## 3. Same-tab serialization (sanity, not re-proof)

Two concurrent requests to the **same** worker (canary `same_worker_serialization`).

- [ ] Timing is consistent with serialization (total ≈ sum of individuals).
      **Note:** live timing against ChatGPT is noisy — backend latency,
      streaming, rate limits, and DOM readiness all confound it. Treat this as
      advisory; the unit suite (`test_lock_resolver::test_mutation_lock_serializes_same_target`)
      is authoritative. If timing looks parallel, investigate via logs (lock
      acquire/release order) before concluding a bug.

## 4. Failure-mode validation

- [ ] **Kill an attacher process** → Chrome remains alive; the owner's monitor
      is unaffected. Restarting the attacher reclaims a tab (or starts a fresh
      one if its identity was PID-derived stdio MCP).
- [ ] **Kill the owner process** → Chrome is orphaned until a new process
      elects. This is **intentional** (deferred failover), not a bug — see
      [runbook §7a](runbook.md#7a-parallel-mode-process-death--chrome-ownership).
      Recover via the safe restart sequence.
- [ ] **Force tab loss / target drift** (e.g. close an owned tab mid-mutation,
      or force a reconnect that lands on a different target) → the in-flight
      operation fails **retryably** with REST 503 `code=owned_tab_required` /
      MCP `isError` marker `(owned_tab_required)`. No silent adoption of
      another process's tab.
- [ ] **Concurrent MCP processes** → transport-aware identity (`mcp:sse:…` /
      `mcp:stdio:…`) gives each a distinct tab-registry entry; no thrash on the
      shared registry key.

## 5. Observability pass

Capture logs from a normal parallel run AND from each failure mode above.

- [ ] An operator can identify **which worker / REST port / transport** failed
      without attaching a debugger.
- [ ] The new log lines from PR2/PR4 surface correctly under parallel mode:
      owner vs attacher (`_owns_chrome`), "Monitor disabled: attached to
      existing Chrome", "Refusing restart: not Chrome owner", the
      `owned_tab_required` markers, drift-guard raises.
- [ ] `/health` reflects REST-side state (note: MCP has its own breaker
      registry that REST `/health` does **not** reflect — see
      [runbook §9](runbook.md#9-log-collection)).

## 6. Exit criterion

When **all** of §1–§5 pass against the live environment:

- [ ] Update release wording from *"merged and opt-in available"* to
      *"parallel multi-tab mode is operationally accepted."*
- [ ] Record the validation run (date, environment, canary JSON) alongside this
      checklist.

Until then, the feature status stays at **opt-in available, not production-proven.**

---

## Out of scope for this validation (tracked as future work)

- Cross-instance pool / single-endpoint router (each worker still needs its own
  local REST/MCP port).
- Owner-process runtime failover / ownership lease (owner death orphans Chrome
  until a new process elects — see runbook §7a).
- Conversation-scoped / project-scoped / account-scoped locks (deliberately
  dropped during design — backend serializes those).

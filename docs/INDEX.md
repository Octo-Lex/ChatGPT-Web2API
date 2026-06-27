# Documentation Index

This is the navigation hub for `chatgpt-web2api` documentation. Every doc in
`docs/` is listed here with a one-line purpose. If you don't know which one to
read, start with the [routing table](#which-doc-should-i-read) below.

---

## Which doc should I read?

| I want to… | Read this first |
|------------|-----------------|
| **try it locally for the first time** | [deployment.md](deployment.md) → Option 1 (pip install) |
| **run it in Docker / headed Chrome in a container** | [deployment.md](deployment.md) → Docker section + [reverse-engineering-notes.md](reverse-engineering-notes.md) (anti-bot caveats) |
| **share it with a non-technical user** | [deployment.md](deployment.md) → cookie-export / remote options |
| **run it always-on under systemd / launchd / NSSM** | [os-supervision.md](os-supervision.md) |
| **diagnose an incident or interpret `/health`** | [runbook.md](runbook.md) |
| **recover from a `circuit_open` / auth expiry** | [runbook.md](runbook.md) → §4 (circuit_open) + §6 (auth recovery) |
| **call the REST or MCP API from code** | [api-reference.md](api-reference.md) |
| **understand how the codebase is structured** | [architecture.md](architecture.md) |
| **understand why we drive Chrome via CDP** | [adr-0001-automation-backend.md](adr-0001-automation-backend.md) |
| **see what's planned / what already landed** | [ROADMAP.md](ROADMAP.md) |
| **reverse-engineer a new ChatGPT endpoint** | [protocol-reference.md](protocol-reference.md) + [reverse-engineering-notes.md](reverse-engineering-notes.md) |

---

## Operating guides (Phase 6)

These four are the day-to-day operator surface — start here for running a
deployment.

| Doc | Purpose |
|-----|---------|
| [deployment.md](deployment.md) | **Installing & sharing.** pip / Docker / cookie-export / remote-deploy options, plus cross-links to supervision and the runbook. |
| [os-supervision.md](os-supervision.md) | **Always-on supervision.** systemd (Linux), launchd (macOS), Task Scheduler + NSSM (Windows); `ensure`-on-a-timer and REST+MCP split-service styles. |
| [runbook.md](runbook.md) | **Operating a running deployment.** Startup checklist, `/health` interpretation, failure modes, breaker states, auth recovery, safe restart, post-deploy validation, logs. Source-cited. |
| [api-reference.md](api-reference.md) | **Calling the API.** OpenAI-compatible REST endpoints and the MCP server surface (tools, transports). |

---

## Architecture & decisions

| Doc | Purpose |
|-----|---------|
| [architecture.md](architecture.md) | **Codebase structure.** Module layout, the `CDPDriver` orchestration/interception hub + extracted collaborators (`backend_client`, `cdp_transport`, `chatgpt_dom`, `completion_detector`), data flow. |
| [adr-0001-automation-backend.md](adr-0001-automation-backend.md) | **Decision record.** Why the automation backend stays on CDP (drives a real Chrome) and defers a Chrome-extension approach. |
| [ROADMAP.md](ROADMAP.md) | **Project plan & history.** Phases 1–6, what landed, follow-ups, and the explicit deferral of Group C lifecycle extraction. |

---

## Reverse-engineering & internals

These capture ChatGPT's web internals **as observed at a point in time**.
ChatGPT changes its DOM/API frequently — treat them as a starting map, not a
contract. The `doctor` command (`chatgpt-web2api doctor`) is the live source of
truth for current selector/endpoint health.

| Doc | Purpose |
|-----|---------|
| [protocol-reference.md](protocol-reference.md) | **ChatGPT web API surface.** Autonomously captured backend-api endpoints, auth, request/response shapes. |
| [reverse-engineering-notes.md](reverse-engineering-notes.md) | **DOM & API findings.** Composer selectors, completion signals, anti-bot notes from the Phase-2 / composer-redesign work. |
| [comprehensive-discovery-plan.md](comprehensive-discovery-plan.md) | **Capture methodology.** Plan for a single structured capture of the full ChatGPT feature/model/project surface. |
| [knowledge-gaps.md](knowledge-gaps.md) | **Open unknowns.** What has been captured vs. what still needs probing. |
| [phase1-results.md](phase1-results.md) | **Historical capture.** Phase-1 protocol discovery results (Jun 2026). |

---

## ZCode-internal planning (not operator docs)

Under `docs/superpowers/` are planning artifacts (specs, plans) from ZCode
sessions. They are working notes, not user-facing documentation — not linked
individually here.

---

## Quick reference

```bash
# Start / reconcile (the one command):
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222

# Health:
curl -s http://localhost:8080/health

# Do NOT use (no __main__ block):
#   python -m chatgpt_web2api.ensure
```

See [runbook.md](runbook.md) §1 (startup checklist) and §8 (post-deploy
validation) for the full operational sequence, including the exact-output
`"Reply with exactly: ok"` sanity send.

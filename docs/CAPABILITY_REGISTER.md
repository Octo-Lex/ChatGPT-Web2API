# Capability Register — ChatGPT-Web2API

Status: proposed baseline for maintainer review (2026-11-21).
Baseline: [master@497527dceabfa3f95961e23c291e618c5570f1ac](https://github.com/Octo-Lex/ChatGPT-Web2API/tree/497527dceabfa3f95961e23c291e618c5570f1ac).
Companion: [Engineering Charter](ENGINEERING_CHARTER.md) (proposed); [Roadmap](ROADMAP.md).

## Purpose and update rules

Protect the product users already have, not every implementation detail. No regression may be hidden by deleting an old test, changing its expected result, or calling an implemented capability obsolete merely because the upstream UI changes. Unsupported behavior is not silently promised. A temporary upstream limitation is a recorded gap, not removal of the capability.

Evidence axes:
- **Implementation**: M = present at the pinned merged baseline; P = proposed/unmerged; G = documented gap or not established.
- **Qualification**: S = source inspected; T = test coverage exists (not necessarily executed here); H = historical reported live/operational evidence, limited to its recorded environment/head; C = exact-head CI evidence. These labels can coexist. None means universal live acceptance.
- A README claim, model catalog, prior demo, fixture, or another project's implementation is not current-account proof. Record the exact head/env, scenario, date, outcome and evidence link on promotion. Preserve negative results.
- Each change affecting a capability must identify its ID, retain its user-visible contract or document the authorized replacement, and supply relevant regression and qualification evidence. A changed internal seam is not automatically a product regression; distinguish tests of the contract from tests of implementation details.

## A. Original product proposition

| ID | Protected capability / promise | Implementation / evidence | Limits to preserve honestly |
|---|---|---|---|
| CAP-01 | Use an authenticated ChatGPT Web session through real Chrome/CDP; no separate upstream OpenAI API key or manual token extraction required | M/S/H; README, CHANGELOG 0.1.0, chrome.py, backend_client.py | User login and subscription/account entitlements, provider controls and usage limits apply; local optional API authentication is a different boundary. Not an official OpenAI API or a guarantee of free/unlimited use. |
| CAP-02 | Simple install and start; CLI, persistent browser session, first-run login | M/S/H; README, CHANGELOG, service.py, chrome.py | No forced extra services for basic single-user mode. Account/profile state is not request/session ownership. |
| CAP-03 | OpenAI-style REST chat completions, both documented URL aliases | M/S/T/H; api_server.py, test_unit.py, test_e2e_chat.py | Compatibility subset, not full OpenAI API parity. |
| CAP-04 | True incremental REST SSE, terminal status and final marker | M/S/T/H; api_server.py, completion_detector.py, test_sse_end_turn_new_chat.py, test_e2e_sse.py | HTTP status may already be committed; incomplete/error stream must not be signaled as success. No silent conversion to buffered-only output. |
| CAP-05 | Discover available models and map common model aliases; select model where supported | M/S/T/H; api_server.py, backend_client.py, chatgpt_dom.py, test_e2e_reads.py | Model slugs/availability drift. Selection can fall back today; do not imply verified requested model when not known. Token usage fields are placeholders (zero), not billing telemetry. |
| CAP-06 | Explicit conversation continuation and existing auto-continue | M/S/T/H; api_server.py, mcp_server.py, test_conversation_guard.py, CHANGELOG 0.2.0 | Preserve convenient continuity within the correct session; not permission to continue another client's conversation. Fresh navigation defects remain open. |
| CAP-07 | Project-scoped chat, default project and project discovery (REST /v1/projects and MCP) | M/S/T/H; api_server.py, mcp_server.py, backend_client.py, CHANGELOG | Respect target project and upstream context/memory rules; project association is not a guarantee of model recall. |
| CAP-08 | System instructions via message prepend; project instructions for persistent context | M/S/T/H; api_server.py, mcp_server.py, CHANGELOG | Prepending text is not native API system-role semantics. |
| CAP-09 | MCP SSE and stdio transports with agent-friendly tool results and progress | M/S/T/H; mcp_server.py, test_integration.py, test_progress.py, test_e2e_mcp.py | SSE recommended; stdio remains supported. MCP progress is not necessarily a live text stream; final tool results are collected. |
| CAP-10 | Non-text response completion and meaningful placeholder/retrieval guidance | M/S/T/H; completion_detector.py, cdp_driver.py, test_non_text_response.py, PR #44 | Existing placeholder behavior is not binary/image delivery, attachment upload or full rich artifact support. |

REST also exposes GET /health and GET / as health alias. API schemas and client-visible fields must be retained or migrated deliberately. Historical feature claims: [README](../README.md), [CHANGELOG](../CHANGELOG.md), [API reference](api-reference.md).

## B. MCP tool and discovery surface

All 16 tools below exist at the pinned baseline (M/S); gating is verified in source and test coverage exists. The historical changelog reports 13/13 tools passing for 0.2.0, later live read/chat and project create/update evidence, and a 16-tool opt-in E2E suite. This is **not** a claim of all 16 live-passing now. Record individual tool qualification after explicit authorized tests; destructive operations require separate consent, isolated test data and cleanup.

| ID | Tool | Retained contract | Exposure |
|---|---|---|---|
| MCP-01 | chat_completion | Chat, model, optional system prompt, explicit conversation/project, content/model/conversation_id result | Default |
| MCP-02 | list_models | Discover model slugs/catalog | Default |
| MCP-03 | list_projects | Discover projects and IDs | Default |
| MCP-04 | create_project | Create actual project; instructions and supported memory_scope (shared/default or project-only) | Write opt-in |
| MCP-05 | delete_project | Irreversible project deletion | Destructive opt-in |
| MCP-06 | update_project_instructions | Update persistent project instructions | Write opt-in |
| MCP-07 | list_project_files | List project files (not upload/download guarantee) | Default |
| MCP-08 | list_conversations | List chats, pagination | Default |
| MCP-09 | get_conversation | Paginated user/assistant history of current branch, oldest-first; total/has_more | Default |
| MCP-10 | archive_conversation | Archive/unarchive | Write opt-in |
| MCP-11 | delete_conversation | Delete a specific conversation | Destructive opt-in |
| MCP-12 | list_memories | Read account memories exposed by upstream | Default |
| MCP-13 | create_memory | Request memory creation using chat mechanism | Write opt-in |
| MCP-14 | delete_memory | Delete upstream memory by ID | Destructive opt-in |
| MCP-15 | list_gpts | Discover Custom GPTs | Default |
| MCP-16 | chat_with_gpt | Chat with specified Custom GPT | Default |

MCP-17: resources, resource templates, prompts, argument completion, Pydantic inputs, rich descriptions, ToolAnnotations, structured output schemas, call-time enforcement (M/S/T). Maintain protocol parity between singleton and pooled paths.
MCP-18: risk-tier gate (M/S/T): W2A_ENABLE_WRITE=1 enables four write tools; W2A_ENABLE_DESTRUCTIVE=1 enables three destructive tools; nine default tools include chat (which DOES mutate chat state). Hidden tools are refused at call time. Do not conflate this gate with authentication. Preserve loopback default and honest auth metadata; review network exposure independently.

Sources: mcp_server.py, config.py, backend_client.py; test_gating.py, test_business.py, test_deep.py, test_integration.py, test_e2e_reads.py, test_e2e_destructive.py, test_e2e_mcp.py. Memory changes upstream may affect live availability; retain discovery/management intent, report provider limitation rather than fabricate success or bypass unavailable features.

## C. Operational and architectural achievements

| ID | Capability | State/evidence and boundary |
|---|---|---|
| OPS-01 | Chrome launch/attach/monitor/restart and token/session refresh | M/S/T; chrome.py, cdp_transport.py, backend_client.py; historical CHANGELOG. Only owner may restart Chrome. |
| OPS-02 | ensure point-in-time readiness and ZCode hook integration | M/S/T/H; #15, #17, #20, #44, test_ensure.py. Not a continuous supervisor; REST owns Chrome, SSE attaches. |
| OPS-03 | Honest /health and breakers (including auth recovery) | M/S/T; #18–20; test_health.py, test_breakers.py, test_breaker_failfast.py. Health is not proof of a successful request. |
| OPS-04 | Rate-limit classification, bounded backoff, REST 429/Retry-After and MCP structured error | M/S/T; resilience.py, test_api_rate_limit.py, test_mcp_rate_limit.py, test_retry_after.py. Preserve safe recovery; existing whole-turn retry after dispatch is NOT guaranteed safe. |
| OPS-05 | Reactive diagnostics and doctor / read-only verification | M/S/T/H; CHANGELOG, test_diagnostics.py, test_doctor.py, test_e2e_doctor.py. Opt-in capture; redact tokens and sensitive payloads. Do not authorize write probes implicitly. |
| OPS-06 | Parallel per-target mutation locks, owned tabs, separate instance registry/lifecycle | M/S/T/H; #33–37, [operational validation](operational-validation.md), test_parallel_tabs_pr4.py, test_lock_resolver.py, test_tab_registry.py. Default off; don't mix worker locking modes; shared account is not tenant isolation. |
| OPS-07 | A1 trusted pre-send baseline and A2 turn-anchored reconciliation (UUID and qualified fallback) | M/S/T/H; #38–39, test_send_baseline.py, test_identity_listener.py, test_turn_anchor.py. #39 reports 20/20 canaries for reproduced stale-return; scoped, not universal. |
| OPS-08 | B1 MCP session-affine bounded lazy driver pool | M/S/T/H; #42, mcp_driver_pool.py, session_key.py, test_mcp_driver_pool.py, test_session_key.py. Default off; size 2, requires parallel_tabs + owned. Structural live canary on 142f2bb; full account-burst qualification pending. No REST pooling or account isolation. README pre-B1 warning is stale as a statement of code availability, remains true of unpooled mode. |
| OPS-09 | Model-aware first-content/idle/hard budgets and non-text propagation handling | M/S/T/H; #44, test_detector_budgets.py, test_non_text_response.py. Preserve slow-but-progressing and reasoning cases. |
| OPS-10 | Safe ownership loss and reconnect; no adopt of unrelated tab in owned mode | M/S/T/H; #44, test_owned_mode_no_adopt.py, test_tab_isolation.py. Read recovery does not confer write replay rights. |
| OPS-11 | Modular stable CDPDriver facade and collaborator seams | M/S/T; #22–26, docs/architecture.md, ROADMAP Phase 5. Preserve compatibility seams until migration evidence exists, not every internal line. |
| OPS-12 | Cross-platform packaging, non-e2e CI matrix and separately opted-in live tests | M/S/T/C; .github/workflows/ci.yml; test_e2e_gate.py. Windows/Linux/macOS Python 3.11/3.12 matrix; not universal platform live certification. |

## D. Evidence ledger and unresolved work

| Reference | What is supported | What is NOT supported |
|---|---|---|
| [Master CI 29195499536](https://github.com/Octo-Lex/ChatGPT-Web2API/actions/runs/29195499536) | Exact baseline SHA: all six test jobs and secret-scan succeeded; lint failed; build skipped | All-green baseline, new live test, or claim all features work today |
| [PR #39](https://github.com/Octo-Lex/ChatGPT-Web2API/pull/39) | Merged A2; historical stale-return repro and scoped 20/20 fresh canaries reported | Universal correlation across later branches, repeats and timing cases |
| [PR #42](https://github.com/Octo-Lex/ChatGPT-Web2API/pull/42) | Merged default-off B1; reported structural isolation, reuse and size cap | Full concurrent account workload, default-on safety |
| [PR #44](https://github.com/Octo-Lex/ChatGPT-Web2API/pull/44) | Merged reliability improvements; reported 678 tests and 3/3 historical non-text race verification | Latest CI green or current-UI acceptance |
| [PR #52](https://github.com/Octo-Lex/ChatGPT-Web2API/pull/52) | P: no-replay send primitive, opt-in single_send and unknown-outcome signaling proposed | Merged protection, exactly-once, durable cross-client idempotency, automatic unknown-outcome recovery |
| [PR #56](https://github.com/Octo-Lex/ChatGPT-Web2API/pull/56) | P: fresh-route/composer/Send-vs-Stop checks proposed | Merged fix or elimination of post-navigation displacement |
| [PR #57](https://github.com/Octo-Lex/ChatGPT-Web2API/pull/57) | P: current aria DOM and R1–R3 candidate; earlier 54c4b09 CI 44747099994 green and operator-reported short REST canary; later 6d1a651 CI 44875975647 failed (22 failed/715 passed on Ubuntu 3.11) | Later-head qualification, all live capabilities, closure of mixed DOM hydration or buffered/final text policy |

Gaps: current-upstream feature-by-feature live requalification; real early SSE output under current DOM; same-text and mixed-layout transitions; long/multibranch history; safe unknown-send recovery; authenticated remote exposure policy; explicit REST session isolation; reliable actual-model reporting; non-text artifact format. These are evidence gaps, not approved expansion tasks. Image/file upload, broader Work/Research features, API parity and alternative browser engines are not recorded as existing shipped capabilities here.

## E. Change acceptance

For each touched ID, attach a before/after contract and evidence (including failures), indicate whether a regression is in product behavior or a changed internal test seam, and run the relevant tests on the candidate head. Use the exact-head CI matrix plus targeted authorized live validation where an upstream boundary changed. No new live account operations were performed in compiling this baseline. A capability may be temporarily reported degraded/unsupported with a precise reason; retirement, scope expansion or changing defaults requires an explicit maintainer decision. Amend this register and the roadmap in the same review when scope changes.

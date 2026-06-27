# Changelog

All notable changes to ChatGPT-Web2API will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`delete_project` tool (16th)** — permanently remove a project via `DELETE /backend-api/gizmos/{id}` (gated under `W2A_ENABLE_DESTRUCTIVE=1`). The tool surface is now 16 (was 15).
- **Reactive drift diagnostics + `doctor` command.** When ChatGPT changes its API/UI and a driver function returns a broken shape, a `@diagnose` decorator captures a redacted artifact (request, live response, expected-vs-actual mismatch) under `~/.chatgpt-web2api/diagnostics/`. The new `chatgpt-web2api doctor` command **auto-discovers** which functions are broken (no human names them) and prints the evidence for fast repair; `doctor --verify <function>` re-runs a read function live to confirm a fix. Enabled via `W2A_DIAGNOSE=1` (off by default). Replaces the throwaway probe scripts used during this session's debugging.
- **Rate-limit handling that makes agentic workflows practical.** When ChatGPT shows its "Too many requests" pop-up, the server now (1) detects it fast via DOM scan and raises a typed `RateLimitError` carrying a parsed `retry_after`, (2) **retries transparently** — dismisses the pop-up (clicks "Got it") and re-runs the request up to 3 times with backoff, so transient limits are invisible to the caller, and (3) when persistent, surfaces a **standard OpenAI `429`** with `Retry-After` and `error.type/code = rate_limit_exceeded` on the REST API (auto-retried by the OpenAI SDK / LangChain / LlamaIndex with zero client integration) and a machine-readable `structuredContent` (`{rate_limited, retry_after, error}`) on the MCP server. New `CDPDriver.dismiss_rate_limit()`, `resilience.retry_on_rate_limit()`, `parse_retry_after()`, and a streaming pre-flight check. Documented in README's "Rate Limits & Agent Retry" section.
- **MCP tool access gating** — graduated access control for the MCP server, modeled on the hermes-gpt sidecar pattern. Mutating tools (`create_project`, `update_project_instructions`, `create_memory`, `archive_conversation`) are hidden unless `W2A_ENABLE_WRITE=1`; destructive tools (`delete_conversation`, `delete_memory`) are hidden unless `W2A_ENABLE_DESTRUCTIVE=1`. Safe reads and core chat remain visible out of the box. Gated tools are hidden from `list_tools` *and* refused at call time (defense-in-depth).
- **Honest auth metadata** — every MCP tool now advertises `securitySchemes: noauth` when no API keys are configured, so MCP clients can configure their connector correctly.
- **Non-loopback warning** — the MCP SSE transport logs a prominent warning when bound to a non-loopback address without `api_keys`, since the server is otherwise reachable from the network.
- **Protocol-level integration tests** — a real MCP client session over an in-memory transport now exercises the full initialize → list_tools → call_tool path, complementing the handler-level unit tests.
- **Full end-to-end test suite** (`W2A_E2E_RUN=1 pytest -m e2e`) — opt-in tests that drive a real ChatGPT account via Chrome CDP, exercising all 16 tools across the driver↔ChatGPT boundary. Deselected by default and excluded from CI (`pytest -m "not e2e"`). Safety model: snapshot/diff for destructive ops, a guaranteed-cleanup finalizer, unique markers, and inter-test pacing (`W2A_E2E_PACE`) to avoid ChatGPT rate limits.

### Fixed
- **`delete_memory` output validation** — the `delete_memory` tool shared `DELETE_RESULT_OUTPUT` with `delete_conversation`, which requires `conversation_id`, but the handler returns `memory_id`. Any `delete_memory` call therefore failed MCP output validation once it was actually reachable. Given a dedicated `DELETE_MEMORY_RESULT_OUTPUT`. Surfaced by the new integration tests (the gating previously hid the tool, masking the bug).
- **`_js_with_data` global `__D` collision (critical)** — the data-injection helper emitted a top-level `const __D = {...};`, which collides with the global `__D` that chatgpt.com's own page defines, raising `SyntaxError: Identifier '__D' has already been declared`. The exception was swallowed by the eval wrapper, so every `_js_with_data`-based read silently returned empty — `list_memories`, `list_projects`, conversation detail, and others returned 0 even though the underlying API calls succeeded. Rewritten to pass `__D` as an IIFE parameter (no declaration to collide, and the data is still passed as a JSON-serialized argument, never string-concatenated). Surfaced by the first live end-to-end test against a real ChatGPT account: before the fix, reads returned 0 and the chat response came back empty; after, `list_models`=15, `list_conversations`=5, `list_projects`=50, `list_memories`=40, and `chat_completion("What is 2+2?")`→`"4"`.
- **`get_models` returned a raw JSON string** — the method returned `await r.text()` (a 39KB string) despite a `-> list[dict]` signature, so `do_list_models` crashed on `m.get('slug')`. Now parses the JSON and extracts the `models` array. Surfaced by the read-only E2E suite; the mocked unit tests returned dicts and masked it.

### Fixed
- **`create_project` creates a true Project (snorlax)** — ChatGPT split Projects and Custom GPTs into separate endpoints. Projects live at `POST /backend-api/projects` (create a `snorlax` gizmo, id `g-p-...`); the legacy `/backend-api/gizmos` endpoint creates a `gpt` gizmo instead. The old code hit `/gizmos`, so every "project" it created was actually a Custom GPT. Now uses `/projects` with the real minimal payload `{name, instructions, memory_scope}` (captured via Super-Browser UI automation). `memory_scope` maps correctly: `project_v2` (Project-only) stays; `global` (Default/shared) → API's `unset`. Verified live: returns a `g-p-` id with `gizmo_type=snorlax`.
- **`update_project_instructions` works** — now uses `PATCH /backend-api/projects/{id}` with a flat body (was hitting `/gizmos/{id}` which 405s). The API requires the current `name` in the body, so we fetch it first. Captured via Super-Browser, verified live. Un-xfailed.
- **Semantic drift detection** — the diagnostic classifier gained `assertions` (semantic checks after the shape check passes), so it now catches "right shape, wrong thing" drift it previously missed. `create_project` asserts `gizmo_type == "snorlax"` — the exact bug that hid behind a valid `{id,name}` shape during the repair. `create_project` now surfaces `gizmo_type` in its return so the assertion can fire on real results.

### Changed
- The tool surface is now **16 tools** (was 15): added `delete_project` (DELETE `/backend-api/gizmos/{id}`, gated under `W2A_ENABLE_DESTRUCTIVE=1`). The "15 tools" invariant across unit/deep/gating/integration tests updated to 16.
- `list_tools` now returns the gated surface via the new `build_tools()`; `_build_tools()` (all 16, unfiltered) is retained for tests.
- **Phase 5 `cdp_driver.py` split — internal refactor, no behavior change.** The ~2986-line monolith was reduced to **1558 lines** by extracting four focused modules while keeping the public `CDPDriver` API byte-stable as an orchestration/interception hub:
  - `backend_client.py` (#22) — token/session/conversation fetch + project/memory CRUD. Also landed follow-up A: bounded transient-404 retry in `_fetch_text` (#23).
  - `cdp_transport.py` (#24) — CDP websocket/session/reconnect primitives.
  - `chatgpt_dom.py` (#25) — composer selectors, send-readiness, typing/send, rate-limit dismiss.
  - `completion_detector.py` (#26) — Phase-1 assistant-node-appear loop + Phase-2 stream/completion-detection loop (delta-only sub-generator re-yielded by the driver).
  - `CDPDriver` retains thin delegators as the **monkeypatch interception seam** used by the extracted modules and the test suite — these are intentional, not cruft (a post-extraction audit found zero delete-safe). Lifecycle/tab-ownership/reconnect extraction (Group C) is deferred as a separate high-risk initiative. See `docs/ROADMAP.md` (Phase 5) for the full landing record and rationale.

## [0.2.0] - 2025-06-06

### Added
- **MCP Server** — expose ChatGPT as an MCP server with 15 tools, resources, and prompts for AI agents
- **15 MCP tools**: chat_completion, list_models, list_projects, list_conversations, get_conversation, delete_conversation, archive_conversation, create_project, update_project_instructions, list_project_files, list_memories, create_memory, delete_memory, list_gpts, chat_with_gpt
- **Prompt argument completion** — autocomplete project names in MCP prompts
- **Memory management** — list (41 memories via `/backend-api/memories`), create (via chat), delete
- **Custom GPT interaction** — list and chat with Custom GPTs
- **Project file listing** — read files attached to ChatGPT projects
- **Archive/unarchive conversations** — reversible alternative to delete
- **Token auto-refresh** — proactive JWT refresh via `ensure_token()`
- **Source guide** (`guide.md`) — teaches AI agents ChatGPT's mental model
- **Rich tool descriptions** — domain knowledge baked into every MCP tool description
- **Output schemas** — structured output on all tools following Memory server pattern
- **Pydantic input validation** — BaseModel schemas for all tool inputs
- **Tool name enum** — prevents string typos in tool routing
- **Resource templates** — dynamic URI templates for project resources
- **Docker deployment** with cookie injection
- **First-run login flow** — auto-detects when user needs to log in
- **Deployment guide** at `docs/deployment.md`
- **Protocol reference** at `docs/protocol-reference.md`

### Changed
- Refactored MCP server to follow official `modelcontextprotocol/servers` patterns
- CDP driver now has 24 methods (was 6)
- All MCP tools have full ToolAnnotations with all 4 hints

### Tested
- Live tested against ChatGPT Plus account: 13/13 tools pass
- 17 models, 50 projects, 41 memories verified
- Chat completion: "8+7?" → "15", multi-turn "×3" → "45"
- Archive + unarchive round-trip verified
- Memory DELETE verified (200 OK)

## [0.1.0] - 2025-06-04

### Added
- Initial release — CDP-driven proxy with OpenAI-compatible API
- Chrome lifecycle management (launch, attach, monitor, restart)
- Message input via `Input.insertText` + JS `MouseEvent` sequence
- Response retrieval via DOM polling + conversation API hybrid
- Streaming SSE support
- Multi-turn conversation continuity
- System prompts via text prepend
- OpenAI Python SDK compatibility
- 19/19 end-to-end tests pass
- 6 clean modules, 1,314 lines

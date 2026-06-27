# Architecture

## System Overview

ChatGPT-Web2API is a CDP-driven proxy that sits between your code and ChatGPT's web interface. It controls a real Chrome browser via the Chrome DevTools Protocol (CDP) to type messages, click buttons, and read responses.

```
                         ┌──────────────────────────────────┐
                         │         Chrome Browser           │
                         │  ┌──────────────────────────┐   │
  HTTP/MCP    CDP        │  │    chatgpt.com            │   │
  Requests    Commands   │  │  ┌────────────────────┐   │   │
────────────►│──────────►│  │  │ ChatGPT Web UI     │   │   │
             │           │  │  │  • Message input    │   │   │
◄────────────│◄──────────│  │  │  • Send button      │   │   │
  JSON/SSE   Events      │  │  │  • Response DOM     │   │   │
                         │  │  └────────────────────┘   │   │
                         │  └──────────────────────────┘   │
                         └──────────────────────────────────┘
```

## Module Architecture

```
src/chatgpt_web2api/
│
├── __main__.py        CLI entrypoint
│   └── Parses CLI args, calls service.run()
│
├── config.py          Configuration
│   └── Loads from: CLI args → env vars → config.json → defaults
│
├── chrome.py          Chrome lifecycle
│   ├── Launch Chrome with --remote-debugging-port
│   ├── Monitor process health
│   └── Restart on crash
│
├── cdp_driver.py      CDPDriver — orchestration facade + interception hub (1558 lines)
│   ├── Lifecycle/tab ownership: connect, reconnect, close, heartbeat,
│   │   owned-tab discovery/adoption, token refresh  (Group C — not extracted)
│   ├── Navigation: select_model, navigate_new_chat/conversation/gpt,
│   │   ensure_current_conversation
│   ├── send_and_stream: the high-level send + stream orchestration shell
│   │   (delegates completion-detection loops to completion_detector.py)
│   └── Thin delegators: the monkeypatch interception seam — every transport/
│       DOM/backend call routes back through self._driver.<method> so test
│       patches on the driver propagate into the collaborators. These are
│       intentional (see Phase 5 audit in docs/ROADMAP.md), not removable.
│
├── backend_client.py      Token / session / conversation fetch + project/memory CRUD
│   └── Extracted Phase 5 (#22). Holds a driver back-reference; routes CDP via
│       self._driver._js* and token refresh via self._driver.ensure_token.
│
├── cdp_transport.py       CDP websocket / session / reconnect primitives
│   └── Extracted Phase 5 (#24). Owns _ws/_msg_id/_pending; calls back into
│       reconnect() on socket death.
│
├── chatgpt_dom.py         Composer / selectors / send-readiness / rate-limit dismiss
│   └── Extracted Phase 5 (#25). Routes transport/breakers/navigation through
│       self._driver; owns no long-lived state.
│
├── completion_detector.py Phase-1 appear loop + Phase-2 stream/completion loop
│   └── Extracted Phase 5 (#26). Delta-only async sub-generator; the driver
│       re-yields its chunks. Yields deltas only — never finish_reason, never
│       writes _current_conv_id.
│
├── api_server.py      OpenAI-compatible HTTP server
│   ├── POST /v1/chat/completions (streaming + non-streaming)
│   ├── GET /v1/models
│   └── ahttp web server
│
├── mcp_server.py      MCP server (15 tools)
│   ├── Tool definitions with Pydantic input schemas
│   ├── ToolName enum for type safety
│   ├── ToolAnnotations (all 4 hints)
│   ├── outputSchema on every tool
│   ├── Resource templates for projects
│   ├── Prompt argument completion
│   └── Business logic functions (pure, testable)
│
└── service.py         Orchestrator
    ├── Start Chrome → connect CDP → start API/MCP
    ├── Route requests to CDP driver
    └── Handle Chrome disconnections
```

## Message Flow

```
1. Request arrives (HTTP or MCP)
2. service.py routes to appropriate handler
3. Handler calls cdp_driver method:
   a. Navigate to chatgpt.com conversation
   b. Type message via Input.insertText
   c. Click send via JS MouseEvent sequence
   d. Poll DOM for response (or fetch from conversation API)
4. Return formatted response
```

## Key Design Decisions

### CDP over HTTP API

ChatGPT's web API has anti-bot protections (Turnstile, PoW, sentinel tokens). Rather than reverse-engineering and maintaining these, we drive a real browser. The browser handles all challenges automatically.

### DOM Poll + API Hybrid

For thinking models, the DOM is empty during the reasoning phase. We use a hybrid approach:
1. Poll the DOM for response text appearing
2. If DOM doesn't update, fetch from the conversation API
3. Use `current_node` from the conversation mapping for authoritative text

### Multi-turn Auto-continuation

When the same conversation is used (no system prompt or project change), we skip navigation and type directly. This saves ~3-6 seconds per turn.

### Pydantic Input Schemas

Following the official `mcp-server-git` pattern, all tool inputs use Pydantic BaseModel classes. This gives us validation, JSON Schema generation, and IDE autocompletion.

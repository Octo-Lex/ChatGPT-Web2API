![Project Banner](https://github.com/Elephant-Rock-Lab/ChatGPT-Web2API/blob/6c825632520652e20adbabf9dde55d37b7e1cfa0/Banner.png)
<div align="center">

# ChatGPT-Web2API

**Turn ChatGPT into an API. No API key. No token extraction. No sentinel solving.**

One command starts a Chrome browser, logs into ChatGPT, and exposes an OpenAI-compatible API + MCP server.

[![CI](https://github.com/Elephant-Rock-Lab/ChatGPT-Web2API/actions/workflows/ci.yml/badge.svg)](https://github.com/Elephant-Rock-Lab/ChatGPT-Web2API/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Elephant-Rock-Lab/ChatGPT-Web2API)](https://github.com/Elephant-Rock-Lab/ChatGPT-Web2API/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-29%20passing-brightgreen)](https://github.com/Elephant-Rock-Lab/ChatGPT-Web2API/actions)

</div>

---

## Why This Exists

You have a **ChatGPT Plus subscription** but want to use it programmatically:

- **Build AI agents** that chat with ChatGPT via MCP (Model Context Protocol)
- **Use the OpenAI Python SDK** against your ChatGPT account — no separate API key
- **Access ChatGPT Projects** for persistent memory and custom instructions
- **Manage memories, conversations, and projects** from code

Other reverse proxies require token extraction, sentinel challenge solving, or cookie management. This one **drives a real Chrome browser** — anti-bot challenges (Turnstile, PoW) are handled automatically.

## Demo

```bash
$ pip install chatgpt-web2api
$ chatgpt-web2api
✓ Chrome launched on port 9222
✓ Navigating to chatgpt.com...
✓ Ready on http://localhost:8080

$ curl -s http://localhost:8080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"auto","messages":[{"role":"user","content":"What is 8+7?"}]}'
{"choices":[{"message":{"content":"8 + 7 = 15"},"finish_reason":"stop"}]}
```

```python
# OpenAI Python SDK — drop-in replacement
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="not-needed")
print(client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "What is 8+7?"}]
).choices[0].message.content)
# → "8 + 7 = 15"
```

## How It Works

```
┌──────────────┐   OpenAI API   ┌──────────────┐   CDP    ┌──────────────┐
│  Your code   │ ──────────────► │  API Server  │ ────────► │    Chrome     │
│  SDK / curl  │ ◄────────────── │  or MCP      │ ◄──────── │  chatgpt.com  │
│  MCP client  │   JSON / SSE    │  Server      │  events   │  (logged in)  │
└──────────────┘                 └──────────────┘           └──────────────┘
```

The proxy types messages, clicks send, and reads responses — exactly like a human. The browser handles all anti-bot challenges transparently.

## Quick Start

```bash
pip install chatgpt-web2api

# Start — opens Chrome, waits for login on first run
chatgpt-web2api

# Then use it:
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"What is 2+2?"}]}'
# → {"choices":[{"message":{"content":"4"}}]}
```

That's it. **One install, one command, one endpoint.**

## What You Get

### 🌐 OpenAI-Compatible REST API

Drop-in replacement for `api.openai.com`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### ⏱️ Rate Limits & Agent Retry

ChatGPT occasionally shows a "Too many requests" pop-up when conversations are
accessed rapidly. ChatGPT-Web2API turns this from a silent failure into a
**practical, agent-friendly signal** — so autonomous workflows back off and
recover instead of breaking.

**Transparent by default.** When the pop-up appears, the server dismisses it
(clicks "Got it") and retries your request with exponential backoff — up to
3 attempts. A *transient* limit is invisible: your agent gets a normal
successful response and never knows it happened.

**A clean pause signal when it persists.** If the limit survives all retries,
the server surfaces a **standard OpenAI `429`** that every major agent framework
already knows how to handle — with **zero client-side integration**:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 60
{"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded", ...}}
```

The OpenAI SDK, LangChain, LlamaIndex, and similar frameworks auto-retry on
`429` + `Retry-After` by default. The wait (`Retry-After`) is parsed from the
pop-up text when ChatGPT gives an exact number, otherwise a conservative 60s.

**MCP clients** (Claude Desktop, Cursor) get the same signal semantically: a
`CallToolResult` with `isError=True` and a machine-readable `structuredContent`
payload an agent can read to decide "pause, then retry this tool":

```json
{"rate_limited": true, "retry_after": 60, "error": "rate_limit_exceeded"}
```

> **Streaming caveat:** a 429 can be sent *before* streaming begins (a pre-flight
> check), but a limit that appears *mid-stream* (after the 200 is committed) is
> surfaced as an inline `[Error: rate_limit_exceeded — retry in Ns]` chunk,
> since the HTTP status can no longer change. Non-streaming requests are
> unaffected.

### 🩺 Troubleshooting API/UI Drift

ChatGPT changes its web API over time, which can silently break driver functions
(the unit-test mocks can't detect this). ChatGPT-Web2API ships a **reactive
diagnostic** that captures evidence at the moment a function breaks, plus a
`doctor` command that auto-discovers the breakage and prints the evidence for
fast repair.

**Enable capture** (off by default) so breakage in the wild is recorded:

```bash
W2A_DIAGNOSE=1 chatgpt-web2api start
```

When a function returns a broken shape, a redacted artifact is written to
`~/.chatgpt-web2api/diagnostics/<function>-<timestamp>-<seq>.json` with the
exact request, live response, expected-vs-actual mismatch (secrets stripped).

**Auto-discover + diagnose:**

```bash
chatgpt-web2api doctor               # which functions have captured breakage?
chatgpt-web2api doctor create_project# print the captured evidence for one
chatgpt-web2api doctor --verify get_models   # re-run a read function live to test a fix
```

`doctor` (no args) **auto-discovers** which functions are broken — no human
needs to name them. It then prints the evidence an AI coding agent (or you)
reads to propose the corrected payload/selector/parse. `--verify` confirms a
read-path fix against the live account before you commit it. No AI is bundled —
the project captures deterministic evidence; the fix is human-applied.

### 🤖 MCP Server (16 Tools for AI Agents)

Expose ChatGPT to Claude Desktop, Cursor, Craft Agent, or any MCP client. Two
transports are supported — **SSE is recommended** for persistent clients like
ZCode; stdio is kept as a compatibility/dev mode.

#### Recommended: SSE transport (one persistent server)

SSE is recommended for ZCode because one long-lived MCP server can serve many
client sessions without spawning a new MCP process or Chrome tab per session.
Start the SSE server once, then point any number of clients at it.

Start it (in a dedicated terminal — Chrome must already be running with an
authenticated session, i.e. `chatgpt-web2api` started first):

```bash
chatgpt-web2api-mcp --transport sse --port 8090
```

Then add the client config:

```json
{
  "mcpServers": {
    "chatgpt-web2api-sse": {
      "type": "sse",
      "url": "http://localhost:8090/sse"
    }
  }
}
```

Every client session that attaches to that URL shares the single MCP server
and its Chrome connection — there is no per-session process or tab growth.

#### Alternative: stdio transport (per-session, dev/debug)

Stdio spawns one MCP child process per client session, so it is not
recommended for many concurrent ZCode sessions. It is useful for single-client
setups, local development, and debugging (logs go straight to stderr):

```json
{
  "mcpServers": {
    "chatgpt": {
      "command": "chatgpt-web2api-mcp",
      "args": ["--cdp-port", "9222"]
    }
  }
}
```

**16 tools** give the agent full control:

| Tool | What it does |
|------|-------------|
| `chat_completion` | Send message, get response (multi-turn, streaming) |
| `list_models` | 17 live model slugs (GPT-5.5, 5.4 Thinking, etc.) |
| `list_projects` | ChatGPT projects with persistent memory |
| `create_project` | Create isolated workspace with dedicated memory |
| `delete_project` | Permanently remove a project (gated: `W2A_ENABLE_DESTRUCTIVE`) |
| `list_conversations` | Recent chats with pagination |
| `get_conversation` | Full message history |
| `archive_conversation` | Archive/unarchive (reversible) |
| `delete_conversation` | Delete permanently |
| `list_memories` | Facts ChatGPT remembers (41 found in testing) |
| `create_memory` | Tell ChatGPT to remember something |
| `delete_memory` | Remove a memory |
| `list_gpts` | Discover Custom GPTs |
| `chat_with_gpt` | Chat with a specific Custom GPT |
| `update_project_instructions` | Change project system prompt |
| `list_project_files` | Files in a project's knowledge base |

Every tool includes rich descriptions with domain knowledge, Pydantic-validated input, structured output schemas, and proper `ToolAnnotations` — agents understand *how* and *when* to use each one without prompting.

### 🔒 MCP Tool Access Control

The MCP server has **no authentication of its own** — any client that can reach it can call its tools. Since several tools mutate or delete data on your *real* ChatGPT account, tools are gated by risk tier so the dangerous ones are never exposed by accident:

| Tier | Tools | Exposed when |
|------|-------|--------------|
| **Safe** (always visible) | `chat_completion`, `chat_with_gpt`, `list_models`, `list_projects`, `list_conversations`, `get_conversation`, `list_memories`, `list_gpts`, `list_project_files` | Out of the box |
| **Write** (account mutation) | `create_project`, `update_project_instructions`, `create_memory`, `archive_conversation` | `W2A_ENABLE_WRITE=1` |
| **Destructive** (irreversible) | `delete_conversation`, `delete_memory` | `W2A_ENABLE_DESTRUCTIVE=1` |

```bash
# Default: safe reads + core chat only — nothing can mutate your account
chatgpt-web2api-mcp

# Enable project/memory/archive creation
W2A_ENABLE_WRITE=1 chatgpt-web2api-mcp

# Full surface (also exposes deletes)
W2A_ENABLE_WRITE=1 W2A_ENABLE_DESTRUCTIVE=1 chatgpt-web2api-mcp
```

Gated tools are **hidden** from `list_tools` *and* **refused at call time** — a client that calls a gated tool by name gets an error result rather than silent execution. Binding the SSE transport to a non-loopback address with no `api_keys` configured logs a prominent warning.

### 🔧 Three Interfaces, One Chrome Session

```bash
# Terminal 1: Start Chrome + API
chatgpt-web2api

# Terminal 2: MCP server (SSE recommended for ZCode; stdio also supported)
chatgpt-web2api-mcp --transport sse --port 8090
# or: chatgpt-web2api-mcp             # stdio (one MCP child per client)

# Terminal 3: Any OpenAI SDK, curl, or HTTP client
curl http://localhost:8080/v1/chat/completions ...
```

## What Makes This Different

| | ChatGPT-Web2API | chat2api | ChatGPTReversed | Official API |
|---|---|---|---|---|
| **Anti-bot handling** | ✅ Automatic (CDP) | ❌ Manual (PoW/Turnstile) | ❌ Manual | N/A |
| **Token extraction** | ❌ None needed | ✅ Required | ✅ Required | N/A |
| **Project memory** | ✅ Full CRUD | ❌ | ❌ | ❌ |
| **MCP server** | ✅ 16 tools | ❌ | ❌ | ❌ |
| **ChatGPT memories** | ✅ List/create/delete | ❌ | ❌ | ❌ |
| **Custom GPTs** | ✅ Chat with any GPT | ❌ | ❌ | ❌ |
| **OpenAI SDK compat** | ✅ Drop-in | ✅ | ❌ | ✅ (native) |
| **Streaming** | ✅ SSE | ✅ | ❌ | ✅ |
| **Multi-turn** | ✅ Auto-continue | ✅ | ❌ | ✅ |
| **Installation** | `pip install` | Docker | npm | `pip install openai` |
| **Cost** | Free (uses your subscription) | Free | Free | Pay-per-token |

**Key insight**: Other proxies fight the anti-bot system. This one *uses the browser as the solution* — Chrome handles Turnstile, PoW, and session management automatically.

## MCP Tools in Action

An AI agent connected via MCP can:

```python
# Create an isolated project with dedicated memory
create_project(name="Python Async Specialist", memory_scope="project_v2")

# Chat within the project (persistent memory across conversations)
chat_completion(message="What are Python coroutines?", project_id="g-p-abc123")

# Manage memories
list_memories()  # → 41 facts ChatGPT remembers
create_memory(content="Always use type hints in Python code")
delete_memory(memory_id="abc-123")

# List and manage conversations
list_conversations(limit=10)
archive_conversation(conversation_id="xyz", archive=True)

# Chat with a Custom GPT
chat_with_gpt(gpt_id="g-hkJGhxxx", message="Analyze this data")
```

## Configuration

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 8080 | API server port |
| `--cdp-port` | 9222 | Chrome debugging port |
| `--headless` | false | Run Chrome headless (may trigger detection) |
| `--log-level` | INFO | DEBUG, INFO, WARNING, ERROR |

### Config File (`config.json`)

```json
{
  "port": 8080,
  "host": "127.0.0.1",
  "cdp_port": 9222,
  "headless": false,
  "default_model": "auto",
  "default_project_id": null,
  "api_keys": [],
  "request_timeout": 120
}
```

See [`config.example.json`](config.example.json) for all available keys.

### Environment Variables

```bash
W2A_PORT=8080 W2A_CDP_PORT=9222 chatgpt-web2api
```

See [`.env.example`](.env.example) for all available variables.

## Tested Models

Live models from a ChatGPT Plus account (June 2025):

| Slug | Type | Best For |
|------|------|----------|
| `auto` | Reasoning | General use (default) |
| `gpt-5-5` | Reasoning, 34K context | Complex analysis |
| `gpt-5-4-thinking` | Extended reasoning | Step-by-step logic |
| `gpt-5-3-mini` | Fast, 34K context | Simple tasks, speed |
| `gpt-5-mini` | Fast, 8K context | Quick answers |

Use `list_models` to get the current catalog.

## Performance

| Operation | Time |
|-----------|------|
| Simple question ("2+2?") | 7s |
| Complex reasoning | 15–36s |
| Multi-turn follow-up | 3–6s faster (auto-continue) |
| Model listing | <1s |
| Memory listing | <1s |

## Requirements

- Python 3.11+
- Chrome or Chromium installed
- ChatGPT Plus subscription

## Project Structure

```
src/chatgpt_web2api/
├── __main__.py          CLI entrypoint (chatgpt-web2api)
├── config.py            Configuration from file/env/CLI
├── chrome.py            Chrome subprocess lifecycle
├── cdp_driver.py        CDP primitives (24 methods)
├── api_server.py        OpenAI-compatible HTTP server
├── mcp_server.py        MCP server (16 tools, resources, prompts)
└── service.py           Orchestrator: Chrome → CDP → API/MCP
```

## Testing

The suite has two tiers:

**Unit tests (default, CI)** — fast, no browser, fully mocked. Run with `pytest`.
These cover structure, business logic, MCP protocol-level behavior, and access gating.

```bash
pytest -v                       # all unit tests (CI runs: pytest -m "not e2e")
```

**End-to-end tests (opt-in)** — drive a *real* ChatGPT account via Chrome CDP.
These are the only tests that exercise the driver↔ChatGPT boundary, which is
exactly where latent bugs hide (the E2E suite has already found four). They
mutate the account, so they are **deselected by default** and never run in CI.

```bash
# 1. Launch Chrome with CDP (reuses your logged-in profile)
#    "chrome.exe" --remote-debugging-port=9222 \
#        --user-data-dir=~/.chatgpt-web2api/chrome-profile https://chatgpt.com/

# 2. Run the E2E suite
W2A_E2E_RUN=1 pytest -m e2e -v
```

E2E safety model:
- **Snapshot-before / diff-after** for every destructive op — no test ever
  deletes state it didn't create in the same session.
- A session registry + **guaranteed-cleanup finalizer** removes anything a test
  created, even on crash.
- **Pacing** (`W2A_E2E_PACE`, default 12s) between tests to avoid ChatGPT's
  "Too many requests" rate limit.
- Reads are zero-risk; chat/memory operations create throwaway state that's
  cleaned up; `create_project`/`update_project_instructions` are `xfail` (broken
  against the current live API — see CHANGELOG).

If not logged in, the suite opens a headed window and waits up to 10 minutes
for you to log in.

## Documentation

- [Protocol Reference](docs/protocol-reference.md) — captured ChatGPT web API endpoints
- [Deployment Guide](docs/deployment.md) — Docker, cookie injection, multi-instance
- [Contributing](CONTRIBUTING.md) — how to contribute
- [Changelog](CHANGELOG.md) — version history

## Limitations

- **Single browser session** — one Chrome profile = one ChatGPT account (scale with nginx round-robin)
- **No headless** — headless Chrome triggers ChatGPT's bot detection; use VNC on servers
- **Cookie expiry** — auth cookies expire ~2 weeks; re-login needed
- **Serial requests** — one chat at a time through the browser (concurrent reads are fine)
- **Memory writes** — ChatGPT's `/backend-api/memories` is read-only; creating memories works via chat interface
- **No image input** — text only (CDP file upload not yet implemented)

## Roadmap

- [ ] Image/file upload to conversations via CDP drag-and-drop
- [ ] Headless mode with anti-detection patches
- [ ] Concurrent chat pooling across multiple Chrome instances
- [ ] Web search mode (trigger ChatGPT's built-in search)
- [ ] DALL-E image generation via ChatGPT
- [ ] Canvas/code execution support
- [ ] Retry logic with exponential backoff for Chrome failures

## License

[MIT](LICENSE) © Elephant Rock Lab

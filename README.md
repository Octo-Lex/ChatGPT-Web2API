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

### 🤖 MCP Server (15 Tools for AI Agents)

Expose ChatGPT to Claude Desktop, Cursor, Craft Agent, or any MCP client:

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

**15 tools** give the agent full control:

| Tool | What it does |
|------|-------------|
| `chat_completion` | Send message, get response (multi-turn, streaming) |
| `list_models` | 17 live model slugs (GPT-5.5, 5.4 Thinking, etc.) |
| `list_projects` | ChatGPT projects with persistent memory |
| `create_project` | Create isolated workspace with dedicated memory |
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

### 🔧 Three Interfaces, One Chrome Session

```bash
# Terminal 1: Start Chrome + API
chatgpt-web2api

# Terminal 2: MCP server (for AI agents)
chatgpt-web2api-mcp

# Terminal 3: Any OpenAI SDK, curl, or HTTP client
curl http://localhost:8080/v1/chat/completions ...
```

## What Makes This Different

| | ChatGPT-Web2API | chat2api | ChatGPTReversed | Official API |
|---|---|---|---|---|
| **Anti-bot handling** | ✅ Automatic (CDP) | ❌ Manual (PoW/Turnstile) | ❌ Manual | N/A |
| **Token extraction** | ❌ None needed | ✅ Required | ✅ Required | N/A |
| **Project memory** | ✅ Full CRUD | ❌ | ❌ | ❌ |
| **MCP server** | ✅ 15 tools | ❌ | ❌ | ❌ |
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
├── mcp_server.py        MCP server (15 tools, resources, prompts)
└── service.py           Orchestrator: Chrome → CDP → API/MCP
```

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

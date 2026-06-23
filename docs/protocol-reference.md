# ChatGPT Web Protocol Reference — Autonomous Discovery

Autonomously captured: 2026-06-02 via CDP Runtime.evaluate + Bearer token auth
Account: ChatGPT Plus (user@example.com)
Success: 21/28 endpoints

## Authentication

### Access Token
```
GET /api/auth/session  →  {accessToken: "eyJ...", user: {...}, expires: "..."}
```
- JWT token, ~1983 chars
- All `/backend-api/` calls need: `Authorization: Bearer <token>`
- Token has expiry; refresh via same endpoint
- `credentials: 'include'` needed for cookies (Cloudflare, session)

### 403 "Unusual activity" Block
- POST endpoints (`/f/conversation`) return 403 when called from `Runtime.evaluate`
- Requires valid sentinel token (Turnstile + PoW + so challenge)
- Must go through the full sentinel flow first: prepare → solve PoW → solve Turnstile → finalize
- This is the main blocker for autonomous message sending

## Model Catalog

| Slug | Title | Max Tokens | Reasoning | Tools |
|------|-------|-----------|-----------|-------|
| `gpt-5-5` | GPT-5.5 | 34,834 | auto | tools, tools2, dalle_3, search, canvas |
| `gpt-5-3` | GPT-5.3 | 34,834 | auto | tools, tools2, dalle_3, search, canvas |
| `gpt-5-2` | GPT-5.2 | 25,384 | auto | tools, tools2, dalle_3, search, canvas |
| `gpt-5-1` | GPT-5.1 | 17,384 | auto | tools, tools2, dalle_3, search, canvas |
| `gpt-5` | GPT-5 | 16,384 | auto | tools, tools2, dalle_3, search, canvas |
| `gpt-5-3-mini` | GPT-5.3 Mini | 34,834 | none | tools, tools2, dalle_3, search, canvas |
| `gpt-5-mini` | GPT-5-mini | 8,191 | none | tools, tools2, dalle_3, search, canvas |
| `auto` | Auto | 16,384 | auto | tools, tools2, dalle_3, search, canvas |

Default: `auto`, Latest version: `5.5`

## Endpoints (Captured + Working)

### Auth & User
| Endpoint | Status | Response Size | Notes |
|----------|--------|--------------|-------|
| `GET /api/auth/session` | 200 | ~2KB | Access token + user info |
| `GET /backend-api/me` | 200 | ~1KB | User profile (id, name, email, country) |
| `GET /backend-api/accounts/check/v4-2023-04-27` | 200 | ~8KB | Account status, plan_type=plus |
| `GET /backend-api/settings/user` | 200 | ~6KB | User settings, announcements, preferences |
| `GET /backend-api/user_segments` | 200 | ~35B | Feature flags |
| `GET /backend-api/user_system_messages` | 200 | — | System messages |
| `GET /backend-api/user_granular_consent` | 200 | — | Consent state |

### Models
| Endpoint | Status | Response Size | Notes |
|----------|--------|--------------|-------|
| `GET /backend-api/models` | 200 | ~46KB | Full model catalog |
| `GET /backend-api/models/gpts` | 200 | ~336B | GPT-specific models |

### Conversations
| Endpoint | Status | Response Size | Notes |
|----------|--------|--------------|-------|
| `GET /backend-api/conversations?offset=0&limit=28&order=updated` | 200 | ~4KB | Conversation list |
| `GET /backend-api/conversation/{id}` | 200 | ~9KB | Full conversation with message mapping |
| `POST /backend-api/conversation/init` | 200 | ~654B | Returns limits, default model |
| `GET /backend-api/conversation/{id}/stream_status` | 200 | — | Stream status |
| `GET /backend-api/pins` | 200 | ~2B | Pinned conversations |
| `GET /backend-api/calpico/chatgpt/rooms/summary` | 200 | ~57B | Rooms summary |

### Projects (Gizmos)
| Endpoint | Status | Response Size | Notes |
|----------|--------|--------------|-------|
| `GET /backend-api/gizmos/snorlax/sidebar` | 200 | ~162KB | All projects + GPTs |
| `GET /backend-api/gizmos/{id}` | 200 | varies | Full project detail |

#### Project Structure (gizmo detail)
```json
{
  "gizmo": {
    "id": "g-p-6a1e3f6804588191902b398f5afcd6a7",
    "short_url": "g-p-6a1e3f6804588191902b398f5afcd6a7-c-project-shared",
    "gizmo_type": "snorlax",
    "display": {"name": "C-project-shared", "description": ""},
    "memory_scope": "global",         // "global" = shared, "project_v2" = dedicated
    "memory_enabled": true,
    "instructions": "",
    "context_stuffing_budget": 49152,
    "voice": {"id": "ember"},
    "tools": [],
    "files": [],
    "current_user_permission": {"can_delete": true, "can_write": true, ...}
  }
}
```

**Memory scopes captured:**
- `global` — shared memory (uses global ChatGPT memory)
- `project_v2` — dedicated memory (project-specific)
- `global_enabled` — on conversation level (inherited from project)

### Images & Tasks
| Endpoint | Status | Response Size | Notes |
|----------|--------|--------------|-------|
| `GET /backend-api/images/bootstrap` | 200 | ~288B | Image count, thumbnail |
| `GET /backend-api/tasks` | 200 | ~62KB | Background tasks (image gen, research) |

#### Task Structure
```json
{
  "task_id": "imagegen_6a1e7295...",
  "title": "Close-up of a black insect on gravel",
  "status": "completed",
  "conversation_id": "...",
  "created_at": "2026-06-02T06:05:09+00:00"
}
```

### Sentinel Flow (Live-Captured + Solved)

### Step 1: Prepare
```
POST /backend-api/sentinel/chat-requirements/prepare
Headers: Authorization: Bearer <token>
Body: {}
Response: {
  persona: "chatgpt-paid",  // "chatgpt-noauth" when unauthenticated
  prepare_token: "gAAAAABq...",
  turnstile: {required: true, dx: "<29KB encrypted blob>"},
  proofofwork: {required: true, seed: "0.559...", difficulty: "0689f6"},
  so: {required: true, collector_dx: "<17KB>", snapshot_dx: "..."}
}
```

### Step 2: Solve PoW (SOLVED!)
```javascript
// Algorithm: SHA-256(seed + counter) where first 3 bytes < parseInt(difficulty, 16)
const target = parseInt(difficulty, 16);
for (let i = 0; i < 10000000; i++) {
    const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(seed + i));
    const bytes = new Uint8Array(hash);
    const first3 = (bytes[0] << 16) | (bytes[1] << 8) | bytes[2];
    if (first3 < target) return i.toString();  // Usually solves in <100 iterations
}
```

### Step 3: Solve Turnstile (Cloudflare CAPTCHA)
- Required, 29KB encrypted `dx` blob
- Must be solved in-browser via Cloudflare's Turnstile widget
- Super Browser v2.0 has auto-solve capability for invisible Turnstile

### Step 4: Solve so challenge
- Required, encrypted `collector_dx` (17KB) + `snapshot_dx`
- Unknown algorithm — likely Kasada-based
- Super Browser v2.0 has detection but not solving (deferred to v2.1)

### Step 5: Finalize
```
POST /backend-api/sentinel/chat-requirements/finalize
Body: {
  prepare_token: "<from step 1>",
  turnstile: "<solved token>",
  proofofwork: {seed, difficulty, answer: "<from step 2>"},
  so: {collector_dx: "<solved>", snapshot_dx: "<solved>"}
}
```

### Full Sentinel Flow Captured from Real Session
The ChatGPT frontend automatically solves all 3 challenges when sending a message.
The conversation/prepare endpoint returns a conduit_token (JWT) used for the conversation.

## PoW Solver — VERIFIED WORKING

Seed: `0.559779845730002`, Difficulty: `0689f6` → Answer: `30` (solved in 31 iterations)

The PoW is trivially solvable — typically requires <100 hash iterations.

### Connectors
| Endpoint | Status | Notes |
|----------|--------|-------|
| `GET /backend-api/aip/connectors/list_accessible` | 404 | May need different params |
| `GET /backend-api/system_hints?mode=basic` | 200 | System capabilities (search) |
| `GET /backend-api/system_hints?mode=connectors` | 200 | Deep Research, GitHub, OpenAI Platform |
| `GET /backend-api/apps/sources_dropdown` | 200 | ~291KB! Full GPT marketplace catalog |

### Chat (BLOCKED by sentinel)
| Endpoint | Status | Notes |
|----------|--------|-------|
| `POST /backend-api/f/conversation` | **403** | Needs valid sentinel token |
| `POST /backend-api/f/conversation/prepare` | ? | Predictive pre-flight |

## Conversation Detail Structure

Captured from real conversation: "Model Inquiry" (gpt-5-5-thinking)

### Message Types in Response

| Content Type | Role | Description |
|-------------|------|-------------|
| `text` | user | User's message text |
| `text` | system | System prompts (hidden, empty parts) |
| `user_editable_context` | user | Editable context (usually empty) |
| `model_editable_context` | assistant | Model's editable context output |
| `thoughts` | assistant | Thinking model's chain-of-thought |
| `reasoning_recap` | assistant | Summary of reasoning process |
| `text` | assistant | Final text response |

### Thinking Model Message Sequence
```
1. [system] text: ""              # System prompt
2. [system] text: ""              # Additional context
3. [system] text: ""              # More context
4. [user]   user_editable_context  # User context
5. [system] text: ""              # Pre-injection
6. [system] text: ""              # Post-injection
7. [user]   text: "Hello..."       # USER MESSAGE
8. [system] text: ""              # Model=thinking
9. [assistant] model_editable_context  # Context injection
10. [system] text: ""             # Separator
11. [assistant] thoughts: []       # THINKING (empty = not captured)
12. [assistant] reasoning_recap: [] # REASONING SUMMARY
13. [assistant] text: "GPT-5.5 Thinking." # FINAL ANSWER
```

### Full Conversation JSON
```json
{
  "title": "Model Inquiry",
  "conversation_id": "6a1ed734-...",
  "gizmo_id": null,
  "default_model_slug": "gpt-5-5-thinking",
  "memory_scope": "global_enabled",
  "current_node": "<uuid>",
  "mapping": {
    "<uuid>": {
      "id": "<uuid>",
      "message": {
        "id": "<uuid>",
        "author": {"role": "user|assistant|system"},
        "content": {
          "content_type": "text|thoughts|reasoning_recap|model_editable_context",
          "parts": ["..."]
        },
        "status": "finished_successfully",
        "metadata": {"model_slug": "gpt-5-5-thinking"}
      },
      "parent": "<parent_uuid>",
      "children": ["<child_uuid>"]
    }
  }
}
```

## User Info Captured
```json
{
  "id": "ua-<opaque-user-id>",
  "name": "User",
  "email": "user@example.com",
  "country": "<country_code>",
  "region": "<region_name>",
  "plan_type": "<free|plus|team|enterprise>",
  "subscription_plan": "<subscription_plan_id>"
}
```

## Key Findings

1. **Auth**: JWT Bearer token from `/api/auth/session` (~1983 chars) — required for all `/backend-api/` calls
2. **PoW SOLVED**: SHA-256 hash comparison — seed + counter, first 3 bytes < difficulty. Trivially solvable (<100 iterations)
3. **Sentinel gatekeeper**: 3 challenges (Turnstile + PoW + so) — PoW solved, Turnstile/so still need browser-based solving
4. **Projects ARE gizmos**: `gizmo_type: "snorlax"`, `memory_scope` differentiates shared (`global`) vs dedicated (`project_v2`)
5. **Message mapping is a tree**: each node has parent + children, supporting branching conversations
6. **Thinking model content types**: `thoughts`, `reasoning_recap`, `model_editable_context`, `text`
7. **Model slug with thinking**: `gpt-5-5-thinking` (derived from base model `gpt-5-5` + thinking mode)
8. **Message sending works**: Via JS click with full MouseEvent sequence on send button
9. **BrowserFetch pattern**: Dedicated `about:blank` scratch frame inherits browser auth (persona=`chatgpt-paid`)
10. **Conversation/prepare**: Returns a `conduit_token` (JWT) for the session

## Unblocked Path Forward

The main remaining blocker is the **Turnstile + so challenges**. Options:
1. **Intercept from real session**: The page solves these automatically — intercept the finalize request
2. **Super Browser v2.0 Turnstile solver**: Has auto-solve for invisible Turnstile
3. **Skip sentinel**: Use the page's own message sending (type + click) which handles sentinel internally

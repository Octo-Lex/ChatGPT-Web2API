# ChatGPT-Web2API — Deployment Guide

Three ways to share this with others, depending on the audience.

---

## Option 1: pip install (Simplest — for developers)

Anyone with Python and Chrome can run it in 3 steps:

```bash
# 1. Install
pip install git+https://github.com/your-org/ChatGPT-Web2API.git

# 2. Start — launches Chrome, opens chatgpt.com
chatgpt-web2api

# 3. First time: log into ChatGPT in the Chrome window that opens.
#    The proxy detects login automatically and starts serving.
```

The Chrome profile is saved at `~/.chatgpt-web2api/chrome-profile/`. Subsequent starts skip login.

### Configuration

Create `~/.chatgpt-web2api/config.json`:

```json
{
  "port": 8080,
  "cdp_port": 9222,
  "api_keys": ["sk-my-secret-key"],
  "default_model": "auto"
}
```

Or use environment variables:

```bash
W2A_PORT=9090 W2A_API_KEYS=sk-key1,sk-key2 chatgpt-web2api
```

---

## Option 2: Docker (For servers / headless)

Requires exporting cookies from an already-logged-in browser session.

### Step 1: Export cookies from your browser

Use a browser extension like [EditThisCookie](https://editthiscookie.com/) or [Cookie-Editor](https://cookie-editor.com/):

1. Open `chatgpt.com` while logged in
2. Export all cookies for `chatgpt.com` as JSON
3. Save as `cookies.json`

The file should look like:

```json
[
  {
    "name": "__Secure-next-auth.session-token",
    "value": "...",
    "domain": ".chatgpt.com",
    "path": "/",
    "secure": true,
    "httpOnly": true
  },
  ...
]
```

### Step 2: Run with Docker

```bash
# Build
docker build -t chatgpt-web2api .

# Run (mount cookies + persistent profile)
docker run -d \
  --name chatgpt-proxy \
  -p 8080:8080 \
  -v ./cookies.json:/data/cookies/cookies.json:ro \
  -v chatgpt-profile:/data/chrome-profile \
  chatgpt-web2api
```

### Step 3: Use it

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Hello"}]}'
```

### Cookie refresh

Cookies expire. When auth fails:

1. Re-export fresh cookies from your browser
2. Replace `cookies.json`
3. Restart the container: `docker restart chatgpt-proxy`

---

## Option 3: Remote server (For teams)

Run the proxy on a server, let others connect to it.

```bash
# On the server (with API key protection)
chatgpt-web2api --host 0.0.0.0 --port 8080

# Or with config
cat > config.json << 'EOF'
{
  "port": 8080,
  "host": "0.0.0.0",
  "api_keys": ["sk-team-key-1", "sk-team-key-2"],
  "default_model": "auto"
}
EOF
chatgpt-web2api --config config.json
```

Others connect:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://your-server:8080/v1",
    api_key="sk-team-key-1"
)

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "Hello"}]
)
```

**Important**: Only one request at a time. The browser is single-threaded. For team use, queue requests or run multiple instances on different ports.

---

## Cookie Export Guide (Detailed)

### Chrome — EditThisCookie

1. Install [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg)
2. Go to `chatgpt.com` while logged in
3. Click the cookie icon in toolbar
4. Click "Export" → copies JSON to clipboard
5. Save as `cookies.json`

### Firefox — Cookie-Editor

1. Install [Cookie-Editor](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/)
2. Go to `chatgpt.com` while logged in
3. Click the cookie icon → "Export" → "Export as JSON"
4. Save as `cookies.json`

### Manual (any browser) — DevTools

1. Open `chatgpt.com`, press F12 → Console
2. Run: `document.cookie`
3. Parse the cookie string into JSON format

---

## Multiple Instances (Scale concurrency)

Each Chrome instance handles one request at a time. Run multiple for throughput:

```bash
# Instance 1
chatgpt-web2api --port 8081 --cdp-port 9222

# Instance 2
chatgpt-web2api --port 8082 --cdp-port 9223 --user-data-dir ~/.chatgpt-web2api/chrome-profile-2

# Instance 3
chatgpt-web2api --port 8083 --cdp-port 9224 --user-data-dir ~/.chatgpt-web2api/chrome-profile-3
```

Put a reverse proxy (nginx, Caddy) in front with round-robin:

```nginx
upstream chatgpt {
    server 127.0.0.1:8081;
    server 127.0.0.1:8082;
    server 127.0.0.1:8083;
}

server {
    listen 8080;
    location / {
        proxy_pass http://chatgpt;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }
}
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "No access token" | Log into ChatGPT in the Chrome window that opens |
| "Chrome CDP did not respond" | Chrome isn't running. Check `chrome_path` in config |
| "Timed out waiting for assistant" | Page may be stuck. Restart the proxy |
| Cookies not working | Re-export fresh cookies. They expire every ~2 weeks |
| Headless fails | Anti-bot detection blocks headless. Use cookie injection + headed mode on a VNC/display. The Dockerfile ships `W2A_HEADLESS=false` for this reason; set `W2A_HEADLESS=true` only if you accept the anti-bot risk |

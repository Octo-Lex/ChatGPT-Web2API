# OS-Level Supervision Guide

> **Audience:** always-on / server deployments outside ZCode. ZCode users should
> use the `ensure` hook (see [deployment.md](deployment.md) and the
> [roadmap Phase 3](ROADMAP.md)) — this guide is for operators who want the
> proxy running continuously under an OS supervisor.

This guide shows how to run `chatgpt-web2api` under a native OS supervisor so it
survives reboots and restarts on failure. It is **documentation only**: no
supervisor scripts are installed by the package, no daemonization code is added,
and no package entrypoints change. The snippets below are copy-paste templates
for the operator to adapt and install themselves.

---

## The recommended reconcile command

Every supervisor on every OS runs the same point-in-time reconcile command:

```bash
chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222
```

`ensure` is **not a daemon**. It reconciles REST + SSE to a healthy state and
exits (exit code `0` ready, `1` reconcile failure, `2` auth/login needed). The
supervisor's job is to invoke it on a schedule or on failure so the stack is
reconciled without a long-lived Python watchdog.

> ⚠️ **Do not use** `python -m chatgpt_web2api.ensure`. The `ensure` submodule
> has no `__main__` block; only the console-script subcommand
> (`chatgpt-web2api ensure`) runs the reconcile. The `-m` form exits silently
> with no action. This is a standing operational note — see
> [deployment.md](deployment.md).

Two supervisor styles are supported:

| Style | What it supervises | Restarts | Best for |
|-------|--------------------|----------|----------|
| **`ensure` on a timer** | nothing long-lived; reconcile on schedule | re-run `ensure` every N minutes | ZCode-adjacent, lightweight |
| **`start` as a service** | the long-lived REST + MCP process | restart the process on crash | classic always-on server |

The **timer** style mirrors the ZCode hook model and needs no long-lived Python
process. The **service** style runs `chatgpt-web2api start` directly and lets
the supervisor own restart policy. Pick one; do not run both against the same
ports/profile.

---

## Health check

All supervisors should gate restart/reconcile decisions on the `/health`
endpoint rather than on a bare process-exists check:

```bash
curl -sf http://localhost:8080/health >/dev/null
```

`/health` returns `status` of `starting` / `healthy` / `degraded` / `broken`,
plus `open_breakers` and per-breaker cooldowns. Treat:

- `healthy` → up, no action
- `starting` → cold bootstrap; give it room (do not restart)
- `degraded` → Chrome alive, driver disconnected (often a transient CDP
  reconnect). The `ensure` degraded policy already waits 20s before restarting
  REST; a supervisor should not second-guess this faster.
- `broken` → Chrome down; `ensure` will restart REST (which relaunches Chrome).

See [ROADMAP.md Phase 3](ROADMAP.md) for the full degraded-REST policy and the
reason a naive `degraded → restart` flap is destructive.

---

## Linux — systemd

### Option A: `ensure` on a timer (recommended for ZCode-adjacent use)

Two units. The service runs `ensure` once; the timer triggers it.

`~/.config/systemd/user/chatgpt-web2api-ensure.service`:

```ini
[Unit]
Description=ChatGPT-Web2API point-in-time reconcile
After=network-online.target

[Service]
Type=oneshot
# Adapt the path if chatgpt-web2api is installed in a venv:
ExecStart=%h/.local/bin/chatgpt-web2api ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222
# Exit 2 = auth/login needed; that is not a service failure, do not treat as error
SuccessExitStatus=0 2
Environment=W2A_LOG_LEVEL=INFO
```

`~/.config/systemd/user/chatgpt-web2api-ensure.timer`:

```ini
[Unit]
Description=Run ChatGPT-Web2API reconcile every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=chatgpt-web2api-ensure.service

[Install]
WantedBy=timers.target
```

Enable (user units — no root needed if Chrome runs in your session):

```bash
systemctl --user daemon-reload
systemctl --user enable --now chatgpt-web2api-ensure.timer
```

### Option B: long-lived `start` service (classic always-on)

`~/.config/systemd/user/chatgpt-web2api.service`:

```ini
[Unit]
Description=ChatGPT-Web2API REST + MCP server
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/chatgpt-web2api start --port 8080 --cdp-port 9222
Restart=on-failure
RestartSec=10
# Give the process room on a cold Chrome boot before the supervisor kills it:
TimeoutStartSec=120
Environment=W2A_LOG_LEVEL=INFO
# Optional: Environment=W2A_API_KEYS=sk-...
# Optional: Environment=W2A_HEADLESS=false   # headed mode needs a display/Xvfb

[Install]
WantedBy=default.target
```

> **Display note:** Chrome runs headed by default. On a headless server you
> need either Xvfb (`W2A_HEADLESS=false` + a virtual display) or you accept
> the anti-bot risk of `W2A_HEADLESS=true`. See the Docker section of
> [deployment.md](deployment.md) for the headed-in-container pattern.

```bash
systemctl --user daemon-reload
systemctl --user enable --now chatgpt-web2api.service
journalctl --user -u chatgpt-web2api.service -f   # logs
```

For a system-wide service (root-owned Chrome), place the units in
`/etc/systemd/system/` and drop `--user`. Headed Chrome under a system service
needs a logged-in user session; the user-unit form is usually simpler.

---

## macOS — launchd

### Option A: `ensure` on an interval

`~/Library/LaunchAgents/com.chatgpt-web2api.ensure.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.chatgpt-web2api.ensure</string>

    <key>ProgramArguments</key>
    <array>
        <!-- adapt path: /opt/homebrew/bin or your venv bin -->
        <string>/usr/local/bin/chatgpt-web2api</string>
        <string>ensure</string>
        <string>--rest-port</string><string>8080</string>
        <string>--mcp-sse-port</string><string>8090</string>
        <string>--cdp-port</string><string>9222</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>W2A_LOG_LEVEL</key>
        <string>INFO</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>300</integer>  <!-- 5 minutes -->

    <key>StandardOutPath</key>
    <string>/tmp/chatgpt-web2api-ensure.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/chatgpt-web2api-ensure.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.chatgpt-web2api.ensure.plist
launchctl start com.chatgpt-web2api.ensure   # trigger once now
tail -f /tmp/chatgpt-web2api-ensure.log
```

### Option B: long-lived `start`

Same structure, but `ProgramArguments` runs `start --port 8080 --cdp-port 9222`,
set `KeepAlive` instead of `StartInterval`, and drop `RunAtLoad`/`StartInterval`:

```xml
<key>KeepAlive</key>
<dict>
    <key>SuccessfulExit</key>
    <false/>
</dict>
```

---

## Windows — Task Scheduler or NSSM

### Option A: Task Scheduler running `ensure` (no admin needed)

Create a task that runs `ensure` on a 5-minute schedule and at logon. From an
elevated PowerShell (or use `taskschd.msc`):

```powershell
# Locate the console script (usually in %LOCALAPPDATA%\Programs\Python\...Scripts
# or your venv's Scripts folder). Example:
$ensure = "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts\chatgpt-web2api.exe"

$action    = New-ScheduledTaskAction -Execute $ensure `
              -Argument "ensure --rest-port 8080 --mcp-sse-port 8090 --cdp-port 9222"
$trigger   = New-ScheduledTaskTrigger -AtLogOn
$schedule  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
              -RepetitionInterval (New-TimeSpan -Minutes 5) `
              -RepetitionDuration (New-TimeSpan -Days 365)
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd

Register-ScheduledTask -TaskName "ChatGPT-Web2API-Ensure" `
    -Action $action -Trigger $trigger,$schedule -Settings $settings `
    -Environment @{ W2A_LOG_LEVEL = "INFO" }
```

`-AtLogOn` boots the reconcile at sign-in; the 5-minute repetition keeps it
healthy. Exit code 2 (auth needed) is not an error — Task Scheduler will still
report success because the process exited 0/2 cleanly; monitor the log instead.

### Option B: NSSM wrapping `start` (long-lived service)

[NSSM](https://nssm.cc/) wraps any executable as a Windows service with restart
policy. Install once, then point it at `chatgpt-web2api.exe`:

```powershell
nssm install ChatGPT-Web2API "C:\path\to\chatgpt-web2api.exe" "start --port 8080 --cdp-port 9222"
nssm set      ChatGPT-Web2API AppEnvironmentExtra "W2A_LOG_LEVEL=INFO"
nssm set      ChatGPT-Web2API AppStdout "C:\Logs\chatgpt-web2api.out.log"
nssm set      ChatGPT-Web2API AppStderr "C:\Logs\chatgpt-web2api.err.log"
nssm set      ChatGPT-Web2API AppRotateFiles 1
nssm set      ChatGPT-Web2API AppRotateBytes 10485760
nssm set      ChatGPT-Web2API AppThrottle 30000   # grace before restart-on-failure
nssm start    ChatGPT-Web2API
```

> **Session/Desktop note:** ChatGPT-Web2API drives a real Chrome window. A
> Windows service runs in Session 0 and cannot show a visible Chrome UI, which
> matters for first-time login and for some anti-bot heuristics. For interactive
> use, prefer **Option A (Task Scheduler at logon)** so Chrome runs in your
> desktop session. Use NSSM only on a dedicated machine where a headed Session-0
> Chrome is acceptable, or where you have already exported cookies (see
> [deployment.md](deployment.md) Option 2).

---

## Restart policy

Across all supervisors, the policy that matches the project's own resilience
design is:

```text
process crash / non-zero exit    → restart after a short backoff (RestartSec=10 / AppThrottle=30s)
status=healthy                   → no action
status=starting (cold boot)      → wait, do NOT restart (Chrome is still booting)
status=degraded                  → let ensure's own 20s-poll-then-restart policy run; do not race it
status=broken (Chrome down)      → ensure restarts REST, which relaunches Chrome
exit code 2 (auth needed)        → do NOT restart-loop; surface for human login (recover_auth)
```

The supervisor should restart on **process exit**, not on `/health=degraded`.
The `ensure` command already encodes the degraded-REST and breaker-cooldown
policies; a supervisor that also restarts on `degraded` will fight `ensure` and
cause destructive browser flapping (visible Chrome window flash, lost in-flight
pages). See [ROADMAP.md Phase 3](ROADMAP.md) for why.

---

## Logs

`chatgpt-web2api` logs to **stderr** at the configured level. Capture it via
the supervisor's stdout/stderr redirection (shown in each section above).

Control verbosity with the `W2A_LOG_LEVEL` environment variable
(`DEBUG` / `INFO` / `WARNING` / `ERROR`; default `INFO`):

```text
Environment=W2A_LOG_LEVEL=DEBUG      # systemd
W2A_LOG_LEVEL = "DEBUG"              # Task Scheduler -Environment
<key>W2A_LOG_LEVEL</key><string>DEBUG</string>   # launchd
nssm set ... AppEnvironmentExtra "W2A_LOG_LEVEL=DEBUG"   # NSSM
```

There is no built-in log file or rotation — the supervisor owns that.

---

## Environment variables (reference)

The commonly-supervised knobs (full set in [`config.py`](../src/chatgpt_web2api/config.py)):

| Variable | Default | Purpose |
|----------|---------|---------|
| `W2A_PORT` | `8080` | REST API port |
| `W2A_HOST` | `127.0.0.1` | REST bind host (use `0.0.0.0` for remote; pair with `W2A_API_KEYS`) |
| `W2A_CDP_PORT` | `9222` | Chrome DevTools Protocol port |
| `W2A_API_KEYS` | — | Comma-separated API keys (required if binding non-loopback) |
| `W2A_DEFAULT_MODEL` | `auto` | Default model slug |
| `W2A_HEADLESS` | `false` | Run Chrome headless (anti-bot risk; see deployment.md) |
| `W2A_LOG_LEVEL` | `INFO` | Log verbosity |
| `W2A_TAB_MODE` | `owned` | Tab acquisition: `owned` (default) or `adopt` |
| `W2A_ENSURE_DEGRADED_POLL_INTERVAL_S` | `2` | `ensure`-only: degraded poll cadence |
| `W2A_ENSURE_DEGRADED_POLL_BUDGET_S` | `20` | `ensure`-only: how long to wait before restarting a degraded REST |
| `W2A_ENSURE_BREAKER_COOLDOWN_GRACE_S` | `5` | `ensure`-only: grace over breaker cooldown before recover/restart |

The `W2A_ENSURE_*` tunables affect **only** the `ensure` command, never the
long-lived `start` process. `W2A_BREAKER_*` threshold/window/cooldown keys do
**not** exist — breaker thresholds stay hardcoded in `breakers.py` until
production data shows they need tuning (deferred follow-up E,
[ROADMAP.md Phase 4](ROADMAP.md)).

---

## What this guide deliberately does NOT do

- **No supervisor scripts installed by the package.** All snippets are
  operator-installed templates.
- **No daemonization code, no built-in watchdog loop.** `ensure` is point-in-time
  by design; continuous supervision belongs to the OS.
- **No package entrypoint changes.** The console scripts (`chatgpt-web2api`,
  `chatgpt-web2api-mcp`) are unchanged.
- **No headless-by-default assumption.** Headed Chrome is the safe default;
  headless is an operator opt-in with documented anti-bot risk.

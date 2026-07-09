#!/usr/bin/env python
"""ZCode SessionStart hook wrapper for `chatgpt-web2api ensure`.

Runs `chatgpt-web2api ensure` to reconcile the bridge (start REST + SSE if
missing, verify health). ALWAYS exits 0 so a bridge failure never blocks the
ZCode session — the bridge being down is an infrastructure problem, not a
reason to prevent the user from working. Failures are logged to stderr where
ZCode captures them in its hook execution log.

Usage (from ZCode hooks.json):
  command: python.exe
  args: [path/to/ensure_hook.py, --rest-port, 8080, --mcp-sse-port, 8090]

Exit codes:
  0 — always (hook passes regardless of bridge state)
  The bridge's actual state is logged to stderr for diagnosis.
"""
import subprocess
import sys


def main() -> None:
    # Forward our args (minus the script name) to `chatgpt-web2api ensure`.
    # The first arg is this script's path; the rest are ensure's flags.
    ensure_args = sys.argv[1:]
    cmd = [sys.executable, "-m", "chatgpt_web2api", "ensure"] + ensure_args

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,  # Chrome cold-start can take 10-15s; 90s is generous
        )
        # Log the output for ZCode's hook execution log.
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print(
                f"ensure exited {result.returncode} — bridge may need manual "
                f"attention (auth=2, failure=1). This does not block the session.",
                file=sys.stderr,
            )
    except subprocess.TimeoutExpired:
        print(
            "ensure timed out after 90s — bridge may be slow to start. "
            "This does not block the session.",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"ensure hook error: {e}", file=sys.stderr)

    # ALWAYS exit 0 — never block the ZCode session.
    sys.exit(0)


if __name__ == "__main__":
    main()

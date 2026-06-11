#!/bin/bash
set -e

# If cookies file exists, start Chrome first, inject cookies, then start the proxy
COOKIE_FILE="/data/cookies/cookies.json"

if [ -f "$COOKIE_FILE" ]; then
    echo "Found cookies file, injecting..."
    
    # Start proxy in background (Chrome will launch)
    chatgpt-web2api "$@" &
    PROXY_PID=$!
    
    # Wait for Chrome CDP to be ready
    echo "Waiting for Chrome CDP..."
    for i in $(seq 1 30); do
        if curl -s http://127.0.0.1:9222/json/version > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    
    # Inject cookies
    chatgpt-web2api inject-cookies "$COOKIE_FILE" "$@"
    
    # Let cookies propagate to session
    sleep 3
    
    # Wait for the proxy
    wait $PROXY_PID
else
    echo "No cookies file found. Starting without auth."
    echo "Mount cookies at /data/cookies/cookies.json for headless auth."
    exec chatgpt-web2api "$@"
fi

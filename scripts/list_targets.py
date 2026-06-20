"""Enumerate ALL CDP targets to find the service worker (and anything else
non-page) that might be originating the send request."""

import json
import urllib.request

targets = json.loads(
    urllib.request.urlopen(
        urllib.request.Request("http://127.0.0.1:9222/json/list"), timeout=5
    ).read()
)
print(f"Total targets: {len(targets)}")
print()
for t in targets:
    print(f"type: {t.get('type')}")
    print(f"  title: {t.get('title','')[:80]}")
    print(f"  url:   {t.get('url','')[:120]}")
    print(f"  ws:    {t.get('webSocketDebuggerUrl','(none)')[:90]}")
    print()

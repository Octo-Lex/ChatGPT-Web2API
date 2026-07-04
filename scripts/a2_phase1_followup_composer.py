"""Phase 1 follow-up: can we observe the client message UUID BEFORE click_send?

Hypothesis A: the pending message id is in the DOM (data-* attribute) before send.
Hypothesis B: the pending message id is in React fiber state (per-prose form).
Hypothesis C (preferred): capture the outgoing POST body via a temporary fetch
            override installed just before click_send and removed right after.

This script tests the cleanest non-invasive approach: install a temporary
fetch wrapper via Runtime.addBinding / Runtime.evaluate that captures the POST
body of /backend-api/f/conversation, fire a send through the bridge, then read
what was captured. The wrapper is removed (restored to original fetch) before
return.

This does NOT mutate ChatGPT state — it only intercepts the outgoing request
to read its body, then lets it proceed unchanged.
"""
import asyncio, json, urllib.request, time, websockets

CDP = 9222
BRIDGE = "http://127.0.0.1:8080"
CONV = "6a48625b-34a4-83ed-93ba-a7153c2e6295"


def list_targets():
    return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=3).read())


# JS that installs a temporary capture wrapper around window.fetch.
# It stores the original fetch, replaces it with a wrapper that clones the
# /backend-api/f/conversation request and stashes its body, then re-installs
# the original on demand via window.__restoreFetch().
INSTALL_CAPTURE_JS = r"""
(function() {
  if (window.__a2Captured) { return 'already installed'; }
  window.__a2Captured = null;
  window.__restoreFetch = null;
  var origFetch = window.fetch;
  window.fetch = function(input, init) {
    try {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      if (url.indexOf('/backend-api/f/conversation') !== -1 && init && init.method === 'POST' && init.body) {
        // body may be a string (JSON) or a ReadableStream; handle string case.
        var bodyStr = (typeof init.body === 'string') ? init.body : null;
        if (bodyStr) {
          window.__a2Captured = bodyStr;
        }
      }
    } catch(e) { window.__a2CapturedErr = String(e); }
    return origFetch.apply(this, arguments);
  };
  window.__restoreFetch = function() {
    window.fetch = origFetch;
  };
  return 'installed';
})()
"""


READ_CAPTURED_JS = r"""
(function() {
  return JSON.stringify({
    captured: window.__a2Captured || null,
    error: window.__a2CapturedErr || null
  });
})()
"""

RESTORE_JS = r"""
(function() {
  if (window.__restoreFetch) { window.__restoreFetch(); window.__restoreFetch = null; }
  return 'restored';
})()
"""


async def main():
    targets = list_targets()
    tgt = next(t for t in targets if t["type"] == "page" and CONV in t.get("url", ""))
    print(f"attached: {tgt['url'][:80]}")
    ws_url = tgt["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=None) as ws:
        nxt = [0]
        def nid():
            nxt[0] += 1; return nxt[0]

        async def evaluate(expr, timeout_ms=10000):
            await ws.send(json.dumps({"id": nid(), "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "awaitPromise": False, "returnByValue": True, "timeout": timeout_ms}}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == nxt[0]:
                    return r

        # 1. Install the capture wrapper
        r = await evaluate(INSTALL_CAPTURE_JS)
        install_result = r.get("result", {}).get("result", {}).get("value")
        print(f"install: {install_result}")
        if install_result != "installed":
            print("FAILED to install capture wrapper")
            return

        # 2. Fire the bridge send
        marker = f"FOLLOWUP-{int(time.time())}"
        print(f"firing bridge send: marker={marker}")
        body = json.dumps({"model": "auto", "conversation_id": CONV,
                           "messages": [{"role": "user", "content": f"Reply with exactly: {marker}"}],
                           "stream": False}).encode()
        req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        bridge_content = "<error>"
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                p = json.loads(resp.read())
                bridge_content = p.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            bridge_content = f"<bridge error: {e}>"
        print(f"bridge returned ({time.time()-t0:.1f}s): {bridge_content}")

        # 3. Read what was captured
        r = await evaluate(READ_CAPTURED_JS)
        captured_json = r.get("result", {}).get("result", {}).get("value", "{}")
        captured = json.loads(captured_json)

        # 4. Restore original fetch immediately
        r = await evaluate(RESTORE_JS)
        print(f"restore: {r.get('result', {}).get('result', {}).get('value')}")

    # 5. Analyze
    print()
    if captured.get("error"):
        print(f"capture error: {captured['error']}")
    if not captured.get("captured"):
        print("VERDICT: no POST body captured — fetch override did not see the send.")
        print("(The send may use XHR or a different fetch path than window.fetch.)")
        return

    body_str = captured["captured"]
    print(f"captured POST body ({len(body_str)} chars):")
    try:
        parsed = json.loads(body_str)
        msgs = parsed.get("messages") or []
        if msgs:
            client_id = msgs[0].get("id")
            print(f"  messages[0].id (client UUID): {client_id}")
            parts = (msgs[0].get("content") or {}).get("parts") or []
            print(f"  messages[0].content.parts[0][:60]: {str(parts[0])[:60]!r}")
            print(f"  conversation_id: {parsed.get('conversation_id')}")
            print(f"  parent_message_id: {parsed.get('parent_message_id')}")
            print(f"  model: {parsed.get('model')}")
            print()
            print("VERDICT: fetch-wrapper capture WORKS.")
            print("The bridge can install a temporary fetch wrapper before click_send,")
            print("read messages[0].id, restore fetch, and use that UUID for ID-based")
            print("correlation against the backend mapping after completion.")
            print()
            print(f"Observed client UUID: {client_id}")
        else:
            print("  (no messages array in body)")
            print(json.dumps(parsed, indent=2)[:1000])
    except Exception as e:
        print(f"  body parse failed: {e}")
        print(f"  raw (first 800): {body_str[:800]}")


asyncio.run(main())

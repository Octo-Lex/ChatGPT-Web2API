"""Live ambiguous-write certification for PR #52 / issue #51 (authorized).

Protocol (as authorized 2026-09-21): ONE instrumented injection, same
isolation model as Experiment 1B — live bridge 8080 untouched; this script
runs the experiment bridge IN-PROCESS on 8081 from the PR #52 working tree;
Chrome 9222 shared with strict owned-tab isolation; disposable project;
non-stream REST with single_send=true.

Fault injection (per protocol — the socket is NOT killed):
  The experiment instance's websocket send() is wrapped. For the ONE
  send-click Runtime.evaluate (expression carrying dispatchEvent +
  MouseEvent), the frame is forwarded to REAL Chrome (the mutation may
  execute remotely), then the local caller sees a reconnect-class
  ConnectionClosedError — the exact "executed-but-ambiguous" branch.
  recv() always delegates normally, so the IdentityListener keeps
  capturing on the same live socket.

Counters recorded (the certification table):
  mutating_Runtime_evaluate_frames, websocket connections created
  (= reconnect proxy), /backend-api/f/conversation POSTs, captured UUIDs
  (bridge log + REST body), REST status, x-should-retry header, backend
  USER nodes containing the sentinel, backend node id vs captured_user_id.

Output: captures/phase0/live-cert-51-<ts>.json + stdout table.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import pathlib
import secrets
import time
import urllib.error
import urllib.request

import websockets

CDP_HTTP = "http://127.0.0.1:9222"
BRIDGE = "http://127.0.0.1:8081"
CONFIG = "captures/phase0/config-1b.json"
OUT_DIR = pathlib.Path("captures/phase0")

SENTINEL = f"PHASE0-LC51-{secrets.token_hex(4)}"

STATE = {
    "mutating_frames": 0,        # send-click Runtime.evaluate frames sent
    "injected": False,
    "ws_connections": 0,         # every websockets.connect == connect + reconnects
    "all_frames": 0,
    "f_conversation_posts": 0,   # from the tab observer
}


class ConnectionClosedError(Exception):
    """Local stand-in; the transport's _should_reconnect classifier matches
    this CLASS NAME, so the mutation path sees a reconnect-class failure —
    while the real socket underneath stays healthy (the protocol's point)."""


class SendFaultWrapper:
    """Wraps a real websockets connection. Passes everything through except
    send(), which — exactly once, on the send-click mutation frame —
    forwards the frame to Chrome and then raises ConnectionClosedError."""

    def __init__(self, real):
        self._real = real

    async def send(self, data):
        is_mutation = False
        try:
            frame = json.loads(data)
            expr = (frame.get("params") or {}).get("expression", "")
            if (frame.get("method") == "Runtime.evaluate"
                    and "dispatchEvent" in expr and "MouseEvent" in expr):
                is_mutation = True
        except Exception:
            pass
        STATE["all_frames"] += 1
        if is_mutation:
            STATE["mutating_frames"] += 1
        # ALWAYS forward to real Chrome first — the remote may execute it.
        await self._real.send(data)
        if is_mutation and not STATE["injected"]:
            STATE["injected"] = True
            raise ConnectionClosedError(
                "connection closed (injected ambiguity: frame was delivered)")

    async def recv(self):
        return await self._real.recv()

    async def close(self, *a, **kw):
        return await self._real.close(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _patched_connect(*args, **kwargs):
    real_ws = await _REAL_CONNECT(*args, **kwargs)
    STATE["ws_connections"] += 1
    return SendFaultWrapper(real_ws)


class _PatchedConnectFactory:
    """websockets.connect returns an object usable as BOTH `await connect()`
    and `async with connect()`. This factory preserves that dual protocol:
    awaiting or entering returns the fault-wrapped real connection."""

    def __init__(self, args, kwargs):
        self._args, self._kwargs = args, kwargs
        self._ws = None

    async def _connect(self):
        real_ws = await _REAL_CONNECT(*self._args, **self._kwargs)
        STATE["ws_connections"] += 1
        self._ws = SendFaultWrapper(real_ws)
        return self._ws

    def __await__(self):
        return self._connect().__await__()

    async def __aenter__(self):
        return await self._connect()

    async def __aexit__(self, *exc):
        if self._ws is not None:
            await self._ws.close()


_REAL_CONNECT = websockets.connect
websockets.connect = lambda *a, **kw: _PatchedConnectFactory(a, kw)
# covers cdp_driver connect() AND reconnect(); the browser-level
# `async with websockets.connect(...)` in _browser_cdp keeps working.


def tab_ids():
    d = json.loads(urllib.request.urlopen(f"{CDP_HTTP}/json/list", timeout=5).read())
    return {t["id"]: t for t in d if t.get("type") == "page"}


async def tab_network_observer(ws_url: str, events: list, stop_at: float):
    """Record /backend-api/ traffic on the experiment tab (request urls,
    wallTime); remember the f/conversation requestId for body retrieval."""
    async with websockets.connect(ws_url, max_size=10**7, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        mid = 100
        while time.monotonic() < stop_at:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            if m.get("method") == "Network.requestWillBeSent":
                p = m["params"]
                url = p.get("request", {}).get("url", "")
                if "/backend-api/" in url:
                    events.append({"kind": "req", "wall": p.get("wallTime"),
                                   "method": p.get("request", {}).get("method"),
                                   "url": url[:110], "rid": p.get("requestId")})
                    if p["request"].get("method") == "POST" and url.endswith("/f/conversation"):
                        STATE["f_conversation_posts"] += 1
            elif m.get("method") == "Network.responseReceived":
                p = m["params"]
                url = p.get("response", {}).get("url", "")
                if url.endswith("/f/conversation"):
                    events.append({"kind": "resp",
                                   "status": p.get("response", {}).get("status"),
                                   "rid": p.get("requestId")})


async def get_response_body(ws_url: str, rid: str) -> dict:
    """Fetch the f/conversation response body (for conversation_id)."""
    async with websockets.connect(ws_url, max_size=10**7, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.getResponseBody",
                                  "params": {"requestId": rid}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if m.get("id") == 1:
                body = (m.get("result") or {}).get("body") or ""
                try:
                    return json.loads(body)
                except Exception:
                    return {"_raw": body[:200]}


async def projection_lookup(ws_url: str, conv_id: str) -> dict:
    """Authed, structure-only: count USER nodes containing the sentinel."""
    js = """
(async (__D) => {
  var s = await fetch('/api/auth/session', {credentials: 'include'});
  var tok = (await s.json()).accessToken;
  var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=200',
    {credentials: 'include', headers: {'Authorization': 'Bearer ' + tok}});
  var j = await r.json().catch(function() { return {}; });
  var matches = [];
  var m = j.mapping || {};
  for (var k in m) {
    var msg = (m[k].message || {});
    if (msg.author && msg.author.role === 'user') {
      var text = '';
      var parts = (msg.content && msg.content.parts) || [];
      for (var p = 0; p < parts.length; p++) if (typeof parts[p] === 'string') text += parts[p];
      if (text.indexOf(__D.sentinel) !== -1) {
        matches.push({node_id: k, message_id: msg.id || null,
                      create_time: msg.create_time || null});
      }
    }
  }
  return {status: r.status, conversation_id: j.conversation_id || null,
          sentinel_user_nodes: matches};
})
"""
    expr = js + "(" + json.dumps({"conv_id": conv_id, "sentinel": SENTINEL}) + ")"
    async with websockets.connect(ws_url, max_size=10**7, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "silent": True,
                                             "awaitPromise": True,
                                             "returnByValue": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
            if m.get("id") == 1:
                return m.get("result", {}).get("result", {}).get("value") or {}


def fire_rest() -> dict:
    body = json.dumps({
        "model": "auto", "stream": False, "single_send": True,
        "messages": [{"role": "user",
                      "content": f"{SENTINEL} — live ambiguity certification. "
                                 f"Reply with exactly: ACK"}],
    }).encode()
    req = urllib.request.Request(f"{BRIDGE}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return {"status": r.status,
                    "headers": dict(r.headers),
                    "x_should_retry": r.headers.get("x-should-retry"),
                    "body": json.loads(r.read().decode(errors="replace")),
                    "elapsed_s": round(time.time() - t0, 2)}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"_raw": raw[:400]}
        return {"status": e.code, "headers": dict(e.headers),
                "x_should_retry": e.headers.get("x-should-retry"),
                "body": parsed,
                "elapsed_s": round(time.time() - t0, 2)}
    except Exception as e:
        return {"error": str(e)[:300], "elapsed_s": round(time.time() - t0, 2)}


async def main() -> None:
    import logging

    # In-process the service does not configure logging (the CLI entry
    # does); enable INFO so the bridge's own causal lines (send_baseline,
    # identity_capture_success, send_outcome_unknown) appear in the run log.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    from chatgpt_web2api.config import Config
    from chatgpt_web2api.service import Service

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pre = set(tab_ids())
    print(f"sentinel: {SENTINEL}")
    print(f"pre-existing page targets: {len(pre)}")

    config = Config.load(CONFIG)
    service = Service(config)
    # start() ends with `await shutdown_event.wait()` — it is a run-forever
    # call. Run it as a task; request_shutdown() at the end releases it.
    svc_task = asyncio.create_task(service.start())
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BRIDGE}/health", timeout=3).read()
            break
        except Exception:
            await asyncio.sleep(0.5)
    print("experiment bridge started on 8081 (PR #52 tree, in-process)")

    # The experiment tab: the driver's OWN tab (may be reclaimed from the
    # registry by id — no NEW tab appears in that case, so diffing is wrong).
    exp = None
    for _ in range(20):
        await asyncio.sleep(1)
        tid = getattr(service._driver, "_target_id", None)
        now = tab_ids()
        if tid and tid in now:
            exp = now[tid]
            break
        new = [t for t_, t in now.items()
               if t_ not in pre and "chatgpt.com" in t.get("url", "")]
        if new:
            exp = new[0]
            break
    if exp is None:
        raise SystemExit("experiment owned tab not found")
    ws_url = exp["webSocketDebuggerUrl"]
    print(f"experiment tab: {exp['id'][:12]} {exp['url'][:60]}")

    events: list = []
    stop_at = time.monotonic() + 200
    obs = asyncio.create_task(tab_network_observer(ws_url, events, stop_at))

    await asyncio.sleep(2.0)
    rest = await asyncio.to_thread(fire_rest)
    print(f"REST: status={rest.get('status')} elapsed={rest.get('elapsed_s')}s "
          f"x-should-retry={rest.get('x_should_retry')}")
    await asyncio.sleep(4.0)  # let any aftermath settle; observer keeps recording

    # conversation id from the f/conversation response body
    conv_id = None
    resp_ev = next((e for e in events if e["kind"] == "resp"), None)
    if resp_ev:
        try:
            rb = await get_response_body(ws_url, resp_ev["rid"])
            conv_id = rb.get("conversation_id")
            if not conv_id:
                c = rb.get("c")  # {conversation_id: ...} keyed shape fallback
                if isinstance(c, dict):
                    conv_id = list(c.keys())[0]
        except Exception as e:
            print(f"response body fetch failed: {e}")
    print(f"conv_id from f/conversation response: {conv_id}")

    proj = {}
    if conv_id:
        try:
            proj = await projection_lookup(ws_url, conv_id)
        except Exception as e:
            proj = {"error": str(e)[:150]}

    obs.cancel()
    service.request_shutdown()
    try:
        await asyncio.wait_for(svc_task, timeout=20)
    except asyncio.TimeoutError:
        print("service.start() did not return after shutdown request")
    try:
        await asyncio.wait_for(service.stop(), timeout=20)
    except Exception as e:
        print(f"service stop: {e}")
    # close leftover owned tab if the service did not
    now = tab_ids()
    leftover = [tid for tid in now if tid not in pre and "chatgpt.com" in now[tid].get("url", "")]
    for tid in leftover:
        try:
            urllib.request.urlopen(f"{CDP_HTTP}/json/close/{tid}", timeout=5).read()
            print(f"closed leftover experiment tab {tid[:12]}")
        except Exception:
            pass

    captured_user_id = None
    try:
        captured_user_id = (rest["body"].get("error", {})
                            .get("captured_user_id"))
    except Exception:
        pass
    nodes = proj.get("sentinel_user_nodes") or []
    result = {
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "sentinel": SENTINEL, "conv_id": conv_id,
        "code_head": "162c80c (PR #52 tree = 346411c + comment-only fix)",
        "env": {"chrome": "shared 9222, owned-tab isolation",
                "live_bridge": "8080 untouched", "injection": "ws.send wrapper: "
                "forward to real Chrome, then raise ConnectionClosedError — once"},
        "counters": {
            "mutating_Runtime_evaluate_frames": STATE["mutating_frames"],
            "fault_injected": STATE["injected"],
            "websocket_connections_created": STATE["ws_connections"],
            "reconnect_calls": max(0, STATE["ws_connections"] - 1),
            "f_conversation_posts": STATE["f_conversation_posts"],
            "rest_status": rest.get("status"),
            "x_should_retry": rest.get("x_should_retry"),
            "rest_error_code": (rest.get("body", {}).get("error", {}) or {}).get("code"),
            "captured_user_id": captured_user_id,
            "backend_sentinel_user_nodes": len(nodes),
            "backend_node_ids": [n.get("node_id") for n in nodes],
            "backend_ids_match_captured": bool(
                captured_user_id and nodes
                and all(n.get("node_id") == captured_user_id or
                        n.get("message_id") == captured_user_id for n in nodes)),
        },
        "rest": {k: rest.get(k) for k in ("status", "elapsed_s")},
        "network_events": [e for e in events if e["kind"] == "req"][:20],
        "projection": proj,
    }
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"live-cert-51-{ts}.json"
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"\nwrote {path}\n==== LIVE CERTIFICATION COUNTERS ====")
    for k, v in result["counters"].items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(main())

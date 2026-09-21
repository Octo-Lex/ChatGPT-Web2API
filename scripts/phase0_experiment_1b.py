"""Phase 0 Experiment 1B — normal-send causal timeline (authorized controlled mutation).

Fires exactly ONE sentinel turn through the real bridge pipeline of the
isolated experiment instance (REST 8081 → send_and_stream → type → baseline
→ click → IdentityListener → detector → TurnAnchor), while two CDP observer
sessions attached to the experiment's OWNED tab record:

  - Network: every /backend-api/ + /api/auth request (method, url, wallTime)
  - DOM @100ms: composer text length, user/assistant counts, mounted user
    message ids, stop-button/thinking/streaming classes, last assistant
    data-message-model-slug, effort chip label

Safety contract:
  - Observers only attach to the experiment bridge's owned tab (diffed
    against captures/phase0/pre-1b-tabids.txt). No other tab is touched.
  - ONE send, no failures injected, no model/effort change (recorded only).
  - Live bridge on 8080 and the user's tabs are untouched.

Outputs: captures/phase0/exp1b-<ts>.json + stdout summary.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import pathlib
import re
import secrets
import time
import urllib.request

import websockets

CDP_HTTP = "http://127.0.0.1:9222"
BRIDGE = "http://127.0.0.1:8081"
PROJECT_ID = "g-p-6ab0df421d288191b97e2781eb7e16a1"
OUT_DIR = pathlib.Path("captures/phase0")
SAMPLE_MS = 100
GRACE_AFTER_REST_S = 12
REST_TIMEOUT_S = 240

SAMPLER_JS = """
(function() {
  var c = document.querySelector('#prompt-textarea');
  var users = document.querySelectorAll('[data-message-author-role="user"]');
  var assists = document.querySelectorAll('[data-message-author-role="assistant"]');
  var uids = [];
  for (var i = 0; i < users.length; i++) {
    var m = users[i].getAttribute('data-message-id');
    if (m) uids.push(m.slice(0, 8));
  }
  var lastA = assists[assists.length - 1];
  var chip = document.querySelector('button.__composer-pill');
  return {
    comp: !!c,
    compLen: c ? (c.textContent || '').length : 0,
    u: users.length, a: assists.length,
    uids: uids.join(','),
    stop: !!document.querySelector('[data-testid="stop-button"]'),
    think: !!document.querySelector('.result-thinking'),
    stream: !!document.querySelector('.result-streaming'),
    slug: lastA ? lastA.getAttribute('data-message-model-slug') : null,
    effort: chip ? (chip.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 20) : null
  };
})()
"""

PROJECTION_FIND_JS = """
(async function() {
  var s = await fetch('/api/auth/session', {credentials: 'include'});
  var tok = (await s.json()).accessToken;
  var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=200',
    {credentials: 'include', headers: {'Authorization': 'Bearer ' + tok}});
  var j = await r.json().catch(function() { return {}; });
  var mapping = j.mapping || {};
  var found = null; var assistants = [];
  for (var k in mapping) {
    var n = mapping[k];
    var msg = n.message || {};
    var role = msg.author ? msg.author.role : null;
    var text = '';
    var parts = (msg.content && msg.content.parts) || [];
    for (var p = 0; p < parts.length; p++) {
      if (typeof parts[p] === 'string') text += parts[p];
    }
    if (role === 'user' && text.indexOf(__D.sentinel) !== -1 && !found) {
      found = {node_id: k, id: msg.id || null, create_time: msg.create_time || null,
               end_turn: msg.end_turn || null};
    }
    if (role === 'assistant') {
      assistants.push({node_id: k, recipient: msg.recipient || null,
        model: (msg.metadata && (msg.metadata.model_slug || msg.metadata.default_model_slug)) || null,
        create_time: msg.create_time || null, end_turn: msg.end_turn || null,
        text_len: text.length});
    }
  }
  assistants.sort(function(x, y) { return (x.create_time||0) - (y.create_time||0); });
  return JSON.stringify({status: r.status, sentinel_user_node: found,
    assistants_tail: assistants.slice(-4), current_node: j.current_node || null});
})()
"""


def tab_ws_for(target_id: str) -> str | None:
    d = json.loads(urllib.request.urlopen(f"{CDP_HTTP}/json/list", timeout=5).read())
    for t in d:
        if t.get("id") == target_id:
            return t.get("webSocketDebuggerUrl")
    return None


def find_experiment_tab() -> str:
    pre = set((OUT_DIR / "pre-1b-tabids.txt").read_text().split())
    d = json.loads(urllib.request.urlopen(f"{CDP_HTTP}/json/list", timeout=5).read())
    for t in d:
        if (t.get("type") == "page" and t["id"] not in pre
                and "chatgpt.com" in t.get("url", "")):
            return t["id"]
    raise SystemExit("experiment tab not found (diff against pre-1b-tabids.txt)")


async def network_recorder(ws_url: str, events: list, stop_at: float) -> None:
    async with websockets.connect(ws_url, max_size=10**7, open_timeout=5) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
        while time.monotonic() < stop_at:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            if m.get("method") == "Network.requestWillBeSent":
                p = m["params"]
                url = p.get("request", {}).get("url", "")
                if "/backend-api/" in url or "/api/auth" in url:
                    events.append({"kind": "req", "mono": time.monotonic(),
                                   "wall": p.get("wallTime"),
                                   "method": p.get("request", {}).get("method"),
                                   "url": url[:120], "rid": p.get("requestId")})
            elif m.get("method") == "Network.responseReceived":
                p = m["params"]
                url = p.get("response", {}).get("url", "")
                if "/backend-api/" in url or "/api/auth" in url:
                    events.append({"kind": "resp", "mono": time.monotonic(),
                                   "status": p.get("response", {}).get("status"),
                                   "url": url[:120], "rid": p.get("requestId")})


async def dom_sampler(ws_url: str, samples: list, stop_at: float) -> None:
    async with websockets.connect(ws_url, max_size=10**7, open_timeout=5) as ws:
        mid = 500
        while time.monotonic() < stop_at:
            t0 = time.monotonic()
            mid += 1
            try:
                await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                                          "params": {"expression": SAMPLER_JS,
                                                     "silent": True,
                                                     "returnByValue": True}}))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    m = json.loads(raw)
                    if m.get("id") == mid:
                        v = m.get("result", {}).get("result", {}).get("value")
                        if v is not None:
                            samples.append({"mono": t0, "wall": time.time(), **v})
                        break
            except Exception as e:
                samples.append({"mono": t0, "wall": time.time(), "err": str(e)[:60]})
            await asyncio.sleep(max(0.005, SAMPLE_MS / 1000 - (time.monotonic() - t0)))


def fire_rest(sentinel: str) -> tuple[float, float, dict]:
    body = json.dumps({
        "model": "auto",
        "stream": False,
        "project_id": PROJECT_ID,
        "messages": [{"role": "user",
                      "content": f"{sentinel} — instrumentation turn. "
                                 f"Reply with exactly: ACK"}],
    }).encode()
    req = urllib.request.Request(
        f"{BRIDGE}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.monotonic(); wall0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=REST_TIMEOUT_S) as r:
            payload = json.loads(r.read().decode(errors="replace"))
            return t0, wall0, {"status": r.status, "body": payload}
    except urllib.error.HTTPError as e:
        return t0, wall0, {"status": e.code,
                           "body": e.read().decode(errors="replace")[:500]}
    except Exception as e:
        return t0, wall0, {"error": str(e)[:300]}


async def post_projection(ws_url: str, conv_id: str, sentinel: str) -> dict:
    async with websockets.connect(ws_url, max_size=10**7, open_timeout=5) as ws:
        expr = ("var __D = " +
                json.dumps({"conv_id": conv_id, "sentinel": sentinel}) + ";\n" +
                PROJECTION_FIND_JS)
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "silent": True,
                                             "awaitPromise": True,
                                             "returnByValue": True}}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
            if m.get("id") == 1:
                return json.loads(m.get("result", {}).get("result", {})
                                  .get("value") or "{}")


async def main() -> None:
    sentinel = f"PHASE0-1B-{secrets.token_hex(4)}"
    tab = find_experiment_tab()
    ws_url = tab_ws_for(tab)
    print(f"experiment tab: {tab[:12]}  sentinel: {sentinel}")

    t_start = time.monotonic()
    rest_done = asyncio.Event()
    rest_result: dict = {}

    def _fire() -> None:
        t0, wall0, res = fire_rest(sentinel)
        rest_result.update(res); rest_result["t0"] = t0; rest_result["wall0"] = wall0
        rest_result["t_done"] = time.monotonic(); rest_result["wall_done"] = time.time()
        rest_done.set()

    # give observers a 2s head start, then fire
    fire_task = asyncio.get_running_loop().run_in_executor(None, lambda: None)
    net_events: list = []
    samples: list = []

    async def delayed_fire() -> None:
        await asyncio.sleep(2.0)
        await asyncio.to_thread(_fire)

    observers_done = asyncio.Event()

    async def run_observers() -> None:
        await rest_done.wait()
        await asyncio.sleep(GRACE_AFTER_REST_S)
        observers_done.set()

    stop_at_holder = {"until": t_start + REST_TIMEOUT_S + 60}

    async def net_task() -> None:
        await network_recorder(ws_url, net_events, stop_at_holder["until"])
    async def dom_task() -> None:
        await dom_sampler(ws_url, samples, stop_at_holder["until"])

    async def controller() -> None:
        await observers_done.wait()
        stop_at_holder["until"] = time.monotonic()  # stop loops

    await asyncio.gather(
        asyncio.create_task(net_task()),
        asyncio.create_task(dom_task()),
        asyncio.create_task(delayed_fire()),
        asyncio.create_task(run_observers()),
        asyncio.create_task(controller()),
    )

    # conversation id: from network POST url? conv id appears in later GETs
    conv_id = None
    for e in net_events:
        mm = re.search(r"/backend-api/conversation/([0-9a-f-]{36})", e.get("url", ""))
        if mm:
            conv_id = mm.group(1)
            break
    proj = {}
    if conv_id:
        try:
            proj = await post_projection(ws_url, conv_id, sentinel)
        except Exception as e:
            proj = {"error": str(e)[:150]}

    out = {
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "sentinel": sentinel, "project_id": PROJECT_ID, "conv_id": conv_id,
        "env": {"chrome": "shared 9222 with live bridge (recorded limitation)",
                "bridge": "8081 isolated instance, working tree "
                          "feat/sse-pool-status-endpoint (master + /health)"},
        "rest": {k: v for k, v in rest_result.items() if k != "t0"},
        "network": net_events, "dom_samples": samples, "projection": proj,
    }
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"exp1b-{ts}.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {path}")

    # compact summary
    r = rest_result
    print(f"REST status={r.get('status')} err={r.get('error')} "
          f"latency={round((r.get('t_done', 0) - r.get('t0', 0)) * 1000)} ms")
    content = ""
    try:
        content = r.get("body", {}).get("choices", [{}])[0] \
            .get("message", {}).get("content", "")[:80]
    except Exception:
        pass
    print(f"reply: {content!r}")
    print(f"conv_id: {conv_id}")
    print(f"network events: {len(net_events)}, dom samples: {len(samples)}")
    if proj.get("sentinel_user_node"):
        print("backend sentinel node:", proj["sentinel_user_node"])
    print("assistant tail:", json.dumps(proj.get("assistants_tail", []))[:300])


if __name__ == "__main__":
    asyncio.run(main())

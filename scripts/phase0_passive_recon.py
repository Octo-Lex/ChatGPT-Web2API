"""Phase 0 passive upstream recon — selector-agnostic structure capture.

Part of the 2026-09 upstream contract audit (docs/research/).
Safety contract (hard rules, do not relax):
  - Discovers EXISTING chatgpt.com tabs only. Never creates, closes, or
    navigates a tab (tab displacement is a known production defect).
  - Runs read-only JS probes (querySelector/count/attribute inventory).
  - Fetches are same-origin GETs the page itself already makes
    (/api/auth/session, /backend-api/models, /backend-api/conversation/{id}).
  - Records STRUCTURE ONLY: no message text, no conversation titles beyond
    what the tab list already exposes, no accessToken value (key names only).

Output: captures/phase0/passive-<ts>.json plus a stdout summary.
The comparison this exists for: backend mapping node count vs mounted DOM
message count per open conversation — the sectioned-loading question.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import pathlib
import urllib.request

import websockets

CDP = 9222
OUT_DIR = pathlib.Path("captures/phase0")
MAX_PROJECTION_CONVS = 4  # keep GET volume minimal
PROJECTION_LIMIT = 500    # ask for more than any open conversation has

COLLECTOR_JS = r"""
(async function() {
  function attrs(el) {
    var out = [];
    for (var i = 0; i < el.attributes.length; i++) out.push(el.attributes[i].name);
    return out;
  }
  function slim(el) {
    if (!el) return null;
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      classes: (el.className && el.className.toString
                ? el.className.toString().split(/\s+/).slice(0, 4) : []),
      role: el.getAttribute('role'),
      testid: el.getAttribute('data-testid'),
      attrNames: attrs(el).slice(0, 12),
    };
  }
  var res = { path: location.pathname, hrefIsConv: location.pathname.indexOf('/c/') !== -1 };

  // Composer: July selectors individually + generic contenteditable inventory.
  res.julyComposer = {
    primary_div_prompt_textarea: !!document.querySelector('div[role="textbox"]#prompt-textarea'),
    primary_div_prosemirror: !!document.querySelector('div[role="textbox"].ProseMirror'),
    fallback_textarea: !!document.querySelector('textarea#prompt-textarea'),
    any_prompt_textarea_id: !!document.querySelector('#prompt-textarea'),
  };
  res.contentEditables = [];
  var ces = document.querySelectorAll('[contenteditable="true"]');
  for (var i = 0; i < Math.min(ces.length, 5); i++) res.contentEditables.push(slim(ces[i]));

  // Buttons: data-testid inventory (values only) + Send/Stop/Model labels.
  var testids = {};
  var btns = document.querySelectorAll('button[data-testid]');
  for (var j = 0; j < btns.length; j++) {
    var t = btns[j].getAttribute('data-testid');
    testids[t] = (testids[t] || 0) + 1;
  }
  res.buttonTestids = testids;
  res.julySendButton = {
    aria_label_send_not_stop: !!document.querySelector('button[aria-label*="Send" i]:not([data-testid="stop-button"])'),
    testid_send_button: !!document.querySelector('button[data-testid="send-button"]'),
    testid_stop_button: !!document.querySelector('button[data-testid="stop-button"]'),
  };
  res.notableLabels = [];
  var lbls = document.querySelectorAll('button[aria-label]');
  for (var k = 0; k < lbls.length; k++) {
    var L = lbls[k].getAttribute('aria-label') || '';
    if (/send|stop|model|attach|upload|plus/i.test(L)) res.notableLabels.push(L.slice(0, 80));
  }
  res.notableLabels = res.notableLabels.slice(0, 15);

  // Message tree: counts + structural sample of first/last turn.
  var roleSel = '[data-message-author-role]';
  res.messages = {
    containers_by_author_role: {
      user: document.querySelectorAll('[data-message-author-role="user"]').length,
      assistant: document.querySelectorAll('[data-message-author-role="assistant"]').length,
      system: document.querySelectorAll('[data-message-author-role="system"]').length,
    },
    attr_message_id: document.querySelectorAll('[data-message-id]').length,
    conversation_turn_testids: document.querySelectorAll('[data-testid^="conversation-turn"]').length,
    articles: document.querySelectorAll('article').length,
  };
  var all = document.querySelectorAll(roleSel);
  function sample(el) {
    if (!el) return null;
    var s = slim(el);
    s.hasMarkdown = !!el.querySelector('.markdown');
    s.hasResultThinking = !!el.querySelector('.result-thinking');
    s.hasResultStreaming = !!el.querySelector('.result-streaming');
    s.hasCopyTestid = !!el.querySelector('[data-testid*="copy"]');
    s.hasMessageIdAttr = el.hasAttribute('data-message-id');
    return s;
  }
  res.firstMessage = all.length ? sample(all[0]) : null;
  res.lastMessage = all.length ? sample(all[all.length - 1]) : null;

  // Virtualization hints: sentinels, load-more, shrunk history containers.
  res.virtualizationHints = {
    load_more_buttons: document.querySelectorAll('button[aria-label*="load" i], [data-testid*="load-more"]').length,
    virtualization_attr_nodes: document.querySelectorAll('[data-virtualization], [data-virt]').length,
    main_children: document.querySelector('main') ? document.querySelector('main').children.length : null,
  };
  return res;
})()
"""

# Structure-only backend probes. The token is fetched transiently INSIDE the
# page JS and never returned or persisted; the projection fetch reproduces
# the report's authenticated method (Bearer header) — cookie-only requests
# to this endpoint return masked 404s, which the report records explicitly.
AUTH_SHAPE_JS = """
(async function() {
  try {
    var r = await fetch('/api/auth/session', {credentials: 'include'});
    var j = await r.json().catch(function() { return {}; });
    return { status: r.status, keys: Object.keys(j).sort(),
             expires: j.expires || null, hasAccessToken: 'accessToken' in j };
  } catch (e) { return { error: String(e) }; }
})()
"""

MODELS_JS = """
(async function() {
  try {
    var s = await fetch('/api/auth/session', {credentials: 'include'});
    var tok = (await s.json()).accessToken;
    var r = await fetch('/backend-api/models?iim=false&is_gizmo=false',
                        {credentials: 'include',
                         headers: {'Authorization': 'Bearer ' + tok}});
    var j = await r.json().catch(function() { return {}; });
    var rows = (j && j.models) ? j.models : [];
    return { status: r.status, topKeys: Object.keys(j).sort(), count: rows.length,
             slugs: rows.map(function(m) { return m.slug + '|' + (m.title || ''); }).sort() };
  } catch (e) { return { error: String(e) }; }
})()
"""

# Parameterized (NOT self-called): eval_on appends the JSON argument when
# injecting data — the IIFE-parameter pattern, never a top-level var __D.
PROJECTION_JS = """
(async (__D) => {
  try {
    var s = await fetch('/api/auth/session', {credentials: 'include'});
    var tok = (await s.json()).accessToken;
    var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=' + __D.limit,
                        {credentials: 'include',
                         headers: {'Authorization': 'Bearer ' + tok}});
    var j = await r.json().catch(function() { return {}; });
    var mapping = j.mapping || {};
    var counts = { user: 0, assistant: 0, system: 0, tool: 0, other: 0 };
    var node0 = null;
    for (var k in mapping) {
      var node = mapping[k];
      var role = node && node.message ? node.message.author && node.message.author.role : null;
      if (role && counts[role] !== undefined) counts[role]++; else counts.other++;
      if (!node0) node0 = { keys: Object.keys(node).sort(),
                            messageKeys: node.message ? Object.keys(node.message).sort() : null };
    }
    return { status: r.status, topKeys: Object.keys(j).sort(),
             mappingNodes: Object.keys(mapping).length, roleCounts: counts,
             currentNode: j.current_node || null, sampleNode: node0,
             mappingNodeKeysHaveChildren: mapping[j.current_node] ? ('children' in mapping[j.current_node]) : null };
  } catch (e) { return { error: String(e) }; }
})
"""


def list_targets() -> list[dict]:
    data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/list", timeout=5).read())
    return [t for t in data if t.get("type") == "page" and "chatgpt.com" in t.get("url", "")]


def conv_id_from_url(url: str) -> str | None:
    if "/c/" not in url:
        return None
    tail = url.split("/c/")[1]
    return tail.split("/")[0].split("?")[0] or None


async def eval_on(ws, expr: str, data: dict | None = None, timeout: float = 8.0) -> dict:
    """Runtime.evaluate with awaitPromise; returns result value or error info."""
    msg_id = eval_on._next  # type: ignore[attr-defined]
    eval_on._next += 1  # type: ignore[attr-defined]
    if data is not None:
        # Data injected as an IIFE CALL ARGUMENT, never a top-level
        # `var __D` — ChatGPT's page defines its own global __D, and that
        # collision is a bug class this project already diagnosed (the
        # production code moved to this pattern). Data-bearing JS constants
        # in this script are therefore parameterized, not self-called.
        expression = f"{expr}({json.dumps(data)})"
        payload = {"id": msg_id, "method": "Runtime.evaluate",
                   "params": {"expression": expression,
                              "awaitPromise": True, "returnByValue": True, "silent": True}}
    else:
        payload = {"id": msg_id, "method": "Runtime.evaluate",
                   "params": {"expression": expr, "awaitPromise": True,
                              "returnByValue": True, "silent": True}}
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        if msg.get("id") != msg_id:
            continue
        if "error" in msg:
            return {"cdpError": msg["error"].get("message")}
        result = msg.get("result", {}).get("result", {})
        if result.get("subtype") == "error":
            return {"jsError": result.get("description", "")[:300]}
        return result.get("value", {"noValue": True})


eval_on._next = 1  # type: ignore[attr-defined]


async def probe_tab(target: dict, do_backend: bool) -> dict:
    out: dict = {"url": target["url"][:120], "title": target.get("title", "")[:60]}
    if not target.get("webSocketDebuggerUrl"):
        out["error"] = "no webSocketDebuggerUrl (prerendered/discarded)"
        return out
    try:
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=10**7,
                                      open_timeout=5) as ws:
            try:
                out["dom"] = await eval_on(ws, COLLECTOR_JS)
            except Exception as e:  # frozen/discarded tab — record and move on
                out["error"] = f"dom probe failed: {e}"
                return out
            if do_backend:
                out["authShape"] = await eval_on(ws, AUTH_SHAPE_JS)
                out["models"] = await eval_on(ws, MODELS_JS)
    except Exception as e:
        out["error"] = f"connect failed: {e}"
    return out


async def probe_projection(target: dict, conv_ids: list[str]) -> dict:
    results = {}
    try:
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=10**7,
                                      open_timeout=5) as ws:
            for cid in conv_ids[:MAX_PROJECTION_CONVS]:
                try:
                    results[cid] = await eval_on(ws, PROJECTION_JS,
                                                 {"conv_id": cid, "limit": PROJECTION_LIMIT})
                except Exception as e:
                    results[cid] = {"error": str(e)}
    except Exception as e:
        for cid in conv_ids[:MAX_PROJECTION_CONVS]:
            results[cid] = {"error": f"connect failed: {e}"}
    return results


async def main() -> None:
    targets = list_targets()
    print(f"chatgpt.com tabs found: {len(targets)} (passive only — no tab lifecycle, no navigation)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    fresh = next((t for t in targets if t["url"].rstrip("/") == "https://chatgpt.com"), targets[0])
    tabs = await asyncio.gather(*(probe_tab(t, do_backend=(t is fresh)) for t in targets))

    conv_ids = [cid for cid in (conv_id_from_url(t["url"]) for t in targets) if cid]
    # Prefer diverse, live tabs: first plain /c/ conversation, then project-scoped.
    plain = [t for t in targets if conv_id_from_url(t["url"])
             and t["url"].split("/c/")[0].endswith("chatgpt.com/")]
    ordered = []
    for t in plain + [t for t in targets if t not in plain]:
        cid = conv_id_from_url(t["url"])
        if cid and cid not in ordered:
            ordered.append(cid)
    print(f"conversation ids in open tabs: {len(conv_ids)}; probing projection for up to {MAX_PROJECTION_CONVS}")
    projections = await probe_projection(fresh, ordered) if ordered else {}

    report = {
        "captured_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "chrome": json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP}/json/version", timeout=5).read())["Browser"],
        "mode": "passive-only (no navigation, no clicks, GET fetches only)",
        "tabs": tabs,
        "projections": projections,
    }
    out_path = OUT_DIR / f"passive-{ts}.json"
    out_path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {out_path}\n")

    # Stdout summary: the sectioned-loading comparison + selector verdicts.
    for tab in tabs:
        dom = tab.get("dom", {})
        msgs = dom.get("messages", {})
        mounted = msgs.get("containers_by_author_role", {})
        cid = conv_id_from_url(tab["url"])
        proj = projections.get(cid) if cid else None
        print(f"— {tab['url'][:100]}")
        print(f"    composer july-hit={dom.get('julyComposer')} send={dom.get('julySendButton')}")
        print(f"    mounted: user={mounted.get('user')} assistant={mounted.get('assistant')} "
              f"turn-testids={msgs.get('conversation_turn_testids')} articles={msgs.get('articles')}")
        if proj and "mappingNodes" in proj:
            rc = proj.get("roleCounts", {})
            print(f"    backend mapping: total={proj['mappingNodes']} user={rc.get('user')} "
                  f"assistant={rc.get('assistant')} status={proj.get('status')} current_node={bool(proj.get('currentNode'))}")
            dom_total = (mounted.get("user") or 0) + (mounted.get("assistant") or 0)
            be_total = (rc.get("user") or 0) + (rc.get("assistant") or 0)
            if be_total:
                ratio = dom_total / be_total
                verdict = "FULL-MOUNT" if ratio > 0.999 else ("PARTIAL-MOUNT" if ratio >= 0.3 else "HEAVILY-TRIMMED")
                print(f"    >>> sectioned-loading: mounted/backend = {dom_total}/{be_total} = {ratio:.2f} → {verdict}")
    models = next((t.get("models") for t in tabs if t.get("models")), None)
    if models:
        print(f"\nmodels endpoint: status={models.get('status')} count={models.get('count')}")
        for slug in (models.get("slugs") or [])[:25]:
            print(f"    {slug}")
    auth = next((t.get("authShape") for t in tabs if t.get("authShape")), None)
    if auth:
        print(f"\nauth session: status={auth.get('status')} keys={auth.get('keys')} hasAccessToken={auth.get('hasAccessToken')}")


if __name__ == "__main__":
    asyncio.run(main())

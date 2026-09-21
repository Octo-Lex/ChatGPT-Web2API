"""Phase 0 Experiment 1A — long-chat mounting race (authorized controlled read).

Measures, on a positively OWNED tab (created by this script, closed by this
script; the user's 26 tabs are never touched), whether ChatGPT's composer
becomes ready BEFORE the mounted DOM of a 314-node sectioned conversation is
count-stable and identity-stable — the ordering the bridge's send baseline
(`_read_assistant_count_baseline`) implicitly assumes.

Safety contract:
  - Creates ONE new tab via PUT /json/new?about:blank and operates only on
    that target id. Never adopts, navigates, or closes any other tab.
  - Read/navigation only. NO sends, NO clicks, NO typing, no DOM mutation.
  - Navigates the owned tab to the natural 314-node specimen conversation
    (web2api project). Read-only evidence; the conversation is not modified.
  - Closes the owned tab in a finally block.

Per run (5 cold-load runs): Page.navigate (same mechanism as
cdp_driver.py:1301), then sample every ~150 ms: url path, readyState,
composer presence, user/assistant counts, mounted conversation-turn ids,
mounted data-message-ids. After 8 s of unchanged count+identity, an
extended 10 s watch at 250 ms catches late flaps (the virtualization case:
identity churn with constant count).

Output: captures/phase0/exp1a-run<i>.json + stdout analysis.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import pathlib
import time
import urllib.request

import websockets

CDP = "http://127.0.0.1:9222"
CONV_URL = ("https://chatgpt.com/g/g-p-6a3a9ddf947481918dcd9f9bbc80017e-"
            "chatgpt-web2api/c/6a8149d3-3f8c-83eb-89ec-27ed24f8b259")
RUNS = 5
SAMPLE_MS = 150
STABLE_MS = 8000       # count+identity unchanged this long → "stable"
EXTENDED_WATCH_MS = 10000
EXTENDED_SAMPLE_MS = 250
RUN_CAP_MS = 75000
OUT_DIR = pathlib.Path("captures/phase0")

SAMPLER_JS = """
(function() {
  var comp = document.querySelector('#prompt-textarea');
  var turns = document.querySelectorAll('[data-testid^="conversation-turn"]');
  var tids = [];
  for (var i = 0; i < turns.length; i++) tids.push(turns[i].getAttribute('data-testid'));
  var msgs = document.querySelectorAll('[data-message-author-role]');
  var mids = [];
  for (var j = 0; j < msgs.length; j++) {
    var m = msgs[j].getAttribute('data-message-id');
    mids.push(m ? m.slice(0, 8) : '-');
  }
  return {
    path: location.pathname.slice(-24),
    rs: document.readyState,
    comp: !!comp,
    u: document.querySelectorAll('[data-message-author-role="user"]').length,
    a: document.querySelectorAll('[data-message-author-role="assistant"]').length,
    tn: tids.join(','),
    m: mids.join(',')
  };
})()
"""


def http_json(path: str, method: str = "GET") -> dict:
    req = urllib.request.Request(CDP + path, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=8).read())


class OwnedTab:
    """The one tab this script owns; every command goes to this target only."""

    def __init__(self) -> None:
        created = http_json("/json/new?about:blank", method="PUT")
        self.target_id: str = created["id"]
        self._ws_url: str = created["webSocketDebuggerUrl"]
        self._ws = None
        self._next_id = 1
        print(f"owned tab created: target={self.target_id[:12]}…")

    async def connect(self) -> None:
        self._ws = await websockets.connect(self._ws_url, max_size=10**7, open_timeout=5)

    async def reconnect(self) -> None:
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        for t in http_json("/json/list"):
            if t.get("id") == self.target_id and t.get("webSocketDebuggerUrl"):
                self._ws_url = t["webSocketDebuggerUrl"]
                break
        await self.connect()

    async def call(self, method: str, params: dict | None = None, timeout: float = 5) -> dict:
        mid = self._next_id
        self._next_id += 1
        msg = {"id": mid, "method": method, "params": params or {}}
        for attempt in (1, 2):
            try:
                await self._ws.send(json.dumps(msg))
                while True:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
                    got = json.loads(raw)
                    if got.get("id") == mid:
                        return got.get("result", {})
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.4)
                await self.reconnect()
        raise RuntimeError("unreachable")

    async def eval(self, expr: str, timeout: float = 4) -> dict:
        res = await self.call("Runtime.evaluate",
                              {"expression": expr, "returnByValue": True,
                               "silent": True, "awaitPromise": False}, timeout)
        r = res.get("result", {})
        if r.get("subtype") == "error":
            return {"__err": (r.get("description") or "js error")[:120]}
        return r.get("value") if r.get("type") != "undefined" else {"__err": "undefined"}

    async def close(self) -> None:
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        try:
            req = urllib.request.Request(f"{CDP}/json/close/{self.target_id}")
            body = urllib.request.urlopen(req, timeout=8).read().decode(errors="replace")
            print(f"owned tab closed ({body.strip()[:40]})")
        except Exception as e:
            print(f"WARN: tab close failed ({e}) — close target {self.target_id} manually")


def signature(s: dict) -> str:
    return f"{s.get('u')}|{s.get('a')}|{s.get('m')}|{s.get('tn')}"


async def run_once(tab: OwnedTab, run_no: int) -> dict:
    samples: list[dict] = []
    t0 = time.monotonic()

    async def sample() -> None:
        t = round((time.monotonic() - t0) * 1000)
        try:
            v = await tab.eval(SAMPLER_JS)
            if "__err" in v:
                samples.append({"t": t, "err": v["__err"]})
            else:
                samples.append({"t": t, **v})
        except Exception as e:
            samples.append({"t": t, "err": str(e)[:80]})

    await tab.call("Page.bringToFront")
    await tab.call("Page.navigate", {"url": CONV_URL})
    nav_t = round((time.monotonic() - t0) * 1000)

    stable_since = None
    last_sig = None
    extended_until = None
    while True:
        elapsed = (time.monotonic() - t0) * 1000
        if elapsed > RUN_CAP_MS:
            break
        await sample()
        loop_start = time.monotonic()
        valid = [s for s in samples if "err" not in s]
        if valid:
            sig = signature(valid[-1])
            if extended_until is None:
                if sig == last_sig and sig != "":
                    if stable_since is None:
                        stable_since = valid[-1]["t"]
                    if valid[-1]["t"] - stable_since >= STABLE_MS and valid[-1].get("comp"):
                        extended_until = valid[-1]["t"] + EXTENDED_WATCH_MS
                        print(f"  run {run_no}: stable at {stable_since} ms "
                              f"(count+identity); extended watch until {extended_until} ms")
                else:
                    stable_since = None
                last_sig = sig
            elif valid[-1]["t"] >= extended_until:
                break
        # sleep to the next tick
        tick = EXTENDED_SAMPLE_MS if extended_until is not None else SAMPLE_MS
        await asyncio.sleep(max(0.005, tick / 1000 - (time.monotonic() - loop_start)))

    return analyze(samples, run_no, nav_t)


def analyze(samples: list[dict], run_no: int, nav_t: int) -> dict:
    valid = [s for s in samples if "err" not in s]
    errors = [s for s in samples if "err" in s]

    def first(pred) -> int | None:
        for s in valid:
            if pred(s):
                return s["t"]
        return None

    composer_ready = first(lambda s: s.get("comp"))
    counts = [(s["t"], s.get("u", 0) + s.get("a", 0)) for s in valid]
    sigs = [(s["t"], signature(s)) for s in valid]

    def stable_from(series) -> int | None:
        if not series:
            return None
        final = series[-1][1]
        for idx, (t, v) in enumerate(series):
            if v == final and all(w == final for _, w in series[idx:]):
                return t
        return None

    count_stable = stable_from(counts)
    ident_stable = stable_from(sigs)

    # churn: identity changed while count constant (after composer ready)
    churn = 0
    for i in range(1, len(valid)):
        if valid[i - 1].get("comp") and valid[i].get("comp"):
            if (valid[i].get("u"), valid[i].get("a")) == (valid[i - 1].get("u"), valid[i - 1].get("a")) \
                    and signature(valid[i]) != signature(valid[i - 1]):
                churn += 1

    rs_timeline = []
    for s in valid:
        if not rs_timeline or rs_timeline[-1][0] != s.get("rs"):
            rs_timeline.append([s.get("rs"), s["t"]])

    if composer_ready is None:
        verdict = "NO-COMPOSER"
    elif (count_stable or 10**9) > composer_ready or (ident_stable or 10**9) > composer_ready:
        verdict = "RACE"
    else:
        verdict = "CLEAN"

    return {
        "run": run_no, "nav_sent_at_ms": nav_t, "verdict": verdict,
        "composer_ready_ms": composer_ready, "count_stable_ms": count_stable,
        "identity_stable_ms": ident_stable,
        "final_u": valid[-1].get("u") if valid else None,
        "final_a": valid[-1].get("a") if valid else None,
        "final_mounted_ids": valid[-1].get("m") if valid else None,
        "identity_churn_with_constant_count": churn,
        "sample_errors": len(errors),
        "readyState_timeline": rs_timeline,
        "samples": samples,
    }


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tab = OwnedTab()
    results = []
    try:
        await tab.connect()
        for i in range(1, RUNS + 1):
            print(f"run {i}: cold navigate → {CONV_URL[:80]}…")
            r = await run_once(tab, i)
            results.append(r)
            print(f"  run {i}: verdict={r['verdict']} composer_ready={r['composer_ready_ms']} ms "
                  f"count_stable={r['count_stable_ms']} ms identity_stable={r['identity_stable_ms']} ms "
                  f"churn={r['identity_churn_with_constant_count']} "
                  f"final(u/a)={r['final_u']}/{r['final_a']} err={r['sample_errors']}")
            if i < RUNS:
                await tab.call("Page.navigate", {"url": "about:blank"})
                await asyncio.sleep(2.5)
    finally:
        await tab.close()

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (OUT_DIR / f"exp1a-{stamp}.json").write_text(
        json.dumps({"captured_at": stamp, "url": CONV_URL, "runs": results},
                   indent=1), encoding="utf-8")

    races = sum(1 for r in results if r["verdict"] == "RACE")
    churns = sum(1 for r in results if r["identity_churn_with_constant_count"] > 0)
    print(f"\n==== EXPERIMENT 1A SUMMARY ({stamp}) ====")
    print(f"runs={len(results)} RACE={races} CLEAN={len(results) - races} "
          f"runs_with_identity_churn_at_constant_count={churns}")
    for r in results:
        print(f"  run {r['run']}: composer@{r['composer_ready_ms']} count_stable@{r['count_stable_ms']} "
              f"identity_stable@{r['identity_stable_ms']} → {r['verdict']}")
    if races == 0 and churns == 0:
        print("verdict: no post-readiness mounting change observed in THIS environment/"
              "conversation/build/account — DOM-count ack not falsified here (scoped claim)")
    else:
        print("verdict: composer-ready does NOT imply mounted-DOM stability → "
              "baseline-race hazard CONFIRMED at contract level")


if __name__ == "__main__":
    asyncio.run(main())

"""CDP transport — Chrome DevTools Protocol wire primitives.

Phase 5 PR2 extraction (no behavior change). Owns the active page-websocket
wire layer that was previously inlined in ``CDPDriver``:

  - ``_reader_loop`` — background reader; sole consumer of ``_ws.recv()``,
    routes each CDP response to its id-keyed pending Future.
  - ``_cdp`` — send a CDP command + await its response via the future table;
    auto-reconnects once through ``driver.reconnect()`` on socket death.
  - ``_should_reconnect`` — pure error classifier for socket-death signatures.
  - ``_js`` / ``_js_strict`` — soft / strict ``Runtime.evaluate`` wrappers.
  - ``_js_with_data`` / ``_js_with_data_strict`` — safe ``__D`` data injection.

The driver-reference collaborator seam: ``CDPTransport`` holds a reference to
its owning ``CDPDriver`` and reaches through it for the live CDP socket and
the id-keyed response table. None of that state migrates into this module —
it stays on the driver so external attribute reads (connect/reconnect/close,
``is_connected``) and test stubs that poke ``driver._ws`` /
``driver._pending`` / ``driver._reader_task`` keep working unchanged.

Boundary (Layer 1 only): this module is the ACTIVE page-websocket wire layer.
Connection lifecycle, tab discovery (``connect``/``reconnect``/
``_find_*_ws``/``_create_owned_tab``/``_browser_cdp``), and ``close`` stay in
``cdp_driver.py`` (Layer 2). ``reconnect`` owns breaker semantics; this module
calls back into ``driver.reconnect()`` but never duplicates or moves breaker
handling.

Call-rule inside CDPTransport method bodies:

  transport state:         self._driver._ws
                           self._driver._msg_id
                           self._driver._pending
  layer-2 callback:        self._driver.reconnect()
  sibling wire helpers:    self._cdp(...)
                           self._js(...)
                           self._js_strict(...)

Internal wire-to-wire calls (``_js`` → ``_cdp``, ``_js_with_data`` → ``_js``)
go through ``self._driver`` (not ``self``) so driver monkeypatches of
``driver._cdp`` / ``driver._js`` keep intercepting — the same driver-facing
seam BackendClient relies on.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class CDPTransport:
    """Active page-websocket CDP wire primitives, composed by ``CDPDriver``.

    Constructed once in ``CDPDriver.__init__`` and stored as
    ``self._transport``. The driver keeps thin delegating methods for every
    method here so its public/private API surface is byte-identical to
    pre-extraction.
    """

    def __init__(self, driver) -> None:
        self._driver = driver

    # ── CDP primitives ────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Background reader: sole consumer of self._ws.recv().

        Routes each incoming CDP message to the matching pending Future by id.
        Messages without an id (unsolicited CDP events like
        ``Network.requestWillBeSent``, ``Page.frameNavigated``) are dispatched
        to a registered event handler if one exists for the event's method
        name (via ``driver._cdp_event_handlers``); otherwise they are logged
        at DEBUG and discarded.

        Event-handler contract: handlers MUST be fast and non-blocking. The
        reader loop is the sole consumer of ``ws.recv()`` and also resolves
        all pending CDP command futures — blocking it on heavy work (e.g.
        parsing a large POST body synchronously) risks unrelated CDP
        timeouts. Handlers that need to do expensive work should schedule it
        via ``loop.create_task`` and return immediately. If a handler raises,
        it is logged and swallowed so one bad handler cannot kill the reader.

        On ConnectionClosed, fails all pending futures so callers don't hang.
        """
        d = self._driver
        try:
            while True:
                raw = await d._ws.recv()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.debug("CDP reader: unparseable frame, discarding")
                    continue
                mid = msg.get("id")
                if mid is None:
                    # Unsolicited CDP event. Dispatch to a registered handler
                    # if one exists for this method name; else debug-log.
                    method = msg.get("method")
                    if method:
                        # Generic dispatch table on the driver (Layer 2 owns
                        # handler registration — transport is wire-only).
                        handlers = getattr(d, "_cdp_event_handlers", None)
                        handler = handlers.get(method) if handlers else None
                        if handler is not None:
                            try:
                                handler(msg)
                            except Exception:
                                logger.exception(
                                    "CDP event handler failed for %s", method
                                )
                        elif logger.isEnabledFor(logging.DEBUG):
                            logger.debug("CDP event: %s", method)
                    continue
                fut = d._pending.pop(mid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
                else:
                    logger.debug("CDP reader: response for unknown/stale id %s", mid)
        except Exception as e:
            # Socket closed or errored — fail all pending callers so they
            # don't hang waiting for a response that will never arrive.
            logger.warning("CDP reader loop ended: %s", e)
            for mid, fut in list(d._pending.items()):
                if not fut.done():
                    fut.set_exception(e)

    async def _cdp(
        self, method: str, params: dict = None, timeout: float = 15, _retry: bool = True
    ) -> dict:
        """Send a CDP command and await its response.

        Uses the background reader + id-keyed Future table (#7 fix) so
        concurrent _cdp calls each receive their own response without
        cross-eating each other's frames.

        Auto-reconnect: if the underlying WebSocket is dead (ConnectionClosed
        on send — the ``no close frame`` case), attempt ONE ``reconnect()``
        then retry the call. Without this, a single mid-session socket drop
        permanently bricks a long-running bridge: ``reconnect()`` exists but
        nothing wired it in, so every subsequent call re-raised the dead
        socket error forever. ``_retry`` guards against recursion: a call that
        fails again after reconnect propagates instead of looping.

        ``reconnect()`` is a Layer-2 driver method and owns breaker semantics;
        this module never duplicates or moves breaker handling — it only calls
        back into the driver for the one-shot reconnect-and-retry.
        """
        d = self._driver
        d._msg_id += 1
        mid = d._msg_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        d._pending[mid] = fut
        try:
            await d._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except Exception as e:
            d._pending.pop(mid, None)
            if _retry and self._should_reconnect(e):
                logger.warning("CDP send failed (%s); reconnecting and retrying once", e)
                await d.reconnect()
                return await self._cdp(method, params, timeout, _retry=False)
            raise
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            d._pending.pop(mid, None)
            raise TimeoutError(f"CDP timeout: {method}")

    @staticmethod
    def _should_reconnect(exc: Exception) -> bool:
        """True for errors that mean the WebSocket is dead/tearing down.

        ConnectionClosed (and its subclasses) and the ``no close frame``
        InvalidState are the socket-death signatures; everything else
        (TimeoutError, application errors) must NOT trigger a reconnect.
        """
        # websockets.ConnectionClosed + subclasses (ConnectionClosedError,
        # ConnectionClosedOK). Imported lazily so a missing/renamed class in
        # other websockets versions degrades to a name check instead of ImportError.
        name = type(exc).__name__
        if name in {"ConnectionClosed", "ConnectionClosedError", "ConnectionClosedOK"}:
            return True
        msg = str(exc).lower()
        return "no close frame" in msg or "connection closed" in msg

    async def _js(self, expr: str, timeout: float = 15) -> str:
        resp = await self._driver._cdp(
            "Runtime.evaluate",
            {
                "expression": expr,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout,
        )
        return resp.get("result", {}).get("result", {}).get("value", "")

    async def _js_with_data(self, expr_template: str, data: dict, timeout: float = 15) -> str:
        """Evaluate JS with safely injected data variables.

        Injects *data* as the ``__D`` argument of an async IIFE so the
        templates can reference ``__D.keyName`` for any key.  The data is
        passed as a JSON-serialized call argument (never string-concatenated
        into the body), which eliminates injection vectors entirely.

        Earlier versions emitted a top-level ``const __D = ...;``, which
        collides with the global ``__D`` that chatgpt.com's own page defines
        and raised ``SyntaxError: Identifier '__D' has already been
        declared`` — silently returning empty for every
        memory/project/conversation read.  Passing ``__D`` as a function
        parameter sidesteps the collision completely: there is no
        declaration to conflict, and the parameter shadows the global
        within the IIFE's scope.

        *expr_template* is evaluated as an expression in a position where
        its return value becomes the IIFE's result, so existing templates
        (which are self-invoking like ``(async () => {...})()``) keep
        working unchanged.
        """
        # Pass __D as an argument. Using `void ` makes `__D=>(...)` an
        # arrow expression body, so the template's value is returned.
        wrapped = f"( (__D) => ({expr_template}) )({json.dumps(data)})"
        return await self._driver._js(wrapped, timeout=timeout)

    async def _js_strict(self, expr: str, timeout: float = 15) -> str:
        """Strict JS evaluation — raises CDPJSError on failure instead of "".

        Inspects the CDP response for:
        - ``error`` (CDP-level error, e.g. execution context destroyed)
        - ``exceptionDetails`` (JS threw an exception)
        - missing ``result.result`` (undefined return, type mismatch)

        On any of these, raises CDPJSError with the detail. On success,
        returns the value string (same as _js).

        Callers that already handle exceptions benefit immediately. Callers
        that depend on the ""-on-error contract must wrap in try/except.
        """
        # Imported lazily to avoid a module-load circular dependency.
        from .cdp_driver import CDPJSError

        resp = await self._driver._cdp(
            "Runtime.evaluate",
            {
                "expression": expr,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout,
        )
        # CDP-level error (e.g. "Execution context was destroyed")
        if "error" in resp:
            err = resp["error"]
            raise CDPJSError(
                f"CDP error evaluating JS: {err.get('message', err)}",
                details=err,
            )
        result = resp.get("result", {})
        # JS exception
        if result.get("exceptionDetails"):
            exd = result["exceptionDetails"]
            exc_text = exd.get("exception", {}).get("description", "") or exd.get("text", "")
            raise CDPJSError(
                f"JS exception: {exc_text[:500]}",
                details=exd,
            )
        inner = result.get("result", {})
        # Undefined or unserializable return
        if inner.get("type") in ("undefined",) or "value" not in inner:
            raise CDPJSError(
                f"JS returned {inner.get('type', 'unknown')} (no value)",
                details={"type": inner.get("type")},
            )
        return inner.get("value", "")

    async def _js_with_data_strict(
        self, expr_template: str, data: dict, timeout: float = 15
    ) -> str:
        """Strict variant of _js_with_data — raises CDPJSError on failure."""
        wrapped = f"( (__D) => ({expr_template}) )({json.dumps(data)})"
        return await self._driver._js_strict(wrapped, timeout=timeout)

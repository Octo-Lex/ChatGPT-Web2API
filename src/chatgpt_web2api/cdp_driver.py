"""CDP Driver — browser automation via Chrome DevTools Protocol.

Connects to an existing Chrome instance via CDP websocket.
Provides typed primitives for:
  - Auth token management
  - JS evaluation
  - Page navigation
  - Message input via CDP Input.insertText
  - Response retrieval via conversation API
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from .diagnostics import diagnose

try:
    import websockets
except ImportError:
    raise ImportError("pip install websockets")

logger = logging.getLogger(__name__)


@dataclass
class StreamChunk:
    """A single streaming chunk."""
    delta: str
    finish_reason: Optional[str] = None


# Conservative fallback wait (seconds) when ChatGPT's pop-up gives no exact
# number (it usually says "a few minutes"). Chosen to be long enough to let
# a real cooldown clear but short enough that a transient blip recovers fast.
RATE_LIMIT_DEFAULT_RETRY_AFTER = 60

# Re-check the access token if it's older than this. The observed ChatGPT
# JWT has a ~10-day lifetime, so 1h is a conservative refresh interval: it
# avoids unnecessary refetches on the happy path while guaranteeing a stale
# token is refreshed well before its real expiry.
TOKEN_TTL_SECONDS = 3600

# A generation is considered "stuck" (vs. merely slow) if no DOM progress
# signal occurs within this window. Slow-but-progressing generations
# (image rendering, deep-research thinking) legitimately exceed this and
# are allowed the full timeout; a true stall fails fast here instead of
# hanging silently to the deadline. Applied to both Phase 1 (node appear)
# and Phase 2 (text streaming).
PHASE_STALL_SECONDS = 45


class RateLimitError(RuntimeError):
    """Raised when ChatGPT shows its 'Too many requests' rate-limit pop-up.

    Carries ``retry_after`` (seconds) so consumer layers can surface a
    standard OpenAI 429 with a ``Retry-After`` header, or an MCP structured
    result with a machine-readable wait. When the pop-up text is available,
    construct via :meth:`from_text` to parse the duration automatically.

    ChatGPT temporarily throttles rapid conversation access. When this fires
    the assistant never responds, so without detection ``send_and_stream``
    would spin for 60s and time out. Catching the pop-up lets callers fail
    fast with a clear, actionable message.
    """

    def __init__(
        self,
        message: str | None = None,
        retry_after: int = RATE_LIMIT_DEFAULT_RETRY_AFTER,
    ) -> None:
        if message is None:
            message = (
                f"ChatGPT rate limit reached (Too many requests). "
                f"Retry in {retry_after}s."
            )
        super().__init__(message)
        self.retry_after = int(retry_after)

    @classmethod
    def from_text(cls, text: str) -> "RateLimitError":
        """Build a RateLimitError, parsing the wait from the pop-up *text*."""
        retry_after = parse_retry_after(text)
        return cls(retry_after=retry_after)


class AuthExpiredError(RuntimeError):
    """Raised when the ChatGPT access token is stale or rejected (HTTP 401).

    Previously a 401 from /backend-api/* was silently swallowed (reads
    returned []/{}/'', send_and_stream blocked 60s then raised a generic
    "Timed out waiting for assistant response"). This error surfaces the
    real cause so callers can prompt re-login instead of misdiagnosing it
    as a timeout or empty data.
    """

    def __init__(self, message: str | None = None) -> None:
        if message is None:
            message = "ChatGPT session expired — re-login required"
        super().__init__(message)


class GenerationStuckError(RuntimeError):
    """Raised when a generation stalls — no DOM progress within the stall window.

    Distinct from a *slow* generation (which keeps making progress and is
    allowed the full timeout). The ``phase`` and ``stalled_for_s`` attributes
    are machine-readable so MCP/REST layers can surface them in structured
    results; the message is for humans.

    - ``phase == "phase_1_appear"``: assistant message node never appeared.
    - ``phase == "phase_2_stream"``: streaming started but text stopped changing.
    """

    def __init__(self, phase: str, stalled_for_s: float) -> None:
        self.phase = phase
        self.stalled_for_s = float(stalled_for_s)
        super().__init__(
            f"Generation stalled in {phase} for {stalled_for_s:.0f}s — no DOM progress"
        )


class CDPJSError(RuntimeError):
    """Raised by _js_strict when a JS evaluation fails (exceptionDetails or
    CDP-level error). The soft _js collapses these to "" silently; _js_strict
    surfaces them so callers can distinguish "the JS threw" from "the result
    is genuinely empty." Carries the raw exceptionDetails for diagnosis.
    """

    def __init__(self, message: str, details: dict | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


# Phrases ChatGPT uses in its rate-limit pop-up. Matched case-insensitively
# against scanned DOM text. Kept narrow to avoid false positives on normal
# chat content (e.g. a user asking about "rate limits" in a message).
_RATE_LIMIT_PHRASES = (
    "too many requests",
    "you're making requests too quickly",
    "temporarily limited access to your conversations",
    "you've reached the rate limit",
)


def is_rate_limited_text(text: str) -> bool:
    """Return True if *text* looks like ChatGPT's rate-limit pop-up copy."""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in _RATE_LIMIT_PHRASES)


def parse_retry_after(text: str, default: int = RATE_LIMIT_DEFAULT_RETRY_AFTER) -> int:
    """Extract a retry-after duration in seconds from ChatGPT's pop-up text.

    The pop-up usually says "Please wait a few minutes" with no exact number;
    in that case we return *default*. When an explicit number is present
    ("try again in 2 minutes", "wait 30 seconds"), parse and convert it.

    Words like "a few minutes" are deliberately NOT parsed to a specific value
    (they're vague); the conservative default is safer than guessing.
    """
    if not text:
        return default
    lowered = text.lower()

    # Look for "<n> minute(s)" or "<n> min", "<n> second(s)" / "<n> sec(s)".
    # Match digits or number words.
    _NUM_WORDS = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }

    def _to_num(token: str) -> int | None:
        if token.isdigit():
            return int(token)
        return _NUM_WORDS.get(token)

    # "<n> minute(s)" → seconds = n * 60
    m = re.search(r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:minutes?|mins?)", lowered)
    if m:
        n = _to_num(m.group(1))
        if n is not None:
            return n * 60

    # "<n> second(s)" / "<n> sec(s)"
    m = re.search(r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:seconds?|secs?)", lowered)
    if m:
        n = _to_num(m.group(1))
        if n is not None:
            return n

    return default


class CDPDriver:
    """Chrome DevTools Protocol driver for ChatGPT automation."""

    def __init__(self, cdp_port: int = 9222) -> None:
        self.port = cdp_port
        self._ws = None
        self._msg_id = 0
        self._access_token = ""
        self._user_name = ""
        self._token_fetched_at: float = 0.0
        self._current_conv_id: Optional[str] = None
        self._current_model: Optional[str] = None
        # CDP response routing (#7): id-keyed futures + background reader
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    # ── Connection ────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Chrome's CDP and authenticate."""
        ws_url = await self._find_page_ws()
        self._ws = await websockets.connect(
            ws_url, max_size=100 * 1024 * 1024,
            ping_interval=20, ping_timeout=10,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("CDP connected to Chrome")
        await self._refresh_token()

    async def reconnect(self) -> None:
        """Reconnect after a socket drop (#4).

        Re-discovers the page websocket URL (Chrome may have restarted with a
        different one), re-opens the connection, and restarts the background
        reader. Resets stale state (#18): _current_conv_id and _current_model
        are cleared because a socket death almost certainly means the page
        navigated or the tab was closed — the old conversation/model context
        is no longer valid.

        Backoff: 3 attempts at 2s/5s/10s before giving up.
        """
        # Stop the old reader if it's still running
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._reader_task = None
        # Close the dead socket if present
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        # Clear stale state (#18) — the page we reconnect to may be different
        self._current_conv_id = None
        self._current_model = None
        self._pending.clear()

        # Reconnect with backoff
        for attempt, delay in enumerate([2, 5, 10], 1):
            try:
                ws_url = await self._find_page_ws()
                self._ws = await websockets.connect(
                    ws_url, max_size=100 * 1024 * 1024,
                    ping_interval=20, ping_timeout=10,
                )
                self._reader_task = asyncio.create_task(self._reader_loop())
                await self._refresh_token()
                logger.info("CDP reconnected on attempt %d", attempt)
                return
            except Exception as e:
                logger.warning("Reconnect attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(delay)
        raise RuntimeError("CDP reconnect failed after 3 attempts")

    async def _find_page_ws(self) -> str:
        """Find a suitable page's websocket URL."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/list"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            targets = json.loads(resp.read())

        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("No browser pages found — is Chrome running with chatgpt.com?")

        # Prefer chatgpt.com page
        chatgpt = [t for t in pages if "chatgpt.com" in t.get("url", "") or "chatgpt.com" in t.get("title", "")]
        candidates = chatgpt if chatgpt else pages

        # #16: liveness check — skip targets whose WS URL is unreachable
        # (crashed tab, about:blank after recovery, etc.)
        for target in candidates:
            ws_url = target.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            try:
                # Quick HTTP check that the page target is alive
                check_url = f"http://127.0.0.1:{self.port}/json"
                with urllib.request.urlopen(
                    urllib.request.Request(check_url), timeout=3
                ) as check_resp:
                    _alive = json.loads(check_resp.read())
                # If we can reach /json and the target has a WS URL, it's alive
                logger.info("Using page: %s", target.get("title", "")[:60])
                return ws_url
            except Exception:
                logger.debug("Target not alive: %s", target.get("title", "")[:40])
                continue
        # Fallback: return the first candidate even if liveness check failed
        target = candidates[0]
        logger.info("Using page (fallback): %s", target.get("title", "")[:60])
        return target["webSocketDebuggerUrl"]

    async def _refresh_token(self) -> None:
        """Get a fresh access token from /api/auth/session."""
        raw = await self._js(
            "(async () => {"
            "  const r = await fetch('/api/auth/session', {credentials:'include'});"
            "  const d = await r.json();"
            "  return JSON.stringify({token: d.accessToken || '', user: d.user?.name || ''});"
            "})()"
        )
        data = json.loads(raw)
        self._access_token = data.get("token", "")
        self._user_name = data.get("user", "")
        self._token_fetched_at = time.time()
        if not self._access_token:
            raise RuntimeError("No access token — not logged into ChatGPT")
        logger.info("Auth: %d chars, user: %s", len(self._access_token), self._user_name)

    # ── CDP primitives ────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Background reader: sole consumer of self._ws.recv().

        Routes each incoming CDP message to the matching pending Future by id.
        Messages without an id (CDP events like Page.frameNavigated) are logged
        at DEBUG and discarded — no caller subscribes to events today, but the
        hook is here for future navigation-ready detection.

        On ConnectionClosed, fails all pending futures so callers don't hang.
        """
        try:
            while True:
                raw = await self._ws.recv()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.debug("CDP reader: unparseable frame, discarding")
                    continue
                mid = msg.get("id")
                if mid is None:
                    # Unsolicited CDP event — no caller subscribes yet.
                    logger.debug("CDP event: %s", msg.get("method", "?"))
                    continue
                fut = self._pending.pop(mid, None)
                if fut and not fut.done():
                    fut.set_result(msg)
                else:
                    logger.debug("CDP reader: response for unknown/stale id %s", mid)
        except Exception as e:
            # Socket closed or errored — fail all pending callers so they
            # don't hang waiting for a response that will never arrive.
            logger.warning("CDP reader loop ended: %s", e)
            for mid, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(e)

    async def _cdp(self, method: str, params: dict = None, timeout: float = 15) -> dict:
        """Send a CDP command and await its response.

        Uses the background reader + id-keyed Future table (#7 fix) so
        concurrent _cdp calls each receive their own response without
        cross-eating each other's frames.
        """
        self._msg_id += 1
        mid = self._msg_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        except Exception:
            self._pending.pop(mid, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise TimeoutError(f"CDP timeout: {method}")

    async def _js(self, expr: str, timeout: float = 15) -> str:
        resp = await self._cdp("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        }, timeout=timeout)
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
        wrapped = (
            f"( (__D) => ({expr_template}) )({json.dumps(data)})"
        )
        return await self._js(wrapped, timeout=timeout)

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
        resp = await self._cdp("Runtime.evaluate", {
            "expression": expr,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        }, timeout=timeout)
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

    async def _js_with_data_strict(self, expr_template: str, data: dict, timeout: float = 15) -> str:
        """Strict variant of _js_with_data — raises CDPJSError on failure."""
        wrapped = (
            f"( (__D) => ({expr_template}) )({json.dumps(data)})"
        )
        return await self._js_strict(wrapped, timeout=timeout)

    # ── Model Selection ───────────────────────────────────────

    async def select_model(self, slug: str) -> bool:
        """Select a model in the ChatGPT model picker.

        Clicks the model picker button, waits for the dropdown,
        finds the item matching *slug*, and clicks it.

        Returns True if the model was selected, False if it failed
        (e.g. model not found, picker not available).  Failures are
        non-fatal — the request proceeds with whatever model is active.
        """
        if slug in ("auto", None, ""):
            return True  # auto is the default, no action needed

        # Track the current model
        self._current_model = slug

        # Click the model picker button
        picker_clicked = await self._js(
            "(function() {"
            "  var btn = document.querySelector('#model-selector-btn') "
            "    || document.querySelector('button[aria-label*=\"Model\"]') "
            "    || document.querySelector('[data-testid*=\"model\"]') "
            "    || document.querySelector('button[class*=\"model\"]');"
            "  if (!btn) return 'no picker';"
            "  btn.click();"
            "  return 'clicked';"
            "})()"
        )
        if picker_clicked != "clicked":
            logger.warning("Model picker not found: %s — proceeding with active model", picker_clicked)
            return False

        # Wait for dropdown to appear
        await asyncio.sleep(0.8)

        # Find and click the target model item
        # The dropdown renders model items as buttons or list items with the slug
        result = await self._js_with_data(
            "(function() {"
            "  var items = document.querySelectorAll("
            "    'button[data-testid*=\"model\"], "
            "    '[class*=\"model-item\"], "
            "    '[class*=\"modelOption\"], "
            "    'li[class*=\"model\"], "
            "    'div[class*=\"model\"] button'"
            "  );"
            "  for (var i = 0; i < items.length; i++) {"
            "    var el = items[i];"
            "    var text = (el.textContent || '').toLowerCase();"
            "    var dataSlug = (el.getAttribute('data-slug') || '').toLowerCase();"
            "    if (dataSlug === __D.slug || text.indexOf(__D.slug) !== -1) {"
            "      el.click();"
            "      return 'selected';"
            "    }"
            "  }"
            "  // Fallback: try broader search in the dropdown"
            "  var allBtns = document.querySelectorAll('button, [role=\"menuitem\"]');"
            "  for (var j = 0; j < allBtns.length; j++) {"
            "    var t = (allBtns[j].textContent || '').toLowerCase();"
            "    if (t.indexOf(__D.slug) !== -1) {"
            "      allBtns[j].click();"
            "      return 'selected-fallback';"
            "    }"
            "  }"
            "  return 'not-found';"
            "})()",
            {"slug": slug.lower()},
        )

        if result in ("selected", "selected-fallback"):
            logger.info("Model selected: %s (%s)", slug, result)
            await asyncio.sleep(0.5)  # Let UI settle
            return True

        # #8: Close the dropdown if model wasn't found, so it doesn't
        # overlay the textarea and corrupt subsequent type/send operations.
        if result == "not-found":
            try:
                await self._js_strict("document.body.click()")  # dismiss dropdown
            except Exception:
                pass  # best-effort
        logger.warning("Model '%s' not found in picker: %s — proceeding with active model", slug, result)
        return False

    # ── Navigation ────────────────────────────────────────────

    async def navigate_new_chat(self, gizmo_id: str = None) -> None:
        """Navigate to a fresh chat. Optionally scope to a project gizmo."""
        url = f"https://chatgpt.com/g/{gizmo_id}/project" if gizmo_id else "https://chatgpt.com/"
        logger.info("Navigate: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(2)

        # Wait for textarea
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                "    ready: !!document.querySelector('#prompt-textarea'),"
                "    url: location.href"
                "  });"
                "})()"
            )
            try:
                state = json.loads(result)
                if state.get("ready"):
                    actual_url = state.get("url", "")
                    # #14: verify we actually landed on chatgpt.com, not an
                    # error/recovery page that happens to have a textarea.
                    if "chatgpt.com" not in actual_url:
                        raise RuntimeError(
                            f"Navigation landed on unexpected URL: {actual_url}"
                        )
                    logger.info("Page ready: %s", actual_url)
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)

        # Settle time for sentinel init
        await asyncio.sleep(2)
        self._current_conv_id = None

    async def navigate_conversation(self, conversation_id: str) -> None:
        """Navigate to an existing conversation for multi-turn."""
        url = f"https://chatgpt.com/c/{conversation_id}"
        logger.info("Navigate to conversation: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(3)

        # Wait for textarea
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                "    ready: !!document.querySelector('#prompt-textarea'),"
                "    url: location.href"
                "  });"
                "})()"
            )
            try:
                state = json.loads(result)
                if state.get("ready"):
                    actual_url = state.get("url", "")
                    # #14: verify we landed on the right conversation
                    if conversation_id not in actual_url:
                        raise RuntimeError(
                            f"Navigation to {conversation_id} landed on {actual_url}"
                        )
                    logger.info("Conversation ready: %s", actual_url)
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        self._current_conv_id = conversation_id

    # ── Message Input ─────────────────────────────────────────

    async def type_message(self, text: str) -> None:
        """Type text into the ChatGPT prompt textarea."""
        # Focus
        focus_result = await self._js(
            "(function() {"
            "  var el = document.querySelector('#prompt-textarea');"
            "  if (!el) return 'no textarea';"
            "  el.focus();"
            "  return 'focused';"
            "})()"
        )
        if focus_result != 'focused':
            await self._capture_selector_diagnostic("#prompt-textarea (type_message)")
            raise RuntimeError("No textarea found")

        # Clear existing text by selecting all first
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "modifiers": 2})
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "modifiers": 2})
        await asyncio.sleep(0.1)

        # Insert text via CDP
        await self._cdp("Input.insertText", {"text": text})
        await asyncio.sleep(0.5)

        # Verify — use _js_strict so a CDP/JS error surfaces as the real
        # cause rather than a generic "Failed to insert text".
        try:
            content = await self._js_strict(
                "document.querySelector('#prompt-textarea')?.textContent || ''"
            )
        except CDPJSError as e:
            raise RuntimeError(f"Failed to verify text insertion: {e}") from e
        if not content:
            raise RuntimeError("Failed to insert text into textarea")
        logger.info("Typed: %s", text[:80])

    async def click_send(self) -> None:
        """Click the send button via JS MouseEvent sequence."""
        # Wait for button to be enabled
        for _ in range(10):
            has_btn = await self._js(
                "(function() {"
                "  var btn = document.querySelector('button[data-testid=\"send-button\"]');"
                "  return btn && !btn.disabled ? 'yes' : 'no';"
                "})()"
            )
            if has_btn == "yes":
                break
            await asyncio.sleep(0.3)

        result = await self._js(
            "(function() {"
            "  var btn = document.querySelector('button[data-testid=\"send-button\"]');"
            "  if (!btn) return 'no send button';"
            "  if (btn.disabled) return 'button disabled';"
            "  var evts = ['pointerdown','mousedown','pointerup','mouseup','click'];"
            "  for (var i = 0; i < evts.length; i++) {"
            "    btn.dispatchEvent(new MouseEvent(evts[i], {bubbles:true, cancelable:true, view:window}));"
            "  }"
            "  return 'sent';"
            "})()"
        )
        if result != "sent":
            await self._capture_selector_diagnostic("send-button (click_send)")
            raise RuntimeError(f"Send failed: {result}")
        # #13: Verify the send actually landed — ChatGPT clears the textarea
        # on successful send. If it's still populated, the click didn't
        # register (modal overlay, React handler unmounted, etc.). Give it
        # a brief moment to clear.
        await asyncio.sleep(0.3)
        try:
            remaining = await self._js_strict(
                "document.querySelector('#prompt-textarea')?.textContent || ''"
            )
        except Exception:
            remaining = ""  # can't verify — proceed optimistically
        if remaining.strip():
            raise RuntimeError(
                f"Send appeared to succeed but textarea still has content "
                f"(len={len(remaining)}) — click may not have registered"
            )
        logger.info("Message sent")

    # ── Response Retrieval ────────────────────────────────────

    async def send_and_stream(self, text: str, timeout: float = 120) -> AsyncIterator[StreamChunk]:
        """Send a message and yield streaming response chunks.

        This is the main high-level operation:
        1. Count existing assistant messages
        2. Type message
        3. Click send
        4. Wait for new assistant message to appear
        5. Poll DOM for streaming text
        6. Fetch final text from conversation API
        """
        # Count existing assistants BEFORE sending
        try:
            initial_raw = await self._js_strict(
                "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length"
            )
            initial_count = int(initial_raw) if initial_raw else 0
        except CDPJSError as e:
            logger.warning("send_and_stream: initial count failed: %s", e)
            initial_count = 0

        # Type and send
        await self.type_message(text)
        await self.click_send()

        # Wait for a new assistant message. The full `timeout` governs (was
        # capped at 60s, which killed slow-to-appear responses like image
        # generation). A stall detector (PHASE_STALL_SECONDS) catches a true
        # hang fast: if the assistant node count doesn't change at all — even
        # 0→1 with empty text counts as progress — for longer than the stall
        # window, we raise GenerationStuckError instead of waiting out the
        # whole deadline. Slow-but-progressing generations (image render,
        # deep-research thinking) keep resetting the stall clock and are
        # allowed the full timeout.
        deadline = time.monotonic() + timeout
        last_node_count = initial_count
        last_progress = time.monotonic()
        while time.monotonic() < deadline:
            # First check for ChatGPT's rate-limit pop-up — if present, fail
            # fast with a clear error instead of waiting out the whole timeout.
            # The pop-up blocks the assistant from responding, so the assistant
            # count would never increase; without this check we'd hit a generic
            # timeout that hides the real cause.
            try:
                dom_scan = await self._js_strict(
                    "(function(){"
                    "  var t = (document.body && document.body.innerText) || '';"
                    "  return JSON.stringify({text: t.slice(0, 4000)});"
                    "})()"
                )
                scanned_text = json.loads(dom_scan).get("text", "")
            except (CDPJSError, json.JSONDecodeError, TypeError):
                scanned_text = ""
            if is_rate_limited_text(scanned_text):
                # from_text parses any explicit wait from the pop-up copy.
                raise RateLimitError.from_text(scanned_text)

            try:
                raw = await self._js_strict(
                    "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length"
                )
                current_count = int(raw or 0)
            except CDPJSError:
                current_count = last_node_count  # no progress signal
            if current_count != last_node_count:
                # Any node-count change is progress (incl. 0→1 with empty text,
                # the slow-render case). Reset the stall clock.
                last_node_count = current_count
                last_progress = time.monotonic()
            if current_count > initial_count:
                break
            if time.monotonic() - last_progress > PHASE_STALL_SECONDS:
                raise GenerationStuckError(
                    "phase_1_appear", time.monotonic() - last_progress
                )
            await asyncio.sleep(0.5)
        else:
            raise GenerationStuckError(
                "phase_1_appear", timeout
            )

        logger.info("Assistant message appeared, waiting for completion...")

        # Poll until generation is done (Stop button gone). A stall detector
        # (PHASE_STALL_SECONDS) catches a stuck generation: if the DOM text
        # doesn't change at all for longer than the stall window while the Stop
        # button is still present, we raise GenerationStuckError instead of
        # falling through to a silent empty/truncated completion. Any DOM-text
        # change resets the stall clock — including edits/reformats where length
        # stays constant but content changes (current != last_dom_text).
        last_dom_text = ""
        last_change_time = time.monotonic()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = await self._js_strict(
                    "(function() {"
                    "  var msgs = document.querySelectorAll('[data-message-author-role=\"assistant\"]');"
                    "  if (!msgs.length) return JSON.stringify({text:'', done:false});"
                    "  var last = msgs[msgs.length - 1];"
                    "  var md = last.querySelector('.markdown');"
                    "  var text = md ? (md.textContent || '') : '';"
                    "  var stopBtn = document.querySelector('button[aria-label=\"Stop\"]');"
                    "  return JSON.stringify({text: text, done: !stopBtn && !!md});"
                    "})()",
                )
                data = json.loads(result)
            except (CDPJSError, json.JSONDecodeError, TypeError):
                await asyncio.sleep(0.5)
                continue

            current = data.get("text", "")
            done = data.get("done", False)

            if current != last_dom_text:
                last_change_time = time.monotonic()
                if len(current) > len(last_dom_text):
                    # Grew: yield just the newly appended chars.
                    delta = current[len(last_dom_text):]
                    yield StreamChunk(delta=delta)
                # Else: text changed without growing (reformat/edit). Don't yield
                # a delta here — the API reconcile path below corrects the final
                # text. Just reset the stall clock (done above).
                last_dom_text = current

            if done:
                break

            if time.monotonic() - last_change_time > PHASE_STALL_SECONDS:
                raise GenerationStuckError(
                    "phase_2_stream", time.monotonic() - last_change_time
                )

            await asyncio.sleep(0.5)

        # Wait for URL to become /c/{id}
        conv_id = ""
        for _ in range(30):
            try:
                url = await self._js_strict("window.location.href")
            except CDPJSError:
                await asyncio.sleep(0.5)
                continue
            if "/c/" in url:
                conv_id = url.split("/c/")[1].split("/")[0].split("?")[0]
                break
            await asyncio.sleep(0.5)

        if conv_id:
            logger.info("Conversation: %s", conv_id)
            self._current_conv_id = conv_id
            # Fetch final text from API (more reliable than DOM for thinking models)
            for _ in range(60):
                api_text = await self._fetch_text(conv_id)
                if api_text and len(api_text) > len(last_dom_text):
                    yield StreamChunk(delta=api_text[len(last_dom_text):])
                    last_dom_text = api_text
                    break
                if api_text:
                    break
                await asyncio.sleep(0.5)

        yield StreamChunk(delta="", finish_reason="stop")

    async def _fetch_text(self, conversation_id: str) -> str:
        """Fetch the latest assistant text from the conversation API.

        Non-OK responses are encoded by the JS as ``{"__status": <code>}``
        rather than ``''`` so Python can distinguish an auth failure (401 →
        AuthExpiredError) from a missing conversation (404) or a network
        error. This parse-and-raise happens here, before any return reaches
        the caller, so callers never see a raw status blob as text.
        """
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async function() {"
                "  try {"
                "    var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=5', {"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    if (!r.ok) return JSON.stringify({__status: r.status});"
                "    var conv = await r.json();"
                "    var mapping = conv.mapping || {};"
                "    var current = conv.current_node || '';"
                "    // #12: Traverse backward from current_node to find the most"
                "    // recent ASSISTANT node with text. current_node may point at a"
                "    // user message or a wrong-branch leaf after regen/edit."
                "    var n = current;"
                "    var guard = 0;"
                "    while (n && guard < 50) {"
                "      guard++;"
                "      var nd = mapping[n] || {};"
                "      var msg = nd.message;"
                "      if (msg && msg.author && msg.author.role === 'assistant') {"
                "        if (msg.content && msg.content.content_type === 'text') {"
                "          var parts = msg.content.parts || [];"
                "          if (parts.length > 0 && parts[0]) return parts[0];"
                "        }"
                "      }"
                "      n = nd.parent;"
                "    }"
                "    return '';"
                "  } catch(e) { return ''; }"
                "})()",
                {"conv_id": conversation_id, "token": self._access_token},
                timeout=15,
        )
        except CDPJSError as e:
            logger.debug("_fetch_text JS failed (will retry): %s", e)
            return ""
        if not raw:
            return ""
        # Detect the status-blob shape (non-OK response) and raise appropriately.
        # Cheap pre-check before json.loads to avoid parsing every valid text body.
        if raw.startswith('{"__status"') or raw.startswith("{ \"__status\""):
            try:
                payload = json.loads(raw)
                status = payload.get("__status")
            except (json.JSONDecodeError, TypeError):
                status = None
            if status == 401:
                raise AuthExpiredError()
            if status is not None:
                raise RuntimeError(f"_fetch_text HTTP {status} for {conversation_id}")
        return raw

    async def dismiss_rate_limit(self) -> bool:
        """Dismiss ChatGPT's 'Too many requests' pop-up by clicking 'Got it'.

        Targets the pop-up by its text ('Too many requests') rather than fragile
        class names: find the ``[role=dialog]`` whose text matches, then click
        the button inside it whose text is 'Got it'. After clicking, re-scan the
        page to confirm the pop-up cleared.

        Best-effort: never raises. Returns True if the pop-up is gone after the
        attempt, False if it couldn't be dismissed (button missing, or the
        limit persists), None if the status is unknown (scan error — the
        click may have succeeded but we can't confirm). Callers should retry
        on False but NOT on None, to avoid hammering an already-dismissed pop-up.
        """
        click_js = (
            "(function(){"
            "  try {"
            "    var dlgs = document.querySelectorAll('[role=dialog]');"
            "    var target = null;"
            "    for (var i = 0; i < dlgs.length; i++) {"
            "      if (/too many requests/i.test(dlgs[i].innerText || '')) { target = dlgs[i]; break; }"
            "    }"
            "    if (!target) return JSON.stringify({clicked: false});"
            "    var btns = target.querySelectorAll('button');"
            "    var btn = null;"
            "    for (var j = 0; j < btns.length; j++) {"
            "      if ((btns[j].innerText || '').trim().toLowerCase() === 'got it') { btn = btns[j]; break; }"
            "    }"
            "    if (!btn) return JSON.stringify({clicked: false});"
            "    btn.click();"
            "    return JSON.stringify({clicked: true});"
            "  } catch(e) { return JSON.stringify({clicked: false, error: e.message}); }"
            "})()"
        )
        try:
            click_raw = await self._js_strict(click_js, timeout=10)
            clicked = json.loads(click_raw).get("clicked", False) if click_raw else False
        except Exception:  # best-effort: never raise
            logger.warning("dismiss_rate_limit: click failed", exc_info=True)
            return None  # unknown — don't trigger retry storm
        if not clicked:
            return False

        # Re-scan to confirm the pop-up cleared.
        try:
            scan = await self._js_strict(
                "(function(){var t=(document.body&&document.body.innerText)||'';"
                "return JSON.stringify({text:t.slice(0,4000)});})()",
                timeout=10,
            )
            text = json.loads(scan).get("text", "") if scan else ""
        except Exception:
            # #19: If the re-scan errors, the status is unknown (not False).
            # Returning False would trigger a retry storm against an already-
            # dismissed pop-up; returning None lets callers skip the retry.
            return None
        return not is_rate_limited_text(text)

    def _check_auth_in_raw(self, raw: str) -> None:
        """#20: Detect auth failure in raw response text and raise.

        Most read methods' JS doesn't check r.ok or r.status — a 401 returns
        the HTML login page body. This helper catches that case in Python so
        a stale token surfaces as AuthExpiredError instead of empty data.
        Called after _js_with_data_strict returns, before json.loads.
        """
        if not raw:
            return
        # Login pages contain these markers
        lower = raw[:500].lower()
        if "sign in" in lower and "chatgpt" in lower and "<html" in lower:
            raise AuthExpiredError(
                "Session expired — read returned login page instead of data"
            )

    async def _capture_selector_diagnostic(self, selector_name: str) -> None:
        """#5: Capture DOM state when a selector fails to match.

        Logs a diagnostic snapshot (URL, title, body text preview, button count)
        so selector drift is diagnosable without W2A_DIAGNOSE=1. Called at
        the point of selector failure (e.g. 'no send button', 'No textarea').
        Best-effort — never raises.
        """
        try:
            snapshot = await self._js_strict(
                "(function(){"
                "  return JSON.stringify({"
                "    url: location.href,"
                "    title: document.title,"
                "    body_preview: (document.body && document.body.innerText || '').slice(0, 300),"
                "    button_count: document.querySelectorAll('button').length,"
                "    textarea_count: document.querySelectorAll('textarea').length"
                "  });"
                "})()",
                timeout=5,
            )
            logger.warning(
                "Selector drift diagnostic (%s): %s", selector_name, snapshot
            )
        except Exception:
            logger.warning("Selector drift diagnostic (%s): capture failed", selector_name)

    # ── API helpers ───────────────────────────────────────────

    @diagnose("get_models")
    async def get_models(self) -> list[dict]:
        """List available models.

        The ChatGPT API returns ``{"title":..., "models":[{"slug":..., ...}]}``
        as a JSON string. Parse it and return just the models array so callers
        get the ``list[dict]`` the signature promises (each with ``slug`` and
        ``title``). Earlier this returned the raw string, which made
        ``do_list_models`` crash on ``m.get('slug')`` — only live testing
        caught it, since the mocked unit tests returned dicts.
        """
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/models?iim=false&is_gizmo=false', {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  return await r.text();"
                "})()",
                {"token": self._access_token},
            )
            self._check_auth_in_raw(raw)
            data = json.loads(raw)
        except (CDPJSError, json.JSONDecodeError, TypeError) as e:
            logger.warning("get_models failed: %s", e)
            return []
        if isinstance(data, dict):
            return data.get("models", [])
        if isinstance(data, list):
            return data
        return []

    @diagnose("get_projects")
    async def get_projects(self) -> list[dict]:
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=5&limit=50', {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  var data = await r.json();"
                "  return JSON.stringify((data.items || []).map(function(i) {"
                "    var g = (i.gizmo || {}).gizmo || {};"
                "    return {id: g.id, name: (g.display || {}).name || '', memory_scope: g.memory_scope || '', short_url: g.short_url || ''};"
                "  }));"
                "})()",
                {"token": self._access_token},
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_projects failed: %s", e)
            return []

    # ── Conversation Management ──────────────────────────────

    @diagnose("get_conversations")
    async def get_conversations(
        self,
        offset: int = 0,
        limit: int = 28,
        order: str = "updated",
    ) -> list[dict]:
        """List recent conversations."""
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/conversations?offset=' + __D.offset + '&limit=' + __D.limit + '&order=' + __D.order, {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  var data = await r.json();"
                "  return JSON.stringify((data.items || []).map(function(c) {"
                "    return {id: c.id, title: c.title || 'Untitled', "
                "      update_time: c.update_time, create_time: c.create_time,"
                "      is_archived: !!c.is_archived, gizmo_id: c.gizmo_id || null};"
                "  }));"
                "})()",
                {"token": self._access_token, "offset": str(offset), "limit": str(limit), "order": order},
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_conversations failed: %s", e)
            return []

    @diagnose("get_conversation")
    async def get_conversation(self, conversation_id: str) -> dict:
        """Get full conversation detail with message mapping."""
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  return await r.text();"
                "})()",
                {"conv_id": conversation_id, "token": self._access_token},
                timeout=30,
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_conversation failed: %s", e)
            return {}

    @diagnose("delete_conversation")
    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation. Returns True on success."""
        await self.ensure_token()
        try:
            result = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
                "      method: 'PATCH',"
                "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
                "      body: JSON.stringify({is_visible: false})"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"conv_id": conversation_id, "token": self._access_token},
            )
        except CDPJSError as e:
            logger.warning("delete_conversation JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info("Deleted conversation: %s", conversation_id)
            if self._current_conv_id == conversation_id:
                self._current_conv_id = None
            return True
        logger.warning("Failed to delete conversation %s: %s", conversation_id, result)
        return False

    async def rename_conversation(
        self, conversation_id: str, title: str
    ) -> bool:
        """Rename a conversation. Returns True on success."""
        await self.ensure_token()
        try:
            result = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
                "      method: 'PATCH',"
                "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
                "      body: JSON.stringify({title: __D.title})"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"conv_id": conversation_id, "token": self._access_token, "title": title},
            )
        except CDPJSError as e:
            logger.warning("rename_conversation JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info("Renamed conversation %s to: %s", conversation_id, title)
            return True
        logger.warning("Failed to rename conversation: %s", result)
        return False

    # ── Project Management ────────────────────────────────────

    @diagnose(
        "create_project",
        capture_js=lambda self: (
            "POST /backend-api/projects",
            {"name": "<arg>", "instructions": "<arg>", "memory_scope": "<arg>"},
        ),
    )
    async def create_project(
        self,
        name: str,
        instructions: str = "",
        memory_scope: str = "project_v2",
    ) -> dict:
        """Create a new ChatGPT project.

        Args:
            name: Project display name
            instructions: Custom instructions for the project
            memory_scope: 'project_v2' (Project-only — isolated memory, the
                dedicated scope) or 'global' (Default — shares memory with
                outside chats, mapped to the API's 'unset' value).

        Returns:
            Created project dict with id (g-p-...), name, memory_scope, etc.

        Note: Projects and Custom GPTs are now separate endpoints. Projects
        live at ``/backend-api/projects`` and create a ``snorlax`` gizmo (id
        prefix ``g-p-``); the legacy ``/backend-api/gizmos`` endpoint creates a
        ``gpt`` gizmo instead. The payload + endpoint here were captured from
        ChatGPT's own UI via browser automation (Super-Browser network capture).
        """
        # The UI sends "unset" for the Default (shared) memory option and
        # "project_v2" for Project-only. Map our public values accordingly.
        api_memory_scope = "project_v2" if memory_scope == "project_v2" else "unset"
        await self.ensure_token()
        raw = await self._js_with_data(
            "(async () => {"
            "  try {"
            "    var body = {"
            "      name: __D.name,"
            "      instructions: __D.instructions,"
            "      memory_scope: __D.api_memory_scope"
            "    };"
            "    var r = await fetch('/backend-api/projects', {"
            "      method: 'POST',"
            "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
            "      body: JSON.stringify(body)"
            "    });"
            "    if (!r.ok) return JSON.stringify({error: 'HTTP ' + r.status, body: await r.text()});"
            "    var data = await r.json();"
            "    var g = ((data.resource || {}).gizmo) || data.gizmo || data;"
            "    return JSON.stringify({"
            "      id: g.id,"
            "      name: (g.display || {}).name || '',"
            "      memory_scope: g.memory_scope || '',"
            "      instructions: g.instructions || '',"
            "      gizmo_type: g.gizmo_type || ''"
            "    });"
            "  } catch(e) { return JSON.stringify({error: e.message}); }"
            "})()",
            {
                "token": self._access_token,
                "name": name,
                "instructions": instructions,
                "api_memory_scope": api_memory_scope,
            },
            timeout=20,
        )
        try:
            self._check_auth_in_raw(raw)
            result = json.loads(raw)
            if "error" in result:
                logger.error("Create project failed: %s", result["error"])
                return result
            logger.info("Created project: %s (%s)", result.get("name"), result.get("id"))
            return result
        except json.JSONDecodeError:
            return {"error": "Invalid response"}

    @diagnose(
        "update_project_instructions",
        capture_js=lambda self: (
            "PATCH /backend-api/projects/{id}",
            {"instructions": "<arg>"},
        ),
    )
    async def update_project_instructions(
        self,
        project_id: str,
        instructions: str,
    ) -> bool:
        """Update a project's custom instructions. Returns True on success.

        Projects are mutated via PATCH /backend-api/projects/{id} with a flat
        body. The API requires the current ``name`` in the body (it's a full
        project-shape PATCH, not a partial), so we fetch the project's name
        first and include it alongside the new instructions. Captured from
        ChatGPT's own UI via Super-Browser network capture.
        """
        await self.ensure_token()
        try:
            result = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r0 = await fetch('/backend-api/gizmos/' + __D.project_id, {"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    var d0 = await r0.json();"
                "    var g0 = d0.gizmo || d0;"
                "    var name = ((g0.display) || {}).name || '';"
                "    var emoji = ((g0.display) || {}).emoji || null;"
                "    var theme = ((g0.display) || {}).theme || null;"
                "    var body = {name: name, instructions: __D.instructions, emoji: emoji, theme: theme};"
                "    var r = await fetch('/backend-api/projects/' + __D.project_id, {"
                "      method: 'PATCH',"
                "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
                "      body: JSON.stringify(body)"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"token": self._access_token, "project_id": project_id, "instructions": instructions},
                timeout=20,
            )
        except CDPJSError as e:
            logger.warning("update_project_instructions JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info("Updated instructions for project: %s", project_id)
            return True
        logger.warning("Failed to update project instructions: %s", result)
        return False

    async def get_project_detail(self, project_id: str) -> dict:
        """Get full project/gizmo detail."""
        raw = await self._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  return await r.text();"
            "})()",
            {"token": self._access_token, "project_id": project_id},
            timeout=15,
        )
        try:
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # ── Archive Conversation ────────────────────────────────

    @diagnose(
        "archive_conversation",
        capture_js=lambda self: (
            "PATCH /backend-api/conversation/{id}",
            {"archive": "<arg>"},
        ),
    )
    async def archive_conversation(
        self, conversation_id: str, archive: bool = True
    ) -> bool:
        """Archive or unarchive a conversation. Returns True on success."""
        await self.ensure_token()
        try:
            result = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
                "      method: 'PATCH',"
                "      headers: {'Authorization': 'Bearer ' + __D.token, 'Content-Type': 'application/json'},"
                "      body: JSON.stringify({is_archived: __D.archive})"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"conv_id": conversation_id, "token": self._access_token, "archive": archive},
            )
        except CDPJSError as e:
            logger.warning("archive_conversation JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info("%s conversation: %s", 'Archived' if archive else 'Unarchived', conversation_id)
            return True
        logger.warning("Failed to archive conversation: %s", result)
        return False

    # ── Memory Management ─────────────────────────────────────

    @diagnose("get_memories")
    async def get_memories(self) -> list[dict]:
        """List all ChatGPT memories."""
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/memories', {"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    if (!r.ok) return JSON.stringify({error: 'HTTP ' + r.status});"
                "    var data = await r.json();"
                "    return JSON.stringify(data);"
                "  } catch(e) { return JSON.stringify({error: e.message}); }"
                "})()",
                {"token": self._access_token},
                timeout=15,
            )
            self._check_auth_in_raw(raw)
            data = json.loads(raw)
            if isinstance(data, dict) and "error" in data:
                logger.error("Get memories failed: %s", data["error"])
                return []
            if isinstance(data, list):
                return data
            for key in ("memories", "items", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return []
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_memories failed: %s", e)
            return []

    @diagnose("create_memory")
    async def create_memory(self, content: str) -> dict:
        """Create a memory by sending a chat message asking ChatGPT to remember.

        The POST /backend-api/memories endpoint returns 405 — ChatGPT only
        creates memories through conversation. This method sends a message
        asking ChatGPT to remember the content, which triggers the memory
        system automatically.
        """
        memory_prompt = (
            f"Please remember this for all future conversations: {content}"
        )

        # Navigate to a fresh chat for memory creation
        await self.navigate_new_chat()

        # Send and collect the response
        full_response = ""
        async for chunk in self.send_and_stream(memory_prompt, timeout=60):
            if chunk.delta:
                full_response += chunk.delta

        conv_id = self._current_conv_id or ""

        logger.info("Memory creation request sent via chat (conv: %s)", conv_id)

        # #17: Check if the memory was actually created by looking for it
        # in the memories list. ChatGPT may refuse or paraphrase — without
        # this check the caller can't tell success from failure.
        memory_created = False
        try:
            memories = await self.get_memories()
            memory_created = any(
                content[:30].lower() in (m.get("content", "")[:50].lower())
                for m in memories
            )
        except Exception:
            pass  # best-effort verification

        return {
            "content": content,
            "method": "chat",
            "conversation_id": conv_id,
            "response": full_response[:200],
            "success": memory_created,
            "note": (
                "Memory creation happens via chat — ChatGPT may paraphrase "
                "or decline. Verified via list_memories."
            ),
        }

    @diagnose("delete_memory")
    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a ChatGPT memory by ID. Returns True on success."""
        await self.ensure_token()
        try:
            result = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/memories/' + __D.memory_id, {"
                "      method: 'DELETE',"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"memory_id": memory_id, "token": self._access_token},
                timeout=15,
            )
        except CDPJSError as e:
            logger.warning("delete_memory JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info("Deleted memory: %s", memory_id)
            return True
        logger.warning("Failed to delete memory %s: %s", memory_id, result)
        return False

    @diagnose("delete_project")
    async def delete_project(self, project_id: str) -> dict:
        """Delete a ChatGPT project by ID. Returns {success, project_id}.

        Projects are deleted via DELETE /backend-api/gizmos/{id} (the gizmos
        endpoint serves both Projects (g-p-) and Custom GPTs (g-) for deletion;
        creation is split across /projects and /gizmos, but deletion is shared).
        Verified to return 200 against a live account.
        """
        await self.ensure_token()
        try:
            result = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
                "      method: 'DELETE',"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"project_id": project_id, "token": self._access_token},
                timeout=15,
            )
        except CDPJSError as e:
            logger.warning("delete_project JS failed: %s", e)
            result = "false"
        success = result == "true"
        if success:
            logger.info("Deleted project: %s", project_id)
        else:
            logger.warning("Failed to delete project %s: %s", project_id, result)
        return {"success": success, "project_id": project_id}

    # ── Custom GPT Navigation ─────────────────────────────────

    async def navigate_gpt(self, gizmo_id: str) -> None:
        """Navigate to a Custom GPT for interaction."""
        url = f"https://chatgpt.com/g/{gizmo_id}"
        logger.info("Navigate to GPT: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(3)
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                "    ready: !!document.querySelector('#prompt-textarea'),"
                "    url: location.href"
                "  });"
                "})()",
            )
            try:
                state = json.loads(result)
                if state.get("ready"):
                    logger.info("GPT page ready: %s", state.get("url"))
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)
        self._current_conv_id = None

    @diagnose("list_gpts")
    async def list_gpts(self) -> list[dict]:
        """List Custom GPTs (non-project gizmos).

        Projects (gizmo_type='snorlax') are excluded — use get_projects()
        for those.  Only marketplace or user-created non-project GPTs
        are returned.
        """
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/gizmos/snorlax/sidebar?owned_only=false&conversations_per_gizmo=0&limit=100', {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  var data = await r.json();"
                "  return JSON.stringify((data.items || []).map(function(i) {"
                "    var g = (i.gizmo || {}).gizmo || {};"
                "    if (g.gizmo_type === 'snorlax' && g.memory_scope) return null;"
                "    return {id: g.id, name: (g.display || {}).name || '', "
                "      description: (g.display || {}).description || '',"
                "      gizmo_type: g.gizmo_type || ''};"
                "  }).filter(Boolean));"
                "})()",
                {"token": self._access_token},
                timeout=20,
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("list_gpts failed: %s", e)
            return []

    # ── Project Files ─────────────────────────────────────────

    @diagnose("get_project_files")
    async def get_project_files(self, project_id: str) -> list[dict]:
        """List files attached to a ChatGPT project."""
        await self.ensure_token()
        try:
            raw = await self._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    if (!r.ok) return '[]';"
                "    var data = await r.json();"
                "    var gizmo = data.gizmo || data;"
                "    var files = gizmo.files || [];"
                "    return JSON.stringify(files.map(function(f) {"
                "      return {id: f.id || '', name: f.file_name || f.name || '', "
                "        size: f.size || 0, mime_type: f.mime_type || ''};"
                "    }));"
                "  } catch(e) { return '[]'; }"
                "})()",
                {"token": self._access_token, "project_id": project_id},
                timeout=15,
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_project_files failed: %s", e)
            return []

    # ── Token Management ──────────────────────────────────────

    async def ensure_token(self) -> str:
        """Ensure a non-stale access token, refreshing if empty OR older than TTL.

        Returns the token. The TTL guard (TOKEN_TTL_SECONDS) catches expiry
        well before the real JWT lifetime; callers should invoke this before
        any /backend-api/* fetch so a stale session surfaces as
        AuthExpiredError (via _fetch_text) rather than silent empty data.
        """
        stale = (
            not self._access_token
            or time.time() - self._token_fetched_at > TOKEN_TTL_SECONDS
        )
        if stale:
            await self._refresh_token()
        return self._access_token

    # ── Lifecycle ─────────────────────────────────────────────

    async def close(self) -> None:
        # Stop the background reader first
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._reader_task = None
        # Fail any pending futures so callers don't hang
        for mid, fut in list(self._pending.items()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("CDP driver closed")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

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
import urllib.parse
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
# and Phase 2 (text streaming). 90s accommodates reasoning/thinking models,
# whose ``result-thinking`` placeholder can hold the DOM static for a minute+
# while the model reasons before the first answer token renders; the
# is_thinking reset covers the labeled phase, but there is an unlabeled gap
# between thinking-end and answer-start that also needs this headroom.
PHASE_STALL_SECONDS = 90

# How long to wait (seconds) for a freshly-created owned tab to settle on
# chatgpt.com before refreshing the access token. ``_create_owned_tab`` only
# waits for the target's webSocketDebuggerUrl to appear in /json/list, which
# fires within milliseconds of Target.createTarget — well before the page has
# navigated to chatgpt.com. Calling ``_refresh_token`` on that cold tab races:
# the relative ``fetch('/api/auth/session')`` resolves against the wrong origin
# (e.g. about:blank) and returns an empty accessToken, tripping the auth gate
# and killing the whole MCP process on startup. Polling for readiness first
# (page on chatgpt.com + readyState !== 'loading') lets the fetch resolve
# correctly. 10s is generous for even a slow first load; the 0.5s poll cadence
# matches ``navigate_new_chat``.
_CONNECT_READY_TIMEOUT = 10

# ChatGPT composer selectors (post-2026 composer redesign).
#
# The old composer was a real <textarea id="prompt-textarea">. The new
# composer is a contenteditable ProseMirror div; the element that still
# carries id="prompt-textarea" is a HIDDEN fallback (<textarea
# class="wcDTda_fallbackTextarea">) that overlays the real composer when
# JS is off. Typing into it does not reach the composer, so the message
# never lands — every send then fails with "no send button" because the
# composer is empty. These selectors target the real, interactive nodes.
#
# COMPOSER_SELECTOR is the primary target; the #prompt-textarea fallback
# is kept as a last-resort so the driver still works if ChatGPT rolls
# the composer back (or on an A/B holdout that hasn't shipped the new
# UI). Both are tried in preference order by the helpers below.
COMPOSER_SELECTOR = 'div[role="textbox"]#prompt-textarea, div[role="textbox"].ProseMirror'
COMPOSER_FALLBACK_SELECTOR = "textarea#prompt-textarea"

# The send button. The new composer has no data-testid="send-button" —
# its affordances are composer-plus-btn and dictation, plus a
# stop-button while generating. The send affordance is the submit
# <button> inside the composer form whose aria-label is "send" (and
# which is not the stop button). We match by aria-label first, then by
# the legacy testid for older deployments.
SEND_BUTTON_SELECTOR = 'button[aria-label*="Send" i]:not([data-testid="stop-button"])'
SEND_BUTTON_FALLBACK_SELECTOR = 'button[data-testid="send-button"]'


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
        # Tab isolation: the targetId of the tab this driver is attached to.
        # _owns_target records whether *we* created it: only tabs we created are
        # closed in close(), so a driver that adopted an existing tab (e.g.
        # Chrome's launch tab) never closes a tab it didn't open — preventing
        # tab accumulation across service restarts while preserving the user's
        # open tabs on clean shutdown.
        self._target_id: Optional[str] = None
        self._owns_target: bool = False

    # ── Connection ────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to Chrome's CDP and authenticate.

        Tab isolation: creates a dedicated chatgpt.com tab via Target.createTarget
        so this process owns its own DOM (no cross-process tab sharing). Falls back
        to the shared-tab discover-and-grab pattern if createTarget fails.

        If already connected (e.g. Service reconnects after login), reuses the
        existing owned tab instead of creating a new one.
        """
        # If we already own a tab from a prior connect attempt, reuse it
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._reader_task = None

        # Resolve which tab to attach to, in priority order. The goal is to
        # never open a redundant tab when a usable chatgpt.com tab already
        # exists (Chrome's launch tab, a leftover from a prior run, or our
        # own previously-owned tab). Ownership (_owns_target) only affects
        # whether close() will tear the tab down — it does not affect reuse.
        #
        #   1. Re-attach to a tab we already know about (_target_id set from
        #      a prior connect), whether we created it or merely adopted it.
        #   2. Otherwise adopt an existing chatgpt.com tab (no new tab).
        #   3. Otherwise create a new owned tab via Target.createTarget.
        #   4. Fallback: attach to any available page tab (shared mode).
        ws_url = None
        if self._target_id:
            # Reuse the tab we already attached to on a prior connect attempt.
            ws_url = self._find_owned_tab_ws()
            if ws_url:
                logger.info("Reusing tab: %s", self._target_id)
        if not ws_url:
            # Try to adopt an existing chatgpt.com tab (no new tab created).
            ws_url = self._adopt_existing_chatgpt_tab()
        if not ws_url:
            # No reusable tab — create a new one.
            try:
                ws_url = await self._create_owned_tab()
                logger.info("Connected via owned tab: %s", self._target_id)
            except Exception as e:
                logger.warning("Tab isolation failed (%s) — falling back to shared tab", e)
                self._target_id = None
                self._owns_target = False
                ws_url = await self._find_page_ws()
        self._ws = await websockets.connect(
            ws_url, max_size=100 * 1024 * 1024,
            ping_interval=20, ping_timeout=10,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info("CDP connected to Chrome")
        # Wait for the freshly-grabbed tab to actually be on chatgpt.com before
        # fetching the token — see _wait_for_chatgpt_ready. Without this the
        # fetch races the page load and returns an empty accessToken, killing
        # the MCP process on startup. Best-effort: a False return falls through
        # to _refresh_token, which has its own retry loop as a safety net.
        await self._wait_for_chatgpt_ready()
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
                ws_url = None
                # Reuse priority mirrors connect() — never create a redundant
                # tab when a usable chatgpt.com tab already exists.
                if self._target_id:
                    ws_url = self._find_owned_tab_ws()
                    if ws_url:
                        logger.info("Re-finding tab: %s", self._target_id)
                if not ws_url:
                    ws_url = self._adopt_existing_chatgpt_tab()
                if not ws_url:
                    logger.info("No reusable tab — creating new one")
                    ws_url = await self._create_owned_tab()
                if not ws_url:
                    ws_url = await self._find_page_ws()
                self._ws = await websockets.connect(
                    ws_url, max_size=100 * 1024 * 1024,
                    ping_interval=20, ping_timeout=10,
                )
                self._reader_task = asyncio.create_task(self._reader_loop())
                # Same settle wait as connect() — the reconnected tab (re-found
                # or re-created) may have just navigated. See _wait_for_chatgpt_ready.
                await self._wait_for_chatgpt_ready()
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

    async def _browser_cdp(self, method: str, params: dict = None, timeout: float = 10) -> dict:
        """Send a browser-domain CDP command via a short-lived browser WS.

        Used for Target.createTarget and Target.closeTarget. Opens a fresh
        connection to the browser-level endpoint (/devtools/browser/...),
        sends one command, awaits the response, closes. Does NOT use the
        page-level _cdp/_reader_loop machinery — those are for the persistent
        page WS only.
        """
        version = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{self.port}/json/version"),
                timeout=5,
            ).read()
        )
        browser_ws_url = version["webSocketDebuggerUrl"]
        mid = self._msg_id + 100000  # offset to avoid collision with page-level ids
        async with websockets.connect(browser_ws_url, max_size=10 * 1024 * 1024) as bws:
            await bws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(bws.recv(), timeout=max(1, deadline - time.monotonic()))
                resp = json.loads(raw)
                if resp.get("id") == mid:
                    return resp
            raise TimeoutError(f"Browser CDP timeout: {method}")

    async def _create_owned_tab(self) -> str:
        """Create a new chatgpt.com tab and return its page WS URL.

        Calls Target.createTarget via the browser WS, stores the targetId,
        then looks up the new tab's webSocketDebuggerUrl via /json/list.
        Returns the page WS URL. Sets self._target_id.
        """
        resp = await self._browser_cdp("Target.createTarget", {"url": "https://chatgpt.com/"})
        if "error" in resp:
            raise RuntimeError(f"Target.createTarget failed: {resp['error']}")
        self._target_id = resp.get("result", {}).get("targetId")
        if not self._target_id:
            raise RuntimeError("Target.createTarget returned no targetId")
        self._owns_target = True  # we created it → close() will tear it down
        logger.info("Created owned tab: %s", self._target_id)
        # Wait for the tab to appear in /json/list, then get its WS URL
        for _ in range(20):
            targets = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{self.port}/json/list"),
                    timeout=5,
                ).read()
            )
            for t in targets:
                if t.get("id") == self._target_id:
                    ws_url = t.get("webSocketDebuggerUrl")
                    if ws_url:
                        logger.info("Owned tab WS: %s", ws_url[:80])
                        return ws_url
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Created tab {self._target_id} but couldn't find its WS URL")

    def _find_owned_tab_ws(self) -> Optional[str]:
        """Look up an owned tab's WS URL from /json/list. Returns None if gone."""
        try:
            targets = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{self.port}/json/list"),
                    timeout=5,
                ).read()
            )
            for t in targets:
                if t.get("id") == self._target_id:
                    return t.get("webSocketDebuggerUrl")
        except Exception:
            pass
        return None

    def _adopt_existing_chatgpt_tab(self) -> Optional[str]:
        """Find an existing chatgpt.com tab in /json/list to adopt.

        ``Target.createTarget`` always opens a new tab, but at startup Chrome
        is typically already on chatgpt.com (the launch URL) and/or a prior
        service run left an owned tab behind. Reusing one of those instead of
        creating yet another keeps the tab count stable across restarts.

        Adopts (in priority order):
          1. A tab we previously owned (id == self._target_id).
          2. The first chatgpt.com page target with a live WS URL.

        Returns the WS URL and sets self._target_id / self._owns_target on a
        hit; returns None when no suitable tab exists (caller should create
        one). Never raises — a /json/list failure collapses to None.
        """
        try:
            targets = json.loads(
                urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{self.port}/json/list"),
                    timeout=5,
                ).read()
            )
        except Exception:
            return None

        # 1. A previously-owned tab we can re-attach to.
        if self._target_id:
            for t in targets:
                if t.get("id") == self._target_id:
                    ws_url = t.get("webSocketDebuggerUrl")
                    if ws_url:
                        # Ownership state is preserved — _owns_target unchanged.
                        return ws_url

        # 2. Any existing chatgpt.com page tab. Adopting it flips ownership to
        #    False so close() will NOT close it (it's not ours to close).
        for t in targets:
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            title = t.get("title", "")
            if "chatgpt.com" not in url and "chatgpt.com" not in title:
                continue
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            self._target_id = t.get("id")
            self._owns_target = False
            logger.info(
                "Adopted existing chatgpt.com tab: %s (will not close on shutdown)",
                self._target_id,
            )
            return ws_url

        return None

    async def _wait_for_chatgpt_ready(self) -> bool:
        """Wait for the connected tab to actually be on chatgpt.com.

        ``connect``/``reconnect`` grab a page websocket whose target exists
        milliseconds after ``Target.createTarget`` — before the page has
        navigated to chatgpt.com. A relative ``fetch('/api/auth/session')``
        fired against that cold tab resolves against the wrong origin (e.g.
        ``about:blank``) and returns an empty accessToken, tripping the auth
        gate and killing the MCP process on startup.

        Polls until ``location.href`` is on chatgpt.com AND ``readyState`` is
        past 'loading'. The token fetch only needs the page to be on the right
        origin with cookies attached — the full SPA (#prompt-textarea) is not
        required, so this is lighter than the ``navigate_*`` readiness checks.

        Mirrors ``_wait_for_login`` (conftest.py): uses the soft ``_js``
        evaluator so a transient CDP error collapses to '' instead of aborting,
        and never raises — a False return falls through to ``_refresh_token``,
        whose own retry loop is the safety net.

        Returns True if ready within the deadline, False on timeout.
        """
        deadline = time.monotonic() + _CONNECT_READY_TIMEOUT
        while time.monotonic() < deadline:
            try:
                raw = await self._js(
                    "(function(){"
                    "  return JSON.stringify({"
                    "    href: location.href,"
                    "    ready: document.readyState"
                    "  });"
                    "})()"
                )
                state = json.loads(raw) if raw else {}
                if (
                    "chatgpt.com" in (state.get("href") or "")
                    and state.get("ready") != "loading"
                ):
                    return True
            except (ValueError, TypeError):
                pass
            await asyncio.sleep(0.5)
        logger.warning(
            "Owned tab did not report chatgpt.com ready within %ds — "
            "proceeding (token refresh will retry)",
            _CONNECT_READY_TIMEOUT,
        )
        return False

    async def _refresh_token(self) -> None:
        """Get a fresh access token from /api/auth/session, with retry.

        The fetch can transiently return an empty accessToken when the page
        hasn't fully settled (cold tab after createTarget, or a navigation in
        flight). Retrying a few times with a short backoff lets the page catch
        up rather than failing the whole connect/reconnect/ensure_token path.
        This is the single chokepoint for all three callers, so the retry
        covers startup and mid-session refresh alike.

        On final failure raises the same RuntimeError every existing caller
        already handles — error semantics are unchanged.
        """
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                raw = await self._js(
                    "(async () => {"
                    "  const r = await fetch('/api/auth/session', {credentials:'include'});"
                    "  const d = await r.json();"
                    "  return JSON.stringify({token: d.accessToken || '', user: d.user?.name || ''});"
                    "})()"
                )
                # _js may return a dict (CDP returnByValue parsed the JSON
                # object) or a string (the JSON.stringify result). Handle both.
                if isinstance(raw, dict):
                    data = raw
                elif isinstance(raw, str):
                    data = json.loads(raw)
                else:
                    data = {"token": ""}
                self._access_token = data.get("token", "")
                self._user_name = data.get("user", "")
                self._token_fetched_at = time.time()
                if self._access_token:
                    logger.info(
                        "Auth: %d chars, user: %s (attempt %d)",
                        len(self._access_token), self._user_name, attempt,
                    )
                    return
                last_error = RuntimeError(
                    "No access token — not logged into ChatGPT"
                )
            except Exception as e:
                # JSON parse error, CDP blip, etc. — record and retry. Don't
                # clobber a partial _access_token from a prior good fetch.
                last_error = e
            if attempt < 3:
                await asyncio.sleep(0.5)
        raise last_error if last_error else RuntimeError(
            "No access token — not logged into ChatGPT"
        )

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

        # Wait for the composer. The new composer is a contenteditable
        # ProseMirror div (#prompt-textarea is now a hidden fallback);
        # COMPOSER_SELECTOR matches the real textbox, with the legacy
        # textarea as a last resort for older deployments.
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                f"    ready: !!document.querySelector('{COMPOSER_SELECTOR}') || !!document.querySelector('{COMPOSER_FALLBACK_SELECTOR}'),"
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
        """Navigate to an existing conversation for multi-turn.

        Sets ``self._current_conv_id`` ONLY after the live tab is verified
        to be at ``/c/{conversation_id}`` with the composer ready. On a
        verified failure (wrong landing URL, or readiness never observed)
        clears any stale ``_current_conv_id`` matching the request and
        raises — never admits an unverified conversation as current. This
        is the invariant the auto-continue paths depend on: ``_current_conv_id``
        means "the live tab is here", not "we attempted to go here".
        """
        url = f"https://chatgpt.com/c/{conversation_id}"
        logger.info("Navigate to conversation: %s", url)
        await self._cdp("Page.navigate", {"url": url})
        await asyncio.sleep(3)

        # Wait for composer (new ProseMirror div, or legacy textarea) AND a
        # verified landing. A for/else means: if the loop completes without
        # `break` (never became ready at the right URL), the else runs and we
        # fail rather than falling through to admit an unverified conversation.
        for _ in range(30):
            result = await self._js(
                "(function() {"
                "  return JSON.stringify({"
                f"    ready: !!document.querySelector('{COMPOSER_SELECTOR}') || !!document.querySelector('{COMPOSER_FALLBACK_SELECTOR}'),"
                "    url: location.href"
                "  });"
                "})()"
            )
            try:
                state = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                state = {}
            if state.get("ready") and self._is_url_at_conversation(
                state.get("url", ""), conversation_id
            ):
                logger.info("Conversation ready: %s", state.get("url", ""))
                break
            await asyncio.sleep(0.5)
        else:
            # Loop exhausted without a verified landing. Clear any stale
            # state that might point here so a later auto-continue can't
            # reuse a known-unverified id, then surface the failure.
            if self._current_conv_id == conversation_id:
                self._current_conv_id = None
            raise RuntimeError(
                f"Navigation to {conversation_id} did not reach a ready "
                f"composer within the timeout"
            )

        await asyncio.sleep(1)
        self._current_conv_id = conversation_id

    @staticmethod
    def _is_url_at_conversation(url: str, conversation_id: str) -> bool:
        """Exact path-segment match: is *url* at ``/c/{conversation_id}``?

        Uses urllib to parse the path and compare the second segment, so a
        high-entropy id can't accidentally match as a substring of another
        path. Query strings and trailing slashes are tolerated; a different
        conversation id or a non-conversation URL returns False.
        """
        if not url or not conversation_id:
            return False
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return False
        if "chatgpt.com" not in (parsed.netloc or "").lower():
            return False
        parts = [p for p in parsed.path.split("/") if p]
        # Expected shape: ["c", "<conversation_id>"]
        return len(parts) >= 2 and parts[0] == "c" and parts[1] == conversation_id

    async def _is_live_conversation_url(self, conversation_id: str) -> bool:
        """Read ``location.href`` and check it is at *conversation_id*.

        Returns False on any read/parse failure rather than raising — callers
        that need fail-closed behavior use ``ensure_current_conversation``,
        which turns an unreadable URL into a navigation attempt.
        """
        try:
            url = await self._js_strict("location.href")
        except CDPJSError:
            return False
        return self._is_url_at_conversation(url or "", conversation_id)

    async def ensure_current_conversation(self, conversation_id: str) -> None:
        """Guarantee the live tab is at *conversation_id* before sending.

        If the live URL already matches, returns without navigating. Otherwise
        navigates and verifies the landing. Raises if the tab cannot be brought
        to the requested conversation — fail-closed, never silently proceeding
        into an unknown tab state. ``_current_conv_id`` is only set on success
        (by ``navigate_conversation``); on failure it is cleared if it matched.
        """
        if await self._is_live_conversation_url(conversation_id):
            return
        await self.navigate_conversation(conversation_id)
        # navigate_conversation raises on failure, so reaching here means it
        # verified the landing. Belt-and-braces: re-check before returning.
        if not await self._is_live_conversation_url(conversation_id):
            if self._current_conv_id == conversation_id:
                self._current_conv_id = None
            raise RuntimeError(
                f"Failed to restore conversation context: {conversation_id}"
            )

    # ── Message Input ─────────────────────────────────────────

    async def type_message(self, text: str) -> None:
        """Type text into the ChatGPT composer.

        The new composer is a contenteditable ProseMirror div; the legacy
        composer was a <textarea id="prompt-textarea">. We focus the new
        textbox first (COMPOSER_SELECTOR), falling back to the textarea
        for older deployments. Once focused, ``Input.insertText`` routes
        the text to whichever element holds focus, so the same insert
        works for both layouts.
        """
        # Focus the composer. Try the ProseMirror textbox first, then the
        # legacy textarea fallback. Returns which one was focused (or
        # 'no composer') so the verify step reads the right element.
        focus_result = await self._js(
            "(function() {"
            f"  var el = document.querySelector('{COMPOSER_SELECTOR}');"
            "  if (el) { el.focus(); return 'composer'; }"
            f"  var fb = document.querySelector('{COMPOSER_FALLBACK_SELECTOR}');"
            "  if (fb) { fb.focus(); return 'fallback'; }"
            "  return 'no composer';"
            "})()"
        )
        if focus_result == "no composer":
            await self._capture_selector_diagnostic("composer (type_message)")
            raise RuntimeError("No composer found")
        focused_target = focus_result  # 'composer' or 'fallback'

        # Clear existing text by selecting all first. Works for both the
        # contenteditable div and the textarea (both honor Select-All).
        await self._cdp("Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2})
        await self._cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "modifiers": 2})
        await asyncio.sleep(0.1)

        # Insert text via CDP — dispatched to the focused element, which
        # is the composer regardless of layout.
        await self._cdp("Input.insertText", {"text": text})
        await asyncio.sleep(0.5)

        # Verify — read text from whichever element we focused, using
        # _js_strict so a CDP/JS error surfaces as the real cause rather
        # than a generic "Failed to insert text".
        verify_selector = COMPOSER_SELECTOR if focused_target == "composer" else COMPOSER_FALLBACK_SELECTOR
        try:
            content = await self._js_strict(
                f"(document.querySelector('{verify_selector}')?.textContent || '')"
            )
        except CDPJSError as e:
            raise RuntimeError(f"Failed to verify text insertion: {e}") from e
        if not content:
            raise RuntimeError("Failed to insert text into composer")
        logger.info("Typed: %s", text[:80])

    async def click_send(self) -> None:
        """Click the send button via JS MouseEvent sequence.

        The new composer has no ``data-testid="send-button"``; its send
        affordance is the submit ``<button aria-label="Send ...">`` inside
        the composer form. We prefer that, falling back to the legacy
        testid selector for older deployments. The stop button (which
        appears during generation) is explicitly excluded — it also
        carries an aria-label, but never "Send".
        """
        # Wait for the send button to appear and be enabled. Try the new
        # aria-label selector first, then the legacy testid fallback.
        for _ in range(10):
            has_btn = await self._js(
                "(function() {"
                f"  var btn = document.querySelector('{SEND_BUTTON_SELECTOR}')"
                f"       || document.querySelector('{SEND_BUTTON_FALLBACK_SELECTOR}');"
                "  return btn && !btn.disabled ? 'yes' : 'no';"
                "})()"
            )
            if has_btn == "yes":
                break
            await asyncio.sleep(0.3)

        result = await self._js(
            "(function() {"
            f"  var btn = document.querySelector('{SEND_BUTTON_SELECTOR}')"
            f"       || document.querySelector('{SEND_BUTTON_FALLBACK_SELECTOR}');"
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
        # (PHASE_STALL_SECONDS) catches a stuck generation: if NO DOM progress
        # occurs for longer than the stall window, we raise GenerationStuckError.
        #
        # Progress is tracked on THREE signals, not just text, so non-text
        # responses (images, tool-use, code interpreter) don't falsely stall:
        #   - text:       .markdown textContent (streamed as deltas for text)
        #   - html_len:   assistant message innerHTML length (grows when img/
        #                 canvas/tool-use elements are added)
        #   - child_count: direct children count (grows when new blocks render)
        # Any of these changing resets the stall clock.
        #
        # Done detection: Stop button gone AND there's meaningful content
        # (either .markdown text OR non-trivial HTML footprint). The threshold
        # (> 50 chars) prevents false 'done' from an empty/partial node.
        last_dom_text = ""
        last_html_len = 0
        last_child_count = 0
        had_non_text_content = False
        # Completion detection for Phase-2. The history here matters — three
        # earlier signals each failed in live testing, all producing an
        # off-by-one where request N returned request N-1's text:
        #   1. ``done = !stopBtn && hasContent`` — broke on the FIRST poll.
        #      Right after send the Stop button hasn't appeared yet (generation
        #      not begun) but html_len > 50 (the message wrapper), so this was
        #      True immediately, leaving last_dom_text empty.
        #   2. ``generation_started && not is_generating`` — the Stop button
        #      FLICKERS off between token batches, breaking mid-generation with
        #      truncated text.
        #   3. Text-stability alone — ``.markdown`` textContent is empty during
        #      streaming (text renders elsewhere until the turn settles), so
        #      "stable empty" never completes and the stall detector fires.
        #
        # The robust signal is the per-turn ACTION BUTTON. ChatGPT renders a
        # copy/feedback action row (data-testid containing "copy" or
        # "response-turn") on an assistant message ONLY once it has finished
        # generating — it is absent while the message is streaming or thinking.
        # Polling for that button on the NEW message is immune to the Stop
        # flicker and to the empty-.markdown-during-streaming quirk. Text is
        # captured from the message's innerText (which IS populated during
        # streaming) rather than .markdown textContent (which lags).
        last_change_time = time.monotonic()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = await self._js_strict(
                    "(function() {"
                    "  var msgs = document.querySelectorAll('[data-message-author-role=\"assistant\"]');"
                    "  if (!msgs.length) return JSON.stringify({text:'', md_text:'', html_len:0, child_count:0, has_action:false, is_thinking:false});"
                    "  var last = msgs[msgs.length - 1];"
                    # Text: the clean answer lives in ``.markdown`` textContent.
                    # It's empty during streaming and populates as the turn
                    # settles — so we ALSO capture ``innerText`` (populated
                    # during streaming) as a fallback. innerText includes the
                    # reasoning UI label ("Thinking.../Thought for N seconds"),
                    # so md_text is captured SEPARATELY and Python prefers it;
                    # the innerText fallback is trimmed of the leading label.
                    "  var md = last.querySelector('.markdown');"
                    "  var mdText = md ? (md.textContent || '') : '';"
                    "  var rawText = (last.innerText || '').trim();"
                    # Strip a leading "Thinking..." / "Thought for …" reasoning
                    # label so the innerText fallback can't leak it as a delta.
                    "  var text = mdText || rawText.replace(/^Think(ing|\\s+for)[^\\n]*\\n?/i, '');"
                    "  var html_len = last.innerHTML.length;"
                    "  var child_count = last.children.length;"
                # has_action: the per-turn copy/feedback button appears only
                # on a completed message. Match common action-button testids.
                "  var has_action = !!("
                "    last.querySelector('[data-testid*=\"copy\"]')"
                "    || last.querySelector('[data-testid*=\"response-turn\"]')"
                "  );"
                    # is_thinking: the active-reasoning indicator. Narrowed to
                    # ``.result-thinking`` AND ``!has_action`` — the action
                    # button marks a finished turn, and ``.result-thinking``
                    # lingers in the DOM after completion as a collapsed
                    # "Thought process" section. WITHOUT the has_action gate
                    # this stayed true forever, and the old ``/thinking/i``
                    # word-match on innerText matched the persistent
                    # "Thought for N seconds" summary label — together they
                    # pinned is_thinking=true on every thinking-model turn
                    # and on any answer that mentioned the word "thinking",
                    # which suppressed all delta emission (see the elif below)
                    # and produced empty responses when _fetch_text lagged.
                    "  var is_thinking = !has_action && !!last.querySelector('.result-thinking');"
                    "  return JSON.stringify({text: text, md_text: mdText, html_len: html_len, child_count: child_count, has_action: has_action, is_thinking: is_thinking});"
                    "})()",
                )
                data = json.loads(result)
            except (CDPJSError, json.JSONDecodeError, TypeError):
                await asyncio.sleep(0.5)
                continue

            current = data.get("text", "")
            md_text = data.get("md_text", "")
            html_len = data.get("html_len", 0)
            child_count = data.get("child_count", 0)
            has_action = data.get("has_action", False)
            is_thinking = data.get("is_thinking", False)

            # Streaming source: prefer the clean .markdown answer container
            # over the innerText fallback (which carries the reasoning label).
            # When md_text is empty (early streaming, before .markdown fills),
            # the innerText fallback (with its leading label already stripped
            # in JS) is what carries the streamed answer.
            current = md_text or current

            # is_thinking means the model is actively reasoning — the DOM is
            # legitimately static for tens of seconds, which is NOT a stall.
            # It MUST reset the stall clock. But — critically — it must NOT
            # block delta emission: after reasoning ends there's a gap where
            # is_thinking is still true (.result-thinking lingers as a
            # collapsed "Thought process" section) yet the answer is actively
            # streaming. The old ``if is_thinking / elif text-changed``
            # structure made the two mutually exclusive, so is_thinking=true
            # suppressed all deltas, freezing last_dom_text="" and yielding an
            # empty response whenever _fetch_text lagged. The reset and the
            # stream are independent concerns — handle them independently.
            if is_thinking:
                last_change_time = time.monotonic()
            if current != last_dom_text:
                last_change_time = time.monotonic()
                if len(current) > len(last_dom_text):
                    delta = current[len(last_dom_text):]
                    yield StreamChunk(delta=delta)
                last_dom_text = current

            # Non-text progress signals (images, tool-use, etc.)
            if html_len != last_html_len or child_count != last_child_count:
                last_change_time = time.monotonic()
                if html_len > 50:
                    had_non_text_content = True
            last_html_len = html_len
            last_child_count = child_count

            # Done: the new message has its action button (copy/feedback),
            # which ChatGPT renders only on a finished turn. This is immune to
            # the Stop-button flicker and the empty-.markdown-during-streaming
            # quirk that broke the earlier heuristics.
            if has_action:
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
            # If no text was captured but Phase-2 detected non-text content
            # (image, tool-use, etc.), surface a placeholder so the agent
            # knows something was generated and where to find it.
            if not last_dom_text and had_non_text_content:
                placeholder = (
                    "[Non-text response generated (image/tool-use/etc.) — "
                    "use get_conversation to retrieve full content.]"
                )
                yield StreamChunk(delta=placeholder)

        yield StreamChunk(delta="", finish_reason="stop")

    async def _fetch_text(self, conversation_id: str) -> str:
        """Fetch the latest assistant text from the conversation API.

        Non-OK responses are encoded by the JS as ``{"__status": <code>}``
        rather than ``''`` so Python can distinguish an auth failure (401 →
        AuthExpiredError) from a missing conversation (404) or a network
        error. This parse-and-raise happens here, before any return reaches
        the caller, so callers never see a raw status blob as text.

        Picks the newest assistant text message by ``create_time`` rather than
        trusting the API's ``current_node`` pointer: that pointer lags behind
        on continued conversations (it still points at the previous turn right
        after a send), which produced an off-by-one where request N returned
        request N-1's text. The newest-by-create-time selection is immune to
        that lag.
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
                # Find the NEWEST assistant text message by create_time.
                # current_node lags on continued conversations, so we cannot
                # trust it to point at the turn we just sent.
                "    var best = null;"
                "    var bestTime = -1;"
                "    for (var k in mapping) {"
                "      var n = mapping[k];"
                "      var m = n.message;"
                "      if (!m || !m.author || m.author.role !== 'assistant') continue;"
                "      if (!m.content || m.content.content_type !== 'text') continue;"
                "      var parts = m.content.parts || [];"
                "      if (!parts.length || !parts[0]) continue;"
                "      var t = m.create_time || 0;"
                "      if (t >= bestTime) { bestTime = t; best = parts[0]; }"
                "    }"
                "    return best || '';"
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
                f"    ready: !!document.querySelector('{COMPOSER_SELECTOR}') || !!document.querySelector('{COMPOSER_FALLBACK_SELECTOR}'),"
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
        # Only close the attached tab if WE created it. An adopted tab
        # (Chrome's launch tab, a leftover from a prior run, or a tab the
        # user opened) is left alone — closing it would accumulate negative
        # side-effects (killing a tab the user expects to stay open).
        if self._target_id and self._owns_target:
            try:
                await self._browser_cdp(
                    "Target.closeTarget", {"targetId": self._target_id}
                )
                logger.info("Closed owned tab: %s", self._target_id)
            except Exception as e:
                logger.debug("Could not close owned tab %s: %s", self._target_id, e)
        elif self._target_id and not self._owns_target:
            logger.info("Leaving adopted tab open: %s", self._target_id)
        self._target_id = None
        self._owns_target = False
        logger.info("CDP driver closed")

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

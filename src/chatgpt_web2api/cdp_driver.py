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
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .breakers import BreakerKind, BreakerRegistry
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
    finish_reason: str | None = None


# Conservative fallback wait (seconds) when ChatGPT's pop-up gives no exact
# number (it usually says "a few minutes"). Chosen to be long enough to let
# a real cooldown clear but short enough that a transient blip recovers fast.
RATE_LIMIT_DEFAULT_RETRY_AFTER = 60

# Re-exported from backend_client (Phase 5 PR1 extraction) for back-compat.
# Canonical home is now backend_client.py.
from .backend_client import TOKEN_TTL_SECONDS  # noqa: E402,F401

# Phase 5 PR4: generation-completion stall window + rate-limit pop-up text
# matcher extracted into completion_detector.py; re-exported here for back-compat
# (is_rate_limited_text is imported from cdp_driver by api_server, chatgpt_dom,
# and tests). _RATE_LIMIT_PHRASES stays private to completion_detector.
from .completion_detector import (  # noqa: E402,F401
    PHASE_STALL_SECONDS,
    is_rate_limited_text,
)

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

# ChatGPT composer / send-button selectors.
#
# Canonical home moved to chatgpt_dom.py in Phase 5 PR3; re-exported here for
# back-compat (tests import these from cdp_driver, and the navigation methods
# that stay here still reference them).
from .chatgpt_dom import (  # noqa: E402,F401
    COMPOSER_FALLBACK_SELECTOR,
    COMPOSER_SELECTOR,
    SEND_BUTTON_FALLBACK_SELECTOR,
    SEND_BUTTON_SELECTOR,
)


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
            message = f"ChatGPT rate limit reached (Too many requests). Retry in {retry_after}s."
        super().__init__(message)
        self.retry_after = int(retry_after)

    @classmethod
    def from_text(cls, text: str) -> RateLimitError:
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


class SendReadinessError(RuntimeError):
    """Raised when the composer / send-readiness path fails — no composer found,
    the composer wouldn't focus, or the send button didn't fire.

    Typed (not bare ``RuntimeError``) so the breaker wiring can classify it
    explicitly at the catch site as ``BreakerKind.COMPOSER_SEND_READINESS``
    rather than guessing from a string. Raised by ``_ensure_send_ready``,
    ``type_message``, and ``click_send``.
    """


class CDPReconnectError(RuntimeError):
    """Raised when CDP reconnect exhausts its 3-attempt backoff without
    re-establishing the websocket.

    Typed (not bare ``RuntimeError``) so the breaker wiring can classify it
    explicitly as ``BreakerKind.CDP_RECONNECT``.
    """


# Phrases ChatGPT uses in its rate-limit pop-up + the ``is_rate_limited_text``
# matcher moved to completion_detector.py (Phase 5 PR4); re-exported above.

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
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    def _to_num(token: str) -> int | None:
        if token.isdigit():
            return int(token)
        return _NUM_WORDS.get(token)

    # "<n> minute(s)" → seconds = n * 60
    m = re.search(
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:minutes?|mins?)", lowered
    )
    if m:
        n = _to_num(m.group(1))
        if n is not None:
            return n * 60

    # "<n> second(s)" / "<n> sec(s)"
    m = re.search(
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:seconds?|secs?)", lowered
    )
    if m:
        n = _to_num(m.group(1))
        if n is not None:
            return n

    return default


class CDPDriver:
    """Chrome DevTools Protocol driver for ChatGPT automation."""

    def __init__(
        self,
        cdp_port: int = 9222,
        tab_mode: str = "owned",
        instance_id: str | None = None,
        breakers: BreakerRegistry | None = None,
    ) -> None:
        self.port = cdp_port
        # Tab isolation strategy: "owned" creates a dedicated chatgpt.com tab
        # per driver (multi-session safe — two drivers get two DOMs). "adopt"
        # reuses an existing chatgpt.com tab (single-process compat). The
        # default is "owned" because adoption lets one session navigate
        # another's shared tab out from under it. See connect().
        self.tab_mode = tab_mode if tab_mode in ("owned", "adopt") else "owned"
        # Owned-tab registry (R3): persists this instance's owned tab so a
        # restarted process reclaims its OWN prior tab instead of orphaning it
        # and creating a new one. Reclaim is instance-scoped (never cross-
        # session adoption) and lease-protected (never steals a live owner's
        # tab). None disables the registry (e.g. adopt mode, tests).
        from .tab_registry import TabRegistry

        self.instance_id = instance_id or TabRegistry.derive_instance_id(cdp_port=cdp_port)
        self._tab_registry = TabRegistry(self.instance_id) if tab_mode == "owned" else None
        self._heartbeat_task: asyncio.Task | None = None
        self._ws = None
        self._msg_id = 0
        self._access_token = ""
        self._user_name = ""
        self._token_fetched_at: float = 0.0
        # Observability for refresh attempts distinct from the last *accepted*
        # token time. _token_fetched_at advances only on a non-empty token;
        # _last_refresh_attempt_at advances on every fetch attempt (success
        # or fail), so backoff/diagnostics can distinguish "stale token, last
        # refresh tried Ns ago" from "never refreshed."
        self._last_refresh_attempt_at: float = 0.0
        self._current_conv_id: str | None = None
        self._current_model: str | None = None
        # CDP response routing (#7): id-keyed futures + background reader
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        # Tab isolation: the targetId of the tab this driver is attached to.
        # _owns_target records whether *we* created it: only tabs we created are
        # closed in close(), so a driver that adopted an existing tab (e.g.
        # Chrome's launch tab) never closes a tab it didn't open — preventing
        # tab accumulation across service restarts while preserving the user's
        # open tabs on clean shutdown.
        self._target_id: str | None = None
        self._owns_target: bool = False
        # Phase 4 PR2: optional circuit-breaker registry. When set, failure
        # sites record/trip their kind and success sites clear failures /
        # recover half-open breakers. None = back-compat (tests, legacy
        # construction) — every recorder checks `if self._breakers:`.
        self._breakers = breakers
        # Phase 5 PR1: backend-api fetch helpers extracted into BackendClient.
        # Lazy import (like _tab_registry) to avoid load-time coupling; the
        # client holds a back-reference to this driver for transport + state.
        from .backend_client import BackendClient

        self._backend_client = BackendClient(self)
        # Phase 5 PR2: CDP wire primitives extracted into CDPTransport. Lazy
        # import for the same reason; the transport reaches through this driver
        # for _ws/_msg_id/_pending and calls back into reconnect() on socket death.
        from .cdp_transport import CDPTransport

        self._transport = CDPTransport(self)
        # Phase 5 PR3: ChatGPT composer DOM interaction extracted into
        # ChatGPTDom. Lazy import for the same reason; the DOM layer reaches
        # through this driver for _js/_cdp/_breakers and calls back into
        # navigate_new_chat() for the send-readiness path.
        from .chatgpt_dom import ChatGPTDom

        self._dom = ChatGPTDom(self)
        # Phase 5 PR4: streaming completion detection (Phase-1 appear loop +
        # Phase-2 stream loop) extracted into CompletionDetector. Lazy import
        # for the same reason; the detector reaches through this driver for
        # _js_strict, _fetch_end_turn, _get_live_conversation_id_best_effort,
        # and reads _current_conv_id (read-only — never assigned by the
        # detector). It yields delta chunks only; the terminal stop chunk and
        # the _current_conv_id mutation stay in send_and_stream.
        from .completion_detector import CompletionDetector

        self._completion = CompletionDetector(self)

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
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._reader_task = None

        # Resolve which tab to attach to, in priority order. The strategy is
        # governed by self.tab_mode:
        #
        #   "owned" (default, multi-session safe): each driver creates its own
        #     chatgpt.com tab via Target.createTarget. Two simultaneous drivers
        #     get two DOMs and cannot navigate each other's tab. Adoption is
        #     skipped unless _target_id is already set (reconnect/restart).
        #
        #   "adopt" (single-process compat): reuse an existing chatgpt.com tab
        #     when present (the pre-multi-session behavior). Cheaper on tab
        #     count, but two drivers adopting the same tab will contend on the
        #     shared DOM — only safe when you know there's a single driver.
        #
        #   1. Re-attach to a tab we already know about (_target_id set from a
        #      prior connect), whether we created it or adopted it. Both modes.
        #   2. owned mode → create a new owned tab.
        #      adopt mode → adopt an existing chatgpt.com tab, else create.
        #   3. Fallback (both modes): attach to any available page tab.
        ws_url = None
        if self._target_id:
            # Reuse the tab we already attached to on a prior connect attempt.
            ws_url = self._find_owned_tab_ws()
            if ws_url:
                logger.info("Reusing tab: %s", self._target_id)
        if not ws_url and self.tab_mode == "adopt":
            # Single-process compat: try to adopt an existing chatgpt.com tab.
            ws_url = self._adopt_existing_chatgpt_tab()
        if not ws_url:
            # Registry reclaim (R3): before creating a new tab, check if THIS
            # instance owned a tab in a prior run that's still alive. Reclaim
            # is instance-scoped and lease-protected — never cross-session
            # adoption, never steals a live owner's tab. Skipped in adopt mode.
            if self._tab_registry:
                try:
                    live_ids = await self._live_target_ids()
                    reclaimed = self._tab_registry.reclaim(live_ids)
                    if reclaimed:
                        self._target_id = reclaimed
                        self._owns_target = True
                        ws_url = self._find_owned_tab_ws()
                        if ws_url:
                            logger.info(
                                "Reclaimed owned tab from registry: %s (instance %s)",
                                reclaimed,
                                self.instance_id,
                            )
                except Exception as e:
                    logger.debug("Tab registry reclaim failed (will create new): %s", e)
        if not ws_url:
            # Default path (owned mode) and adopt-mode fallback: create a new
            # dedicated tab so this driver owns its own DOM.
            try:
                ws_url = await self._create_owned_tab()
                logger.info("Connected via owned tab: %s", self._target_id)
                # Record the new tab in the registry so a restart can reclaim it.
                if self._tab_registry and self._target_id:
                    try:
                        self._tab_registry.record(self._target_id)
                    except Exception as e:
                        logger.debug("Tab registry record failed: %s", e)
            except Exception as e:
                logger.warning("Tab isolation failed (%s) — falling back to shared tab", e)
                self._target_id = None
                self._owns_target = False
                ws_url = await self._find_page_ws()
        self._ws = await websockets.connect(
            ws_url,
            max_size=100 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=10,
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
        # Establish the send-readiness invariant before connect() returns: a
        # connected driver must be able to type a message. connect() may have
        # attached to a chatgpt.com/ home/landing tab (or adopted an arbitrary
        # existing tab) that is auth-valid but lacks the composer — without
        # this, the next type_message raises "No composer found" and surfaces
        # as an opaque 500. Done AFTER auth so we never navigate on an
        # unauthenticated page. Best-effort: a failure logs and falls through
        # (send_and_stream has its own defensive check); it does not abort
        # startup, since reads (list_models etc.) work without a composer.
        try:
            await self._ensure_send_ready()
        except Exception as e:
            logger.warning(
                "connect(): send-readiness not established (%s) — reads still "
                "work; sends will fail until the tab reaches a chat page",
                e,
            )
        # Start the heartbeat lease for our owned tab (R3), so a long
        # generation (60-90s) doesn't let the lease expire and let another
        # process reclaim our tab mid-stream. Background task, cancelled in
        # close(). Also opportunistically heartbeats on send/connect.
        self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        """Start the background heartbeat task for the owned-tab lease."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        if not self._tab_registry:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        """Refresh this instance's tab lease every HEARTBEAT_INTERVAL_SECONDS.

        Runs for the driver's lifetime so a 90s generation can't expire the
        60s TTL. Self-healing: a single heartbeat exception is logged and the
        loop continues — if the task died, the lease would expire and another
        process could reclaim our tab mid-session (ensure_current_conversation
        guards wrong-conversation sends, but not the tab being closed/reused).
        Only CancelledError (close/shutdown) stops the loop.
        """
        from .tab_registry import HEARTBEAT_INTERVAL_SECONDS

        try:
            while True:
                try:
                    await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                    self._tab_registry.heartbeat(self._target_id)
                except asyncio.CancelledError:
                    raise  # shutdown — let it propagate
                except Exception as e:
                    logger.warning("Heartbeat failed (will retry): %s", e)
        except asyncio.CancelledError:
            pass

    async def _live_target_ids(self) -> set[str]:
        """Return the set of currently-live page target IDs from /json/list."""
        import urllib.request

        try:
            loop = asyncio.get_event_loop()

            def _fetch():
                with urllib.request.urlopen(
                    f"http://localhost:{self.port}/json", timeout=5
                ) as resp:
                    import json as _json

                    targets = _json.loads(resp.read())
                return {t.get("id") for t in targets if t.get("type") == "page"}

            return await loop.run_in_executor(None, _fetch)
        except Exception:
            return set()

    def tab_status(self) -> dict:
        """Snapshot of this driver's tab/session state (R6 observability).

        Surfaced for logging at connect() and available for /health or
        debugging. Includes the registry entry (instance_id, target_id,
        heartbeat age) plus the live driver state (tab_mode, owns_target,
        current conversation).
        """
        status = {
            "tab_mode": self.tab_mode,
            "target_id": self._target_id,
            "owns_target": self._owns_target,
            "instance_id": self.instance_id,
            "conv_id": self._current_conv_id,
        }
        if self._tab_registry:
            status["registry"] = self._tab_registry.status()
        return status

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
            except (TimeoutError, asyncio.CancelledError):
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
                # Reuse priority mirrors connect(): re-attach to a known
                # _target_id (both modes), then honor tab_mode for the
                # create-vs-adopt decision.
                if self._target_id:
                    ws_url = self._find_owned_tab_ws()
                    if ws_url:
                        logger.info("Re-finding tab: %s", self._target_id)
                if not ws_url and self.tab_mode == "adopt":
                    ws_url = self._adopt_existing_chatgpt_tab()
                if not ws_url:
                    logger.info("No reusable tab — creating new one")
                    ws_url = await self._create_owned_tab()
                if not ws_url:
                    ws_url = await self._find_page_ws()
                self._ws = await websockets.connect(
                    ws_url,
                    max_size=100 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=10,
                )
                self._reader_task = asyncio.create_task(self._reader_loop())
                # Same settle wait as connect() — the reconnected tab (re-found
                # or re-created) may have just navigated. See _wait_for_chatgpt_ready.
                await self._wait_for_chatgpt_ready()
                await self._refresh_token()
                logger.info("CDP reconnected on attempt %d", attempt)
                # Success: clear CDP failure history and recover a half-open
                # breaker. Only after refresh_token succeeds — a reconnect that
                # reopens the socket but can't auth isn't a clean recovery.
                if self._breakers:
                    self._breakers.record_success(BreakerKind.CDP_RECONNECT)
                return
            except Exception as e:
                logger.warning("Reconnect attempt %d failed: %s", attempt, e)
                if attempt < 3:
                    await asyncio.sleep(delay)
        if self._breakers:
            self._breakers.record_failure(BreakerKind.CDP_RECONNECT)
        raise CDPReconnectError("CDP reconnect failed after 3 attempts")

    async def _find_page_ws(self) -> str:
        """Find a suitable page's websocket URL."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/list")
        with urllib.request.urlopen(req, timeout=5) as resp:
            targets = json.loads(resp.read())

        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("No browser pages found — is Chrome running with chatgpt.com?")

        # Prefer chatgpt.com page
        chatgpt = [
            t
            for t in pages
            if "chatgpt.com" in t.get("url", "") or "chatgpt.com" in t.get("title", "")
        ]
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
                raw = await asyncio.wait_for(
                    bws.recv(), timeout=max(1, deadline - time.monotonic())
                )
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

    def _find_owned_tab_ws(self) -> str | None:
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

    def _adopt_existing_chatgpt_tab(self) -> str | None:
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
                if "chatgpt.com" in (state.get("href") or "") and state.get("ready") != "loading":
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

        Delegated to BackendClient (Phase 5 PR1 extraction). Kept as a thin
        delegator so callers, reconnect/connect paths, and test stubs that
        patch ``driver._refresh_token`` keep working unchanged.
        """
        await self._backend_client._refresh_token()

    # ── CDP primitives ────────────────────────────────────────

    async def _reader_loop(self) -> None:
        """Background reader: sole consumer of self._ws.recv().

        Delegated to CDPTransport (Phase 5 PR2 extraction). Preserved exactly:
        sole ``_ws.recv()`` consumer, routes responses to ``_pending`` by id,
        fails all pending futures on socket close.
        """
        await self._transport._reader_loop()

    async def _cdp(
        self, method: str, params: dict = None, timeout: float = 15, _retry: bool = True
    ) -> dict:
        """Send a CDP command and await its response.

        Delegated to CDPTransport (Phase 5 PR2 extraction). Preserved exactly:
        id-keyed future routing, one reconnect-and-retry through
        ``self.reconnect()`` on socket death (Layer-2 breaker semantics stay
        there), ``_retry`` recursion guard.
        """
        return await self._transport._cdp(method, params, timeout, _retry)

    @staticmethod
    def _should_reconnect(exc: Exception) -> bool:
        """True for socket-death signatures; False otherwise.

        Delegated to CDPTransport (Phase 5 PR2 extraction). Pure classifier,
        no state."""
        from .cdp_transport import CDPTransport

        return CDPTransport._should_reconnect(exc)

    async def _js(self, expr: str, timeout: float = 15) -> str:
        """Soft ``Runtime.evaluate`` — returns "" on failure.

        Delegated to CDPTransport (Phase 5 PR2 extraction)."""
        return await self._transport._js(expr, timeout)

    async def _js_with_data(self, expr_template: str, data: dict, timeout: float = 15) -> str:
        """Evaluate JS with safely injected ``__D`` data variables (soft).

        Delegated to CDPTransport (Phase 5 PR2 extraction)."""
        return await self._transport._js_with_data(expr_template, data, timeout)

    async def _js_strict(self, expr: str, timeout: float = 15) -> str:
        """Strict ``Runtime.evaluate`` — raises CDPJSError on failure.

        Delegated to CDPTransport (Phase 5 PR2 extraction)."""
        return await self._transport._js_strict(expr, timeout)

    async def _js_with_data_strict(
        self, expr_template: str, data: dict, timeout: float = 15
    ) -> str:
        """Strict variant of _js_with_data — raises CDPJSError on failure.

        Delegated to CDPTransport (Phase 5 PR2 extraction)."""
        return await self._transport._js_with_data_strict(expr_template, data, timeout)

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
            logger.warning(
                "Model picker not found: %s — proceeding with active model", picker_clicked
            )
            return False

        # Wait for dropdown to appear
        await asyncio.sleep(0.8)

        # Find and click the target model item
        # The dropdown renders model items as buttons or list items with the slug
        result = await self._js_with_data(
            "(function() {"
            "  var items = document.querySelectorAll("
            '    \'button[data-testid*="model"], '
            '    \'[class*="model-item"], '
            '    \'[class*="modelOption"], '
            '    \'li[class*="model"], '
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
        logger.warning(
            "Model '%s' not found in picker: %s — proceeding with active model", slug, result
        )
        return False

    # ── Navigation ────────────────────────────────────────────

    async def navigate_new_chat(self, gizmo_id: str = None) -> None:
        """Navigate to a fresh chat. Optionally scope to a project gizmo."""
        if gizmo_id:
            url = f"https://chatgpt.com/g/{gizmo_id}/project"
        else:
            # The bare ``chatgpt.com/`` home shell renders only the hidden
            # fallback textarea (``name=prompt-textarea``, no id, not visible),
            # so neither COMPOSER_SELECTOR nor COMPOSER_FALLBACK_SELECTOR matches
            # and type_message fails with "No composer found". The
            # ``?model=auto`` query triggers the SPA to render the real
            # ProseMirror composer reliably. Verified live: bare home → no
            # composer after 20s; ``?model=auto`` → composer present.
            url = "https://chatgpt.com/?model=auto"
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
                        raise RuntimeError(f"Navigation landed on unexpected URL: {actual_url}")
                    logger.info("Page ready: %s", actual_url)
                    break
            except (json.JSONDecodeError, TypeError):
                pass
            await asyncio.sleep(0.5)

        # Settle time for sentinel init
        await asyncio.sleep(2)
        self._current_conv_id = None

    async def _has_composer(self) -> bool:
        """Is a send-capable composer present on the live tab?

        Delegated to ChatGPTDom (Phase 5 PR3 extraction)."""
        return await self._dom._has_composer()

    async def _ensure_send_ready(self) -> None:
        """Guarantee the live tab can accept a typed message.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction). Preserved exactly:
        poll-then-navigate-via-``navigate_new_chat``, COMPOSER_SEND_READINESS
        breaker record_failure on persistent failure (registry stays on driver).
        """
        await self._dom._ensure_send_ready()

    async def _wait_for_composer(self, timeout: float = 8) -> bool:
        """Poll until a composer appears, or *timeout* seconds elapse.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction)."""
        return await self._dom._wait_for_composer(timeout)

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
                f"Navigation to {conversation_id} did not reach a ready composer within the timeout"
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
            raise RuntimeError(f"Failed to restore conversation context: {conversation_id}")

    # ── Message Input ─────────────────────────────────────────

    async def type_message(self, text: str) -> None:
        """Type text into the ChatGPT composer.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction). Preserved exactly:
        focus → platform-aware select-all → CDP insertText → canonical verify
        with one retry; COMPOSER_SEND_READINESS breaker record_failure on
        persistent failure (registry stays on driver).
        """
        await self._dom.type_message(text)

    async def _detect_select_all_modifier(self) -> int:
        """Return the CDP modifiers value for select-all on the live platform.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction)."""
        return await self._dom._detect_select_all_modifier()

    async def _verify_composer_text(self, selector: str, expected: str) -> bool:
        """Canonical-equality check: does the composer hold *expected*?

        Delegated to ChatGPTDom (Phase 5 PR3 extraction)."""
        return await self._dom._verify_composer_text(selector, expected)

    async def click_send(self) -> None:
        """Click the send button via JS MouseEvent sequence.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction). Preserved exactly:
        aria-label-then-legacy selector, COMPOSER_SEND_READINESS breaker
        record_failure on miss / record_success on confirmed send (registry
        stays on driver)."""
        await self._dom.click_send()

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

        # Phase 5 PR4: the Phase-1 (assistant-node appear) and Phase-2 (DOM
        # streaming + completion-detection) loops were extracted verbatim into
        # CompletionDetector.stream_until_complete — a delta-only async
        # sub-generator. Re-yield its chunks with NO buffering / post-processing
        # so the public yield sequence stays byte-equivalent to pre-extraction.
        # The detector yields StreamChunk(delta=...) only; the terminal
        # finish_reason="stop" chunk is emitted by the tail below. The detector
        # raises RateLimitError / GenerationStuckError on the same conditions
        # the inline loops did; those propagate out of the async-for unchanged.
        async for chunk in self._completion.stream_until_complete(
            initial_count=initial_count,
            timeout=timeout,
        ):
            yield chunk


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
            # last_dom_text is the streamed-text baseline accumulated by the
            # Phase-2 loop in CompletionDetector; had_non_text_content flags an
            # image/tool-use turn. Both are surfaced as per-call results on the
            # detector (read here; the loop owned them pre-extraction).
            last_dom_text = self._completion.last_dom_text
            had_non_text_content = self._completion.had_non_text_content
            # Fetch final text from API (more reliable than DOM for thinking models)
            for _ in range(60):
                api_text = await self._fetch_text(conv_id)
                if api_text and len(api_text) > len(last_dom_text):
                    yield StreamChunk(delta=api_text[len(last_dom_text) :])
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

        Delegated to BackendClient (Phase 5 PR1 extraction). The 401→
        AuthExpiredError+trip behavior is preserved exactly; a transient 404
        (conversation not yet persisted after send) is now retried a bounded
        number of times before surfacing as RuntimeError (PR #23).
        """
        return await self._backend_client._fetch_text(conversation_id)

    async def _conversation_id_from_url(self) -> str:
        """Parse the conversation id from the live tab's location.href.

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client._conversation_id_from_url()

    async def _get_live_conversation_id_best_effort(self) -> str:
        """Resolve the in-flight conversation id by cheapest available source.

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client._get_live_conversation_id_best_effort()

    async def _fetch_end_turn(self, conversation_id: str) -> bool:
        """Backend secondary completion signal: is the latest assistant TEXT
        node marked ``end_turn === true``?

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client._fetch_end_turn(conversation_id)

    async def dismiss_rate_limit(self) -> bool:
        """Dismiss ChatGPT's 'Too many requests' pop-up by clicking 'Got it'.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction). Preserved exactly:
        text-targeted click + re-scan, tri-state return (True/False/None).
        """
        return await self._dom.dismiss_rate_limit()

    def _check_auth_in_raw(self, raw: str) -> None:
        """#20: Detect auth failure in raw response text and raise.

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        self._backend_client._check_auth_in_raw(raw)

    async def _capture_selector_diagnostic(self, selector_name: str) -> None:
        """#5: Capture DOM state when a selector fails to match.

        Delegated to ChatGPTDom (Phase 5 PR3 extraction). Best-effort — never
        raises."""
        await self._dom._capture_selector_diagnostic(selector_name)

    # ── API helpers ───────────────────────────────────────────

    @diagnose("get_models")
    async def get_models(self) -> list[dict]:
        """List available models.

        Delegated to BackendClient (Phase 5 PR1 extraction). @diagnose wraps
        the caller-facing entry point so timing/observability is unchanged.
        """
        return await self._backend_client.get_models()

    @diagnose("get_projects")
    async def get_projects(self) -> list[dict]:
        """List projects. Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client.get_projects()

    # ── Conversation Management ──────────────────────────────

    @diagnose("get_conversations")
    async def get_conversations(
        self,
        offset: int = 0,
        limit: int = 28,
        order: str = "updated",
    ) -> list[dict]:
        """List recent conversations. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.get_conversations(offset, limit, order)

    @diagnose("get_conversation")
    async def get_conversation(self, conversation_id: str) -> dict:
        """Get full conversation detail with message mapping.

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client.get_conversation(conversation_id)

    @diagnose("delete_conversation")
    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.delete_conversation(conversation_id)

    async def rename_conversation(self, conversation_id: str, title: str) -> bool:
        """Rename a conversation. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.rename_conversation(conversation_id, title)

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
        """Create a new ChatGPT project. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.create_project(name, instructions, memory_scope)

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
        """Update a project's custom instructions.

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client.update_project_instructions(project_id, instructions)

    async def get_project_detail(self, project_id: str) -> dict:
        """Get full project/gizmo detail. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.get_project_detail(project_id)

    # ── Archive Conversation ────────────────────────────────

    @diagnose(
        "archive_conversation",
        capture_js=lambda self: (
            "PATCH /backend-api/conversation/{id}",
            {"archive": "<arg>"},
        ),
    )
    async def archive_conversation(self, conversation_id: str, archive: bool = True) -> bool:
        """Archive or unarchive a conversation. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.archive_conversation(conversation_id, archive)

    # ── Memory Management ─────────────────────────────────────

    @diagnose("get_memories")
    async def get_memories(self) -> list[dict]:
        """List all ChatGPT memories. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.get_memories()

    @diagnose("create_memory")
    async def create_memory(self, content: str) -> dict:
        """Create a memory via chat. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.create_memory(content)

    @diagnose("delete_memory")
    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a ChatGPT memory by ID. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.delete_memory(memory_id)

    @diagnose("delete_project")
    async def delete_project(self, project_id: str) -> dict:
        """Delete a ChatGPT project by ID. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.delete_project(project_id)

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
        """List Custom GPTs (non-project gizmos). Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.list_gpts()

    # ── Project Files ─────────────────────────────────────────

    @diagnose("get_project_files")
    async def get_project_files(self, project_id: str) -> list[dict]:
        """List files attached to a ChatGPT project. Delegated to BackendClient (Phase 5 PR1)."""
        return await self._backend_client.get_project_files(project_id)

    # ── Token Management ──────────────────────────────────────

    async def ensure_token(self) -> str:
        """Ensure a non-stale access token, refreshing if empty OR older than TTL.

        Delegated to BackendClient (Phase 5 PR1 extraction)."""
        return await self._backend_client.ensure_token()

    # ── Lifecycle ─────────────────────────────────────────────

    async def close(self) -> None:
        # Stop the background reader first
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._reader_task = None
        # Stop the heartbeat lease task and clear our registry entry so a
        # future restart of THIS instance creates fresh rather than reclaiming
        # a tab we just closed.
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await asyncio.wait_for(self._heartbeat_task, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                pass
        self._heartbeat_task = None
        if self._tab_registry:
            try:
                # Only clear if the entry still belongs to us. If we crashed
                # earlier, went stale, and another process reclaimed our
                # instance's entry, unconditional clear would delete THEIR lease.
                self._tab_registry.clear_if_owner(self._target_id)
            except Exception as e:
                logger.debug("Tab registry clear failed: %s", e)
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
                await self._browser_cdp("Target.closeTarget", {"targetId": self._target_id})
                logger.info("Closed owned tab: %s", self._target_id)
            except Exception as e:
                logger.debug("Could not close owned tab %s: %s", self._target_id, e)
        elif self._target_id and not self._owns_target:
            logger.info("Leaving adopted tab open: %s", self._target_id)
        self._target_id = None
        self._owns_target = False
        logger.info("CDP driver closed")

    async def recover_auth(self) -> bool:
        """Probe whether the ChatGPT session is valid again, and if so reset
        the AUTH_EXPIRED breaker.

        Delegated to BackendClient (Phase 5 PR1 extraction). The 401
        AUTH_EXPIRED trip/reset semantics are preserved exactly.
        """
        return await self._backend_client.recover_auth()

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    # PR3/5: read-only owned-target state for the lock resolver + observability.
    # Backs ``has_owned_target``, which the resolver uses to decide per-target
    # vs port-wide locking in parallel mode. Mirrors the close() guard at
    # :1535 — "a driver that adopted a tab never closes a tab it didn't open."
    @property
    def target_id(self) -> str | None:
        """The owned tab's CDP targetId, or None if none owned/adopted."""
        return self._target_id

    @property
    def owns_target(self) -> bool:
        """True iff this driver created its target (owned mode), not adopted."""
        return self._owns_target

    @property
    def has_owned_target(self) -> bool:
        """True iff the driver holds a dedicated owned tab target.

        The condition the parallel-tabs lock resolver checks before granting a
        per-target lock: ``tab_mode == "owned"`` AND ``_owns_target`` AND a
        non-empty ``_target_id``.
        """
        return self.tab_mode == "owned" and self._owns_target and bool(self._target_id)

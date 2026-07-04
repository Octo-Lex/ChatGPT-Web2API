"""A2 identity listener — observes the client-generated message UUID from
ChatGPT's outgoing send POST and exposes it for turn correlation.

Investigation findings (Phase 1, peer-reviewed):
  - The send goes to ``POST /backend-api/f/conversation`` (note the ``/f/``
    prefix — fetch/stream).
  - The POST body's ``messages[0].id`` is a client-generated UUID that
    survives into the backend conversation mapping as the user node's
    ``message.id`` (exact match, verified 12/12).
  - ``Network.requestWillBeSent`` with ``maxPostDataSize=4MB`` carries the
    POST body in ``request.postData`` (100% capture, 12/12).
  - ``window.fetch`` runtime override does NOT work (the frontend captures
    the fetch reference at module load); CDP Network is the ONLY viable
    capture mechanism.

The listener is a target-lifecycle component owned by ``CDPDriver`` (not the
transport — the transport is wire-only). The driver attaches it on
``connect()``/``reconnect()`` and re-arms it on target drift. Per send, the
driver arms a capture scope before ``click_send`` and consumes the captured
UUID after the send fires (the UUID only exists in the POST that
``click_send`` generates).

Handler contract: the ``Network.requestWillBeSent`` handler registered here
is called synchronously from ``CDPTransport._reader_loop`` (the sole
``ws.recv()`` consumer). It MUST be fast: it does only a URL/method prefilter
synchronously and schedules the heavy POST-body parse via
``loop.create_task`` so the reader loop is never blocked on JSON parsing or
hashing of large request bodies.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# CDP Network.enable parameter for capturing POST bodies. Stress test 2
# verified no truncation up to 2MB with this value.
_MAX_POST_DATA_SIZE = 4 * 1024 * 1024

# The send endpoint. Note the /f/ prefix (fetch/stream).
_SEND_ENDPOINT_SUFFIX = "/backend-api/f/conversation"

# UUID v4 shape (8-4-4-4-12 hex). Used to validate messages[0].id.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


@dataclass
class CaptureResult:
    """The outcome of one capture scope."""

    uuid: str | None = None
    # Why capture failed/succeeded, for metrics/diagnostics.
    reason: str = ""
    # When multiple candidate POSTs matched, this records how we picked.
    candidate_count: int = 0
    request_id: str | None = None


class CaptureScope:
    """Per-send capture scope, armed before ``click_send`` and closed in
    ``finally`` on every terminal path.

    Closing the scope clears the armed state so a late POST from a failed
    send cannot contaminate the next send (failure-mode E). The scope holds
    a future that ``wait_for_captured_uuid`` awaits; the handler resolves it
    when a matching POST is observed or the scope is closed without a match.
    """

    def __init__(
        self,
        listener: IdentityListener,
        expected_text_hash: str,
        conversation_id: str | None,
        target_id: str | None,
        send_sequence_id: int,
    ) -> None:
        self._listener = listener
        self.expected_text_hash = expected_text_hash
        self.conversation_id = conversation_id
        self.target_id = target_id
        self.send_sequence_id = send_sequence_id
        self._future: asyncio.Future[CaptureResult] | None = None
        self._closed = False

    def _arm(self) -> None:
        """Resolve the wait future when a result lands or the scope closes."""
        self._future = asyncio.get_event_loop().create_future()

    @property
    def future(self) -> asyncio.Future[CaptureResult] | None:
        return self._future

    def close(self) -> None:
        """Clear armed state. Safe to call multiple times (idempotent)."""
        if self._closed:
            return
        self._closed = True
        # If wait_for_captured_uuid is still awaiting, unblock it with no UUID.
        if self._future is not None and not self._future.done():
            self._future.set_result(CaptureResult(reason="scope_closed"))
        self._listener._clear_active_scope(self)

    def _resolve(self, result: CaptureResult) -> None:
        """Called by the listener when a matching POST is observed."""
        if self._future is not None and not self._future.done():
            self._future.set_result(result)


class IdentityListener:
    """Persistent per-target CDP Network listener for client-message-ID capture.

    Owned by ``CDPDriver`` (Layer 2). The driver attaches it on connect/reconnect
    and arms a per-send capture scope before each ``click_send``.

    The listener registers ONE event handler (``Network.requestWillBeSent``)
    on the driver's ``_cdp_event_handlers`` table (the generic dispatch
    mechanism added in Step 1). The handler is non-blocking: it does a fast
    URL/method prefilter and schedules the heavy parse via ``create_task``.
    """

    def __init__(self, driver) -> None:
        self._driver = driver
        self._ready = False
        self._active_scope: CaptureScope | None = None
        self._send_sequence = 0
        # Metrics counters (emitted via structured logs; full metric plumbing
        # comes with the observability pass).
        self.capture_success_count = 0
        self.capture_missed_count = 0
        self.capture_ambiguous_count = 0
        self.fallback_reasons: dict[str, int] = {}
        self.postdata_missing_count = 0
        self.reenabled_count = 0
        self.reconnect_count = 0

    # ── Lifecycle ──────────────────────────────────────────────

    async def attach(self) -> None:
        """Register the event handler and enable the Network domain.

        Called by the driver on connect/reconnect. Idempotent: safe to call
        when already attached (re-registers the handler, re-enables Network).
        """
        d = self._driver
        # Register the handler on the driver's dispatch table.
        d._cdp_event_handlers["Network.requestWillBeSent"] = self._on_request_will_be_sent
        # Enable the Network domain with POST-body capture.
        try:
            await d._cdp(
                "Network.enable",
                {"maxPostDataSize": _MAX_POST_DATA_SIZE},
                timeout=10,
            )
            self._ready = True
            self.reconnect_count += 1
            logger.info("identity_listener_ready (send_seq=%d)", self._send_sequence)
        except Exception as e:
            self._ready = False
            logger.warning("identity_listener_attach_failed: %s", e)

    def detach(self) -> None:
        """Unregister the handler (e.g., on close or before a fresh attach)."""
        d = self._driver
        d._cdp_event_handlers.pop("Network.requestWillBeSent", None)
        self._ready = False

    def is_alive(self) -> bool:
        """Health check: is the listener ready to capture?

        This is a liveness signal, not a guarantee that the next POST will be
        captured. The driver calls this before each send and calls
        ``reenable_if_stale`` if False.
        """
        return self._ready

    async def reenable_if_stale(self) -> bool:
        """Idempotently re-enable the Network domain if the listener is stale.

        Returns True if the listener is ready after this call. Used as a
        pre-send health check inside the MutationLock.
        """
        if self._ready:
            return True
        try:
            await self._driver._cdp(
                "Network.enable",
                {"maxPostDataSize": _MAX_POST_DATA_SIZE},
                timeout=10,
            )
            self._ready = True
            self.reenabled_count += 1
            logger.info("identity_listener_reenabled")
            return True
        except Exception as e:
            self._ready = False
            logger.warning("identity_listener_reenable_failed: %s", e)
            return False

    # ── Per-send capture ───────────────────────────────────────

    def arm_capture_scope(
        self,
        *,
        expected_text_hash: str,
        conversation_id: str | None,
        target_id: str | None,
    ) -> CaptureScope:
        """Arm a fresh capture scope before ``click_send``.

        The driver calls this inside the MutationLock, after
        ``reenable_if_stale``. The returned scope MUST be closed in ``finally``
        on every terminal path (success, timeout, exception, cancellation).
        """
        self._send_sequence += 1
        scope = CaptureScope(
            listener=self,
            expected_text_hash=expected_text_hash,
            conversation_id=conversation_id,
            target_id=target_id,
            send_sequence_id=self._send_sequence,
        )
        scope._arm()
        # Replace any stale active scope (shouldn't happen if close() is used
        # correctly, but defensive).
        if self._active_scope is not None and not self._active_scope._closed:
            logger.warning(
                "identity_listener: arming new scope while previous is active "
                "(seq=%d); closing stale", self._active_scope.send_sequence_id
            )
            self._active_scope.close()
        self._active_scope = scope
        return scope

    async def wait_for_captured_uuid(self, timeout: float = 5.0) -> str | None:
        """Wait for the armed scope to capture a UUID, or timeout.

        Returns the captured UUID, or None if the scope closed without a
        capture (timeout, scope closed by finally, or no matching POST).
        """
        scope = self._active_scope
        if scope is None or scope.future is None:
            return None
        try:
            result = await asyncio.wait_for(scope.future, timeout=timeout)
            return result.uuid
        except TimeoutError:
            self.capture_missed_count += 1
            self._record_fallback_reason("capture_timeout")
            logger.info(
                "identity_capture_missed: timeout after %.1fs (seq=%d)",
                timeout, scope.send_sequence_id,
            )
            return None

    def _clear_active_scope(self, scope: CaptureScope) -> None:
        """Internal: called by CaptureScope.close() to clear the active ref."""
        if self._active_scope is scope:
            self._active_scope = None

    def _record_fallback_reason(self, reason: str) -> None:
        self.fallback_reasons[reason] = self.fallback_reasons.get(reason, 0) + 1

    # ── Event handler (non-blocking) ────────────────────────────

    def _on_request_will_be_sent(self, msg: dict) -> None:
        """``Network.requestWillBeSent`` handler.

        Called SYNCHRONOUSLY from the reader loop. MUST be fast. Does only
        a URL/method prefilter; if promising, schedules the heavy parse via
        ``create_task`` so the reader loop is never blocked.
        """
        scope = self._active_scope
        if scope is None or scope._closed:
            return  # no capture armed
        try:
            params = msg.get("params") or {}
            req = params.get("request") or {}
            url = req.get("url") or ""
            method = req.get("method") or ""
            # Fast prefilter: only POST to the send endpoint.
            if method != "POST" or not url.rstrip("/").endswith(_SEND_ENDPOINT_SUFFIX):
                return
            # Promising — schedule the heavy parse off the reader loop.
            loop = asyncio.get_event_loop()
            loop.create_task(self._process_send_post(scope, msg, url))
        except Exception:
            # Never let handler errors escape into the reader loop.
            logger.exception("identity_listener: handler error in prefilter")

    async def _process_send_post(self, scope: CaptureScope, msg: dict, url: str) -> None:
        """Heavy parse + validation, scheduled off the reader loop.

        Validates the POST belongs to this send (failure-mode B), extracts
        the UUID, and resolves the scope's future.
        """
        try:
            params = msg.get("params") or {}
            req = params.get("request") or {}
            request_id = params.get("requestId")
            post_data = req.get("postData")

            if not post_data:
                # Failure-mode C: postData absent → try getRequestPostData once.
                self.postdata_missing_count += 1
                post_data = await self._fetch_post_data(request_id)
                if not post_data:
                    self._record_fallback_reason("postdata_missing")
                    return  # leave scope open; a later POST may match

            # Parse + validate.
            try:
                parsed = json.loads(post_data)
            except (json.JSONDecodeError, TypeError) as e:
                self._record_fallback_reason("postdata_unparseable")
                logger.debug("identity_capture: post_data not JSON: %s", e)
                return

            # Failure-mode B: validate the POST belongs to this send.
            action = parsed.get("action")
            if action != "next":
                return  # not a user send (could be a regenerate/edit/etc.)
            messages = parsed.get("messages") or []
            if not messages:
                return
            m0 = messages[0]
            author = (m0.get("author") or {}).get("role")
            if author != "user":
                return
            uuid = m0.get("id")
            if not uuid or not _UUID_RE.match(str(uuid)):
                self._record_fallback_reason("uuid_invalid")
                return
            # conversation_id match if known.
            body_conv_id = parsed.get("conversation_id")
            if scope.conversation_id and body_conv_id and body_conv_id != scope.conversation_id:
                # Different conversation — not our send.
                return
            # text-hash match if available (failure-mode D: multiple POSTs).
            parts = (m0.get("content") or {}).get("parts") or []
            body_text = "\n".join(str(p) for p in parts if isinstance(p, str))
            if body_text:
                body_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
                if body_hash != scope.expected_text_hash:
                    # Text doesn't match — could be a different send (retry,
                    # regenerate). Don't resolve; leave scope open.
                    logger.debug(
                        "identity_capture: text hash mismatch (expected %s, got %s) — "
                        "not our send, leaving scope open",
                        scope.expected_text_hash[:12], body_hash[:12],
                    )
                    return

            # Success — resolve the scope.
            self.capture_success_count += 1
            scope._resolve(CaptureResult(
                uuid=uuid,
                reason="matched",
                candidate_count=1,
                request_id=request_id,
            ))
            logger.info(
                "identity_capture_success: uuid=%s seq=%d",
                uuid, scope.send_sequence_id,
            )
        except Exception:
            logger.exception("identity_listener: error processing send POST")

    async def _fetch_post_data(self, request_id: str | None) -> str | None:
        """Failure-mode C: try Network.getRequestPostData if postData absent."""
        if not request_id:
            return None
        try:
            result = await self._driver._cdp(
                "Network.getRequestPostData",
                {"requestId": request_id},
                timeout=10,
            )
            return (result.get("result") or {}).get("postData")
        except Exception as e:
            logger.debug("identity_capture: getRequestPostData failed: %s", e)
            return None


def hash_sent_text(text: str) -> str:
    """Stable SHA-256 hash of the sent prompt, for capture validation.

    The hash is computed over the *raw* sent text (before any normalization)
    because the POST body carries the raw text. Used by the capture scope to
    validate a POST belongs to this send (failure-mode D).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

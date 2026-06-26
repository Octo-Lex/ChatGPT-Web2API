"""Backend client — ChatGPT backend-api fetch helpers extracted from CDPDriver.

Phase 5 PR1 extraction (no behavior change). Owns the method bodies that talk
to ChatGPT's backend-api over HTTP via ``Runtime.evaluate``:

  - token / session lifecycle (``_refresh_token``, ``ensure_token``, ``recover_auth``)
  - conversation fetch (``_fetch_text``, ``_fetch_end_turn``, conversation-id
    resolution, ``_check_auth_in_raw``)
  - backend-api read/mutate (models, projects, conversations, memories, gpts)

The driver-reference collaborator seam: ``BackendClient`` holds a reference to
its owning ``CDPDriver`` and reaches through it for the live CDP transport
(``_js`` / ``_js_strict`` / ``_js_with_data`` / ``_js_with_data_strict``) and
for shared mutable state (``_access_token``, ``_breakers``, ``_current_conv_id``,
``_user_name``, ``_token_fetched_at``). None of that state migrates into this
module — it stays on the driver so external attribute reads and test stubs keep
working unchanged.

Call-rule inside BackendClient method bodies:

  transport calls:        self._driver._js_with_data_strict(...)
                          self._driver._js_strict(...)
  driver-owned state:     self._driver._access_token
                          self._driver._breakers
                          self._driver._current_conv_id
  moved backend peers:    self.ensure_token(...)
                          self._refresh_token(...)
                          self._check_auth_in_raw(...)

Internal backend-to-backend calls stay on ``self`` (not ``self._driver``) to
avoid recursing through the driver delegator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .breakers import BreakerKind

logger = logging.getLogger(__name__)


class _Transient404(Exception):
    """Sentinel raised by ``_fetch_text_once`` on a backend 404.

    Internal to ``_fetch_text``'s bounded retry loop — never escapes this
    module. The 404 is a transient race (conversation not yet persisted
    immediately after a send), NOT an auth failure or persistent backend
    fault, so it is deliberately not modeled as a breaker signal.
    """


# Re-check the access token if it's older than this. The observed ChatGPT
# JWT has a ~10-day lifetime, so 1h is a conservative refresh interval: it
# avoids unnecessary refetches on the happy path while guaranteeing a stale
# token is refreshed well before its real expiry.
#
# Canonical home moved here from cdp_driver.py in Phase 5 PR1; cdp_driver
# re-exports it for back-compat.
TOKEN_TTL_SECONDS = 3600


class BackendClient:
    """Backend-api fetch helpers, composed by ``CDPDriver``.

    Constructed once in ``CDPDriver.__init__`` and stored as
    ``self._backend_client``. The driver keeps thin delegating methods for
    every method here so its public/private API surface is byte-identical to
    pre-extraction.
    """

    def __init__(self, driver) -> None:
        self._driver = driver

    # ── Token Management ──────────────────────────────────────

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
        d = self._driver
        last_error: Exception | None = None
        for attempt in range(1, 4):
            d._last_refresh_attempt_at = time.time()
            try:
                raw = await d._js(
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
                # Parse into locals FIRST. Only commit to instance state after
                # a non-empty token is observed — a transient empty fetch (cold
                # tab, navigation in flight, CDP blip) must not clobber a
                # previously-valid token. The exception path already avoided
                # clobbering; this closes the empty-SUCCESS path too. A real
                # auth expiry surfaces as a typed 401 on the next backend call.
                new_token = data.get("token", "")
                new_user = data.get("user", "")
                if new_token:
                    d._access_token = new_token
                    d._user_name = new_user
                    d._token_fetched_at = time.time()
                    logger.info(
                        "Auth: %d chars, user: %s (attempt %d)",
                        len(d._access_token),
                        d._user_name,
                        attempt,
                    )
                    return
                last_error = RuntimeError("No access token — not logged into ChatGPT")
            except Exception as e:
                # JSON parse error, CDP blip, etc. — record and retry. Don't
                # clobber a partial _access_token from a prior good fetch.
                last_error = e
            if attempt < 3:
                await asyncio.sleep(0.5)
        raise (
            last_error if last_error else RuntimeError("No access token — not logged into ChatGPT")
        )

    async def ensure_token(self) -> str:
        """Ensure a non-stale access token, refreshing if empty OR older than TTL.

        Returns the token. The TTL guard (TOKEN_TTL_SECONDS) catches expiry
        well before the real JWT lifetime; callers should invoke this before
        any /backend-api/* fetch so a stale session surfaces as
        AuthExpiredError (via _fetch_text) rather than silent empty data.

        Reaches ``_refresh_token`` through the driver delegator so test
        monkeypatches of ``driver._refresh_token`` intercept. This is NOT
        recursive: ``_refresh_token`` is a leaf that does not call
        ``ensure_token``.
        """
        d = self._driver
        stale = not d._access_token or time.time() - d._token_fetched_at > TOKEN_TTL_SECONDS
        if stale:
            await d._refresh_token()
        return d._access_token

    async def recover_auth(self) -> bool:
        """Probe whether the ChatGPT session is valid again, and if so reset
        the AUTH_EXPIRED breaker.

        Called by the REST/MCP fail-fast preflight when AUTH_EXPIRED is the open
        breaker — a user may have logged back in via the browser since the trip.
        This refreshes the access token (a lightweight ``/api/auth/session``
        fetch, NOT a chat send): a non-empty token means auth is restored, so we
        reset the breaker and return True. A failure or empty token leaves the
        breaker open and returns False (caller proceeds to fail-fast).

        Does NOT touch ``record_success`` — auth recovery is an explicit
        ``reset()``, matching the indefinite-auth semantics (auth is not a
        rolling-window breaker).
        """
        d = self._driver
        if d._breakers is None or not d._breakers.is_open(BreakerKind.AUTH_EXPIRED):
            return True  # nothing to recover
        try:
            await d._refresh_token()
        except Exception as e:
            logger.info("Auth recovery probe failed: %s", e)
            return False
        # _refresh_token only returns on a non-empty token (it raises on failure).
        logger.info("Auth recovered — resetting AUTH_EXPIRED breaker")
        d._breakers.reset(BreakerKind.AUTH_EXPIRED)
        return True

    # ── Auth detection in raw responses ───────────────────────

    def _check_auth_in_raw(self, raw: str) -> None:
        """#20: Detect auth failure in raw response text and raise.

        Most read methods' JS doesn't check r.ok or r.status — a 401 returns
        the HTML login page body. This helper catches that case in Python so
        a stale token surfaces as AuthExpiredError instead of empty data.
        Called after _js_with_data_strict returns, before json.loads.
        """
        # Imported lazily to avoid a module-load circular dependency
        # (cdp_driver imports backend_client at CDPDriver.__init__ time).
        from .cdp_driver import AuthExpiredError

        if not raw:
            return
        # Login pages contain these markers
        lower = raw[:500].lower()
        if "sign in" in lower and "chatgpt" in lower and "<html" in lower:
            if self._driver._breakers:
                self._driver._breakers.trip(
                    BreakerKind.AUTH_EXPIRED, "login page returned instead of data"
                )
            raise AuthExpiredError("Session expired — read returned login page instead of data")

    # ── Conversation fetch ────────────────────────────────────

    # Bounded retry for the transient 404 returned by the backend-api
    # immediately after a send, before the just-created conversation is
    # persisted server-side. This is a transient race, NOT an auth failure or
    # a persistent backend fault, so it is deliberately NOT a breaker signal
    # (follow-up C decides separately whether persistent 404/5xx should ever
    # trip a breaker). The bound stays small so a genuinely-missing
    # conversation surfaces quickly.
    _FETCH_TEXT_404_MAX_ATTEMPTS = 4
    _FETCH_TEXT_404_BACKOFF_SECONDS = 0.5

    async def _fetch_text(self, conversation_id: str) -> str:
        """Fetch the latest assistant text from the conversation API.

        Non-OK responses are encoded by the JS as ``{"__status": <code>}``
        rather than ``''`` so Python can distinguish an auth failure (401 →
        AuthExpiredError) from a missing conversation (404) or a network
        error. This parse-and-raise happens here, before any return reaches
        the caller, so callers never see a raw status blob as text.

        A 404 specifically is treated as a transient race and retried a
        bounded number of times: the backend-api returns 404 immediately
        after a send while the just-created conversation is still
        propagating server-side. Only 404 is retried — 401 still raises
        ``AuthExpiredError`` immediately (with breaker trip) and any other
        non-OK status still raises ``RuntimeError`` immediately. After the
        retry bound is exhausted the 404 surfaces as ``RuntimeError`` so
        callers see the same type they did pre-retry.

        Picks the newest assistant text message by ``create_time`` rather than
        trusting the API's ``current_node`` pointer: that pointer lags behind
        on continued conversations (it still points at the previous turn right
        after a send), which produced an off-by-one where request N returned
        request N-1's text. The newest-by-create-time selection is immune to
        that lag.
        """
        from .cdp_driver import CDPJSError

        last_error: Exception | None = None
        for attempt in range(1, self._FETCH_TEXT_404_MAX_ATTEMPTS + 1):
            try:
                return await self._fetch_text_once(conversation_id)
            except _Transient404 as e:
                # Transient race — retry after a short backoff unless this was
                # the final attempt, in which case it falls through to the
                # RuntimeError raise below.
                last_error = e
                if attempt < self._FETCH_TEXT_404_MAX_ATTEMPTS:
                    logger.debug(
                        "_fetch_text 404 for %s (attempt %d/%d), retrying",
                        conversation_id,
                        attempt,
                        self._FETCH_TEXT_404_MAX_ATTEMPTS,
                    )
                    await asyncio.sleep(self._FETCH_TEXT_404_BACKOFF_SECONDS)
                continue
            except CDPJSError as e:
                # JS transport failure: not a 404 race. Preserve the pre-retry
                # behavior of swallowing it and returning "" (callers retry
                # the whole send poll loop).
                logger.debug("_fetch_text JS failed: %s", e)
                return ""
        # Bound exhausted — surface the 404 as RuntimeError, matching the
        # pre-retry behavior callers already handle.
        raise RuntimeError(
            f"_fetch_text HTTP 404 for {conversation_id} "
            f"after {self._FETCH_TEXT_404_MAX_ATTEMPTS} attempts"
        ) from last_error

    async def _fetch_text_once(self, conversation_id: str) -> str:
        """Single backend-api conversation fetch + status decode.

        Returns the assistant text on success, raises ``_Transient404`` on a
        404 (so the bounded retry loop in ``_fetch_text`` can catch it),
        raises ``AuthExpiredError`` (with breaker trip) on 401, and raises
        ``RuntimeError`` for any other non-OK status. Empty/blank bodies and
        JS-transport failures are handled by the caller.

        Status decode (the ``__status`` blob shape) lives here so it runs
        before any text reaches the caller regardless of the retry wrapper.
        """
        # Imported lazily to avoid a module-load circular dependency.
        from .cdp_driver import AuthExpiredError

        d = self._driver
        await self._driver.ensure_token()
        raw = await d._js_with_data_strict(
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
            "      if (!parts.length || !parts.some(function(p){ return String(p).trim(); })) continue;"
            "      var t = m.create_time || 0;"
            "      if (t >= bestTime) { bestTime = t; best = parts.filter(function(p){ return String(p).trim(); }).join('\\n'); }"
            "    }"
            "    return best || '';"
            "  } catch(e) { return ''; }"
            "})()",
            {"conv_id": conversation_id, "token": d._access_token},
            timeout=15,
        )
        if not raw:
            return ""
        # Detect the status-blob shape (non-OK response) and raise appropriately.
        # Cheap pre-check before json.loads to avoid parsing every valid text body.
        if raw.startswith('{"__status"') or raw.startswith('{ "__status"'):
            try:
                payload = json.loads(raw)
                status = payload.get("__status")
            except (json.JSONDecodeError, TypeError):
                status = None
            if status == 401:
                if d._breakers:
                    d._breakers.trip(BreakerKind.AUTH_EXPIRED, "HTTP 401 from backend-api")
                raise AuthExpiredError()
            if status == 404:
                # Transient race: conversation not yet persisted after send.
                # The bounded retry in _fetch_text catches this.
                raise _Transient404(conversation_id)
            if status is not None:
                raise RuntimeError(f"_fetch_text HTTP {status} for {conversation_id}")
        return raw

    async def _conversation_id_from_url(self) -> str:
        """Parse the conversation id from the live tab's ``location.href``.

        Returns ``""`` when the URL is not yet a conversation URL (e.g. still
        on the composer/new-chat page before the first send resolves) or when
        the JS evaluation fails. Best-effort — callers retry on empty.
        """
        from .cdp_driver import CDPJSError

        try:
            url = await self._driver._js_strict("window.location.href")
        except (CDPJSError, TypeError):
            return ""
        if not url or "/c/" not in url:
            return ""
        return url.split("/c/")[1].split("/")[0].split("?")[0]

    async def _get_live_conversation_id_best_effort(self) -> str:
        """Resolve the in-flight conversation id by cheapest available source.

        Ordered for a new-chat poll loop where ``_current_conv_id`` is still
        None (it is only set AFTER the loop, from the URL — see
        ``send_and_stream``):

        1. ``self._driver._current_conv_id`` — populated on continued
           conversations (REST path) and after the first completed send.
        2. ``location.href`` ``/c/{id}`` — available within ~1s of send, once
           ChatGPT navigates to the new conversation.

        Deliberately does NOT consult the conversation backend API: that is an
        expensive fetch, and this helper is called every ~1s during polling.
        Returns ``""`` if no source has a usable id yet.
        """
        if self._driver._current_conv_id:
            return self._driver._current_conv_id
        return await self._conversation_id_from_url()

    async def _fetch_end_turn(self, conversation_id: str) -> bool:
        """Backend secondary completion signal: is the latest assistant TEXT
        node marked ``end_turn === true``?

        A fallback for the Phase-2 DOM completion detector: if the action-
        button selector drifts again (as it did when ChatGPT moved the buttons
        to a sibling container), the DOM ``has_action`` stays false forever
        and the loop stalls. This reads the conversation API and checks the
        terminal flag on the newest assistant text node — the same
        newest-by-create-time selection ``_fetch_text`` uses (NOT current_node,
        which lags on continued conversations, and NOT reasoning_recap nodes,
        which carry empty text).

        Returns False on ANY failure (fetch error, parse error, no assistant
        text node, end_turn falsy). Callers treat False as "not confirmed,
        keep polling DOM" — this is defense-in-depth, never the sole signal.
        """
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
                "(async function() {"
                "  try {"
                "    var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=5', {"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    if (!r.ok) return 'false';"
                "    var conv = await r.json();"
                "    var mapping = conv.mapping || {};"
                # Newest assistant TEXT node by create_time (mirrors
                # _fetch_text: current_node lags; reasoning_recap has no text).
                "    var bestTime = -1; var bestEnd = false;"
                "    for (var k in mapping) {"
                "      var n = mapping[k]; var m = n.message;"
                "      if (!m || !m.author || m.author.role !== 'assistant') continue;"
                "      if (!m.content || m.content.content_type !== 'text') continue;"
                "      var parts = m.content.parts || [];"
                "      if (!parts.length || !parts.some(function(p){ return String(p).trim(); })) continue;"
                "      var t = m.create_time || 0;"
                "      if (t >= bestTime) { bestTime = t; bestEnd = !!m.end_turn; }"
                "    }"
                "    return bestEnd ? 'true' : 'false';"
                "  } catch(e) { return 'false'; }"
                "})()",
                {"conv_id": conversation_id, "token": d._access_token},
                timeout=15,
            )
        except CDPJSError:
            return False
        return raw == "true"

    # ── Backend API: Models & Projects ─────────────────────────

    async def get_models(self) -> list[dict]:
        """List available models.

        The ChatGPT API returns ``{"title":..., "models":[{"slug":..., ...}]}``
        as a JSON string. Parse it and return just the models array so callers
        get the ``list[dict]`` the signature promises (each with ``slug`` and
        ``title``). Earlier this returned the raw string, which made
        ``do_list_models`` crash on ``m.get('slug')`` — only live testing
        caught it, since the mocked unit tests returned dicts.
        """
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/models?iim=false&is_gizmo=false', {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  return await r.text();"
                "})()",
                {"token": d._access_token},
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

    async def get_projects(self) -> list[dict]:
        d = self._driver
        from .cdp_driver import CDPJSError

        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
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
                {"token": d._access_token},
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_projects failed: %s", e)
            return []

    # ── Conversation Management ──────────────────────────────

    async def get_conversations(
        self,
        offset: int = 0,
        limit: int = 28,
        order: str = "updated",
    ) -> list[dict]:
        """List recent conversations."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
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
                {
                    "token": d._access_token,
                    "offset": str(offset),
                    "limit": str(limit),
                    "order": order,
                },
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_conversations failed: %s", e)
            return []

    async def get_conversation(self, conversation_id: str) -> dict:
        """Get full conversation detail with message mapping."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
                "(async () => {"
                "  var r = await fetch('/backend-api/conversation/' + __D.conv_id, {"
                "    headers: {'Authorization': 'Bearer ' + __D.token}"
                "  });"
                "  return await r.text();"
                "})()",
                {"conv_id": conversation_id, "token": d._access_token},
                timeout=30,
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_conversation failed: %s", e)
            return {}

    async def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation. Returns True on success."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            result = await d._js_with_data_strict(
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
                {"conv_id": conversation_id, "token": d._access_token},
            )
        except CDPJSError as e:
            logger.warning("delete_conversation JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info("Deleted conversation: %s", conversation_id)
            if d._current_conv_id == conversation_id:
                d._current_conv_id = None
            return True
        logger.warning("Failed to delete conversation %s: %s", conversation_id, result)
        return False

    async def rename_conversation(self, conversation_id: str, title: str) -> bool:
        """Rename a conversation. Returns True on success."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            result = await d._js_with_data_strict(
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
                {"conv_id": conversation_id, "token": d._access_token, "title": title},
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
        d = self._driver
        await self._driver.ensure_token()
        raw = await d._js_with_data(
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
                "token": d._access_token,
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
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            result = await d._js_with_data_strict(
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
                {
                    "token": d._access_token,
                    "project_id": project_id,
                    "instructions": instructions,
                },
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
        d = self._driver
        raw = await d._js_with_data(
            "(async () => {"
            "  var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
            "    headers: {'Authorization': 'Bearer ' + __D.token}"
            "  });"
            "  return await r.text();"
            "})()",
            {"token": d._access_token, "project_id": project_id},
            timeout=15,
        )
        try:
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # ── Archive Conversation ────────────────────────────────

    async def archive_conversation(self, conversation_id: str, archive: bool = True) -> bool:
        """Archive or unarchive a conversation. Returns True on success."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            result = await d._js_with_data_strict(
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
                {"conv_id": conversation_id, "token": d._access_token, "archive": archive},
            )
        except CDPJSError as e:
            logger.warning("archive_conversation JS failed: %s", e)
            result = "false"
        if result == "true":
            logger.info(
                "%s conversation: %s", "Archived" if archive else "Unarchived", conversation_id
            )
            return True
        logger.warning("Failed to archive conversation: %s", result)
        return False

    # ── Memory Management ─────────────────────────────────────

    async def get_memories(self) -> list[dict]:
        """List all ChatGPT memories."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
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
                {"token": d._access_token},
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

    async def create_memory(self, content: str) -> dict:
        """Create a memory by sending a chat message asking ChatGPT to remember.

        The POST /backend-api/memories endpoint returns 405 — ChatGPT only
        creates memories through conversation. This method sends a message
        asking ChatGPT to remember the content, which triggers the memory
        system automatically.

        Note: ``navigate_new_chat`` and ``send_and_stream`` are driver-owned
        (DOM/send concerns, not backend-fetch concerns), so they are reached
        through the driver reference rather than as moved peers.
        """
        d = self._driver
        memory_prompt = f"Please remember this for all future conversations: {content}"

        # Navigate to a fresh chat for memory creation
        await d.navigate_new_chat()

        # Send and collect the response
        full_response = ""
        async for chunk in d.send_and_stream(memory_prompt, timeout=60):
            if chunk.delta:
                full_response += chunk.delta

        conv_id = d._current_conv_id or ""

        logger.info("Memory creation request sent via chat (conv: %s)", conv_id)

        # #17: Check if the memory was actually created by looking for it
        # in the memories list. ChatGPT may refuse or paraphrase — without
        # this check the caller can't tell success from failure.
        memory_created = False
        try:
            memories = await d.get_memories()
            memory_created = any(
                content[:30].lower() in (m.get("content", "")[:50].lower()) for m in memories
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

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a ChatGPT memory by ID. Returns True on success."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            result = await d._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/memories/' + __D.memory_id, {"
                "      method: 'DELETE',"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"memory_id": memory_id, "token": d._access_token},
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

    async def delete_project(self, project_id: str) -> dict:
        """Delete a ChatGPT project by ID. Returns {success, project_id}.

        Projects are deleted via DELETE /backend-api/gizmos/{id} (the gizmos
        endpoint serves both Projects (g-p-) and Custom GPTs (g-) for deletion;
        creation is split across /projects and /gizmos, but deletion is shared).
        Verified to return 200 against a live account.
        """
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            result = await d._js_with_data_strict(
                "(async () => {"
                "  try {"
                "    var r = await fetch('/backend-api/gizmos/' + __D.project_id, {"
                "      method: 'DELETE',"
                "      headers: {'Authorization': 'Bearer ' + __D.token}"
                "    });"
                "    return r.ok ? 'true' : 'false';"
                "  } catch(e) { return 'error:' + e.message; }"
                "})()",
                {"project_id": project_id, "token": d._access_token},
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

    # ── Custom GPTs ─────────────────────────────────────────

    async def list_gpts(self) -> list[dict]:
        """List Custom GPTs (non-project gizmos).

        Projects (gizmo_type='snorlax') are excluded — use get_projects()
        for those.  Only marketplace or user-created non-project GPTs
        are returned.
        """
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
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
                {"token": d._access_token},
                timeout=20,
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("list_gpts failed: %s", e)
            return []

    # ── Project Files ─────────────────────────────────────────

    async def get_project_files(self, project_id: str) -> list[dict]:
        """List files attached to a ChatGPT project."""
        from .cdp_driver import CDPJSError

        d = self._driver
        await self._driver.ensure_token()
        try:
            raw = await d._js_with_data_strict(
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
                {"token": d._access_token, "project_id": project_id},
                timeout=15,
            )
            self._check_auth_in_raw(raw)
            return json.loads(raw)
        except (CDPJSError, json.JSONDecodeError) as e:
            logger.warning("get_project_files failed: %s", e)
            return []

"""OpenAI-compatible API server.

Endpoints:
  POST /v1/chat/completions  — chat (streaming + non-streaming)
  GET  /v1/models            — model catalog
  GET  /v1/projects          — ChatGPT projects
  GET  /health               — health + Chrome status
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from aiohttp import web

from .cdp_driver import (
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
    RateLimitError,
    is_rate_limited_text,
)
from .config import Config
from .cross_process_lock import CrossProcessLock, LockAcquisitionError
from .resilience import retry_on_rate_limit

logger = logging.getLogger(__name__)

# Model mapping: user-facing names → ChatGPT web slugs
MODEL_MAP = {
    "gpt-5.5": "gpt-5-5",
    "gpt-5.5-thinking": "gpt-5-5-thinking",
    "gpt-5.3": "gpt-5-3",
    "gpt-5.2": "gpt-5-2",
    "gpt-5.1": "gpt-5-1",
    "gpt-5": "gpt-5",
    "gpt-5-mini": "gpt-5-mini",
    "gpt-5.3-mini": "gpt-5-3-mini",
    "auto": "auto",
    # Legacy aliases
    "gpt-4o": "auto",
    "gpt-4": "gpt-5",
    "gpt-3.5-turbo": "gpt-5-mini",
}


class APIServer:
    """OpenAI-compatible API backed by CDP automation."""

    def __init__(self, config: Config, driver: CDPDriver) -> None:
        self._config = config
        self._driver = driver
        self._cdp_port = config.chrome.cdp_port
        self._request_count = 0
        # Health telemetry (event-derived, not polled). These are the only
        # fields that make sense to cache: they mark WHEN something happened,
        # not whether something is alive right now (that's computed live in
        # _handle_health). Without last_successful_send_at, a zombie process
        # that never connected (cdp_connected=false, requests_served=0) looks
        # identical to a freshly-started healthy one — both report "waiting".
        self._started_at = time.time()
        self._last_error: str | None = None
        self._last_successful_send_at: float | None = None
        # Track last conversation for multi-turn continuity
        self._last_conv_id: str | None = None
        self._last_project_id: str | None = None

        self.app = web.Application(client_max_size=10 * 1024 * 1024)
        self.app.router.add_post("/v1/chat/completions", self._handle_chat)
        self.app.router.add_post("/chat/completions", self._handle_chat)
        self.app.router.add_get("/v1/models", self._handle_models)
        self.app.router.add_get("/v1/projects", self._handle_projects)
        self.app.router.add_get("/health", self._handle_health)
        self.app.router.add_get("/", self._handle_health)

    # ── Auth ──────────────────────────────────────────────────

    def _check_auth(self, request: web.Request) -> web.Response | None:
        """Check API key if configured. Returns error response or None."""
        keys = self._config.server.api_keys
        if not keys:
            return None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
        else:
            key = request.query.get("key", "")
        if key not in keys:
            return web.json_response(
                {"error": {"message": "Invalid API key", "type": "auth_error"}},
                status=401,
            )
        return None

    # ── Handlers ──────────────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Honest health endpoint — observes current reality, not a stale mirror.

        The old version returned ``"waiting"`` when CDP was disconnected, which
        is indistinguishable from "freshly started, connecting now" — a zombie
        process (HTTP listener up, CDP never connected) reported the same
        status as a healthy one. This version distinguishes four states:

        - ``starting``: listener up, driver not yet connected, never served
        - ``healthy``: Chrome alive AND driver connected
        - ``degraded``: Chrome alive but driver disconnected (zombie/recovering)
        - ``broken``: Chrome itself unreachable

        Live fields (chrome_running, driver_connected) are computed fresh on
        each call — /health is infrequent (supervisor poll), and cached state
        would lag reality. Event-derived fields (started_at, last_error,
        last_successful_send_at, requests_served) are tracked on the instance.
        """
        import urllib.request

        driver_connected = bool(self._driver.is_connected)

        # Chrome liveness: cheap HTTP GET to /json/version. If Chrome is dead,
        # this fails fast (connection refused). Run synchronously — /health is
        # infrequent and the call is sub-millisecond on loopback.
        chrome_running = False
        try:
            loop = asyncio.get_event_loop()
            def _probe():
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{self._cdp_port}/json/version", timeout=2
                    ) as r:
                        return r.status == 200
                except Exception:
                    return False
            chrome_running = await loop.run_in_executor(None, _probe)
        except Exception:
            chrome_running = False

        # Status logic — zombie case (Chrome up, driver dead) is "degraded",
        # never "ok"/"waiting". The old "waiting" non-answer is gone.
        if not chrome_running:
            status = "broken"
        elif not driver_connected:
            status = "degraded"
        elif self._last_successful_send_at is None and self._request_count == 0:
            status = "starting"
        else:
            status = "healthy"

        return web.json_response({
            "status": status,
            "chrome_running": chrome_running,
            "cdp_connected": driver_connected,
            "driver_connected": driver_connected,
            "requests_served": self._request_count,
            "started_at": self._started_at,
            "last_successful_send_at": self._last_successful_send_at,
            "last_error": self._last_error,
        })

    async def _handle_models(self, request: web.Request) -> web.Response:
        if err := self._check_auth(request):
            return err
        try:
            raw = await self._driver.get_models()
        except Exception:
            raw = []

        models = []
        for m in raw:
            slug = m.get("slug", "")
            models.append({
                "id": slug,
                "object": "model",
                "created": 1700000000,
                "owned_by": "chatgpt-web",
            })

        if not models:
            for slug in ["auto", "gpt-5-5", "gpt-5-mini"]:
                models.append({"id": slug, "object": "model", "created": 1700000000, "owned_by": "chatgpt-web"})

        return web.json_response({"object": "list", "data": models})

    async def _handle_projects(self, request: web.Request) -> web.Response:
        if err := self._check_auth(request):
            return err
        try:
            projects = await self._driver.get_projects()
        except Exception as e:
            logger.error("Failed to get projects: %s", e)
            projects = []
        return web.json_response({"object": "list", "data": projects})

    async def _handle_chat(self, request: web.Request) -> web.Response:
        if err := self._check_auth(request):
            return err

        self._request_count += 1

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response(
                {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}},
                status=400,
            )

        messages = body.get("messages", [])
        if not messages:
            return web.json_response(
                {"error": {"message": "No messages provided", "type": "invalid_request_error"}},
                status=400,
            )

        model = body.get("model", self._config.chatgpt.default_model)
        stream = body.get("stream", False)
        project_id = (
            body.get("project_id")
            or body.get("gizmo_id")
            or (body.get("metadata", {}) or {}).get("project_id")
            or self._config.chatgpt.default_project_id
        )
        conversation_id = body.get("conversation_id")

        # Build conversation text from all messages
        # Includes prior assistant context for stateless clients (OpenAI SDK)
        system_parts = []
        conversation_lines = []
        user_msg_count = 0
        MAX_HISTORY_TURNS = 10  # Cap to avoid textarea overflow

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            else:
                content = str(content)

            if role == "system":
                system_parts.append(content)
            elif role == "user":
                conversation_lines.append(f"[User]\n{content}")
                user_msg_count += 1
            elif role == "assistant":
                conversation_lines.append(f"[Assistant]\n{content}")

        # Trim to last N turns if too many messages
        if len(conversation_lines) > MAX_HISTORY_TURNS * 2:
            conversation_lines = conversation_lines[-(MAX_HISTORY_TURNS * 2):]

        # Verify at least one user message exists
        if user_msg_count == 0:
            return web.json_response(
                {"error": {"message": "No user message", "type": "invalid_request_error"}},
                status=400,
            )

        # Compose final text
        prefix = ""
        if system_parts:
            prefix = "[System Instructions]\n" + "\n".join(system_parts) + "\n\n"
        full_text = prefix + "\n".join(conversation_lines)

        model_slug = MODEL_MAP.get(model, model)
        timeout = self._config.server.request_timeout

        logger.info(
            "Request #%d: model=%s->%s conv=%s project=%s stream=%s msg=%.60s",
            self._request_count, model, model_slug, conversation_id, project_id, stream, full_text,
        )

        # Serialize — cross-process lock so MCP + REST don't corrupt each other
        async with CrossProcessLock(cdp_port=self._cdp_port):
            try:
                # Select model if specified (non-fatal on failure)
                if model_slug and model_slug != "auto":
                    selected = await self._driver.select_model(model_slug)
                    if not selected:
                        logger.warning(
                            "Could not select model '%s', proceeding with active model",
                            model_slug,
                        )

                # Decide: continue existing conversation or start fresh?
                if conversation_id:
                    # Explicit conversation_id from client — navigate to it
                    await self._driver.navigate_conversation(conversation_id)
                elif (self._last_conv_id
                      and self._driver._current_conv_id == self._last_conv_id
                      and project_id == self._last_project_id
                      and not system_parts):
                    # Same session, same project, no system prompt override — continue.
                    # Reconcile against the live tab before sending: another process
                    # sharing the Chrome tab may have navigated it since our last turn,
                    # which would leave _current_conv_id stale. ensure_current_conversation
                    # verifies location.href and navigates back if needed (fail-closed).
                    logger.info("Continuing conversation: %s", self._last_conv_id)
                    await self._driver.ensure_current_conversation(self._last_conv_id)
                else:
                    # Fresh chat
                    await self._driver.navigate_new_chat(gizmo_id=project_id)
                    self._last_project_id = project_id

                if stream:
                    return await self._stream_response(request, model_slug, full_text, timeout)
                else:
                    return await self._full_response(request, model_slug, full_text, timeout)

            except Exception as e:
                logger.error("Chat error: %s", e, exc_info=True)
                self._last_error = f"{type(e).__name__}: {e}"
                return self._error_response(e)

    # ── Error mapping ─────────────────────────────────────────

    def _error_response(self, exc: Exception) -> web.Response:
        """Map a driver exception to an OpenAI-shaped error response.

        - RateLimitError → HTTP 429 with the canonical OpenAI
          ``rate_limit_exceeded`` type/code and a ``Retry-After`` header, so any
          OpenAI-aware agent framework (SDK, LangChain, LlamaIndex) automatically
          backs off and retries with zero client integration.
        - AuthExpiredError → HTTP 401 ``invalid_api_key`` — the ChatGPT session
          expired; previously this surfaced as silent empty data or a generic
          timeout.
        - GenerationStuckError → HTTP 504 ``generation_stuck`` — the generation
          stalled (no DOM progress within the stall window); the phase is in the
          message for diagnosis.
        - Everything else stays a 500 ``server_error`` (a real failure, not
          retriable).
        """
        if isinstance(exc, RateLimitError):
            retry_after = str(int(exc.retry_after))
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "rate_limit_exceeded",
                        "param": None,
                        "code": "rate_limit_exceeded",
                    }
                },
                status=429,
                headers={"Retry-After": retry_after},
            )
        if isinstance(exc, AuthExpiredError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_api_key",
                        "param": None,
                        "code": "invalid_api_key",
                    }
                },
                status=401,
            )
        if isinstance(exc, GenerationStuckError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "param": None,
                        "code": "generation_stuck",
                    }
                },
                status=504,
            )
        if isinstance(exc, LockAcquisitionError):
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "server_error",
                        "param": None,
                        "code": "lock_timeout",
                    }
                },
                status=503,
            )
        return web.json_response(
            {"error": {"message": str(exc), "type": "server_error"}},
            status=500,
        )

    # ── Response formatters ───────────────────────────────────

    async def _full_response(
        self, request: web.Request, model: str, text: str, timeout: float
    ) -> web.Response:
        """Non-streaming: collect all chunks, return one JSON.

        The send is wrapped in ``retry_on_rate_limit`` so a transient
        ChatGPT "Too many requests" pop-up is dismissed and retried
        transparently — the client only sees it (as a 429) if the limit
        persists across all retries.
        """

        async def _send_and_collect() -> str:
            collected = ""
            async for chunk in self._driver.send_and_stream(text, timeout=timeout):
                collected += chunk.delta
            return collected

        full_text = await retry_on_rate_limit(self._driver, _send_and_collect)

        conv_id = self._driver._current_conv_id or ""
        self._last_conv_id = conv_id
        self._last_successful_send_at = time.time()

        return web.json_response({
            "id": f"chatcmpl-{uuid.uuid4().hex[:29]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "conversation_id": conv_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    async def _stream_response(
        self, request: web.Request, model: str, text: str, timeout: float
    ) -> web.Response:
        """Streaming: SSE chunks as they arrive.

        Rate-limit handling for streaming is split, because once
        ``resp.prepare()`` commits the HTTP 200 status we can no longer send a
        429:

        - **Pre-flight** (before prepare): a single DOM scan. If throttled, we
          retry transparently (dismiss + backoff). If it persists, we return a
          proper 429 here while the status is still changeable.
        - **Mid-stream** (after prepare): a throttle is rare here (pre-flight
          cleared it), but if one occurs it falls back to the inline
          ``[Error: ...]`` SSE chunk — documented as a known limitation.
        """

        async def _preflight() -> None:
            """Raise RateLimitError if the pop-up is present right now."""
            try:
                scan = await self._driver._js_strict(
                    "(function(){var t=(document.body&&document.body.innerText)||'';"
                    "return JSON.stringify({text:t.slice(0,4000)});})()",
                    timeout=10,
                )
            except Exception:
                # CDP/JS error during scan — assume no rate limit (proceed).
                return
            try:
                body = json.loads(scan).get("text", "") if scan else ""
            except (json.JSONDecodeError, TypeError):
                body = ""
            if is_rate_limited_text(body):
                raise RateLimitError.from_text(body)

        # Transparent pre-flight retry — dismisses the pop-up and retries so a
        # transient limit never reaches the client as an error.
        try:
            await retry_on_rate_limit(self._driver, _preflight, max_attempts=3)
        except RateLimitError:
            # Persistent at pre-flight: still pre-prepare, so send a clean 429.
            raise

        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        cid = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        created = int(time.time())

        # Role chunk
        await self._send_sse(resp, {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        })

        try:
            async for chunk in self._driver.send_and_stream(text, timeout=timeout):
                if chunk.delta:
                    await self._send_sse(resp, {
                        "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": chunk.delta}, "finish_reason": None}],
                    })
                if chunk.finish_reason:
                    conv_id = self._driver._current_conv_id or ""
                    self._last_conv_id = conv_id
                    if chunk.finish_reason == "stop":
                        self._last_successful_send_at = time.time()
                    await self._send_sse(resp, {
                        "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                        "conversation_id": conv_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": chunk.finish_reason}],
                    })
        except RateLimitError as e:
            # Mid-stream throttle (rare after pre-flight). Status is locked at
            # 200, so we can't upgrade to 429; surface as an inline error chunk
            # with a recognizable marker so clients can detect it.
            logger.warning("Mid-stream rate limit: %s", e)
            await self._send_sse(resp, {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": f"\n\n[Error: rate_limit_exceeded — retry in {e.retry_after}s]"}, "finish_reason": "error"}],
            })
        except AuthExpiredError:
            # Session expired mid-stream (status locked at 200). Surface with a
            # recognizable marker so clients can prompt re-login.
            logger.warning("Mid-stream auth expiry")
            await self._send_sse(resp, {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": "\n\n[Error: auth_expired — re-login required]"}, "finish_reason": "error"}],
            })
        except GenerationStuckError as e:
            # Generation stalled mid-stream (status locked at 200). Surface the
            # phase + duration so the client can decide whether to retry.
            logger.warning("Mid-stream generation stuck: %s", e)
            await self._send_sse(resp, {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": f"\n\n[Error: generation_stuck — stalled in {e.phase} for {e.stalled_for_s:.0f}s]"}, "finish_reason": "error"}],
            })
        except Exception as e:
            logger.error("Stream error: %s", e)
            await self._send_sse(resp, {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": f"\n\n[Error: {e}]"}, "finish_reason": "error"}],
            })

        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    @staticmethod
    async def _send_sse(resp: web.StreamResponse, data: dict) -> None:
        await resp.write(f"data: {json.dumps(data)}\n\n".encode())

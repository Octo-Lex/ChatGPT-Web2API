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
from typing import Optional

from aiohttp import web

from .cdp_driver import CDPDriver
from .config import Config

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
        self._lock = asyncio.Lock()
        self._request_count = 0
        # Track last conversation for multi-turn continuity
        self._last_conv_id: Optional[str] = None
        self._last_project_id: Optional[str] = None

        self.app = web.Application(client_max_size=10 * 1024 * 1024)
        self.app.router.add_post("/v1/chat/completions", self._handle_chat)
        self.app.router.add_post("/chat/completions", self._handle_chat)
        self.app.router.add_get("/v1/models", self._handle_models)
        self.app.router.add_get("/v1/projects", self._handle_projects)
        self.app.router.add_get("/health", self._handle_health)
        self.app.router.add_get("/", self._handle_health)

    # ── Auth ──────────────────────────────────────────────────

    def _check_auth(self, request: web.Request) -> Optional[web.Response]:
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
        return web.json_response({
            "status": "ok" if self._driver.is_connected else "waiting",
            "cdp_connected": self._driver.is_connected,
            "requests_served": self._request_count,
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

        # Serialize — one request at a time through the browser
        async with self._lock:
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
                    # Same session, same project, no system prompt override — continue
                    logger.info("Continuing conversation: %s", self._last_conv_id)
                    await asyncio.sleep(2)  # Let the page settle
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
                return web.json_response(
                    {"error": {"message": str(e), "type": "server_error"}},
                    status=500,
                )

    # ── Response formatters ───────────────────────────────────

    async def _full_response(
        self, request: web.Request, model: str, text: str, timeout: float
    ) -> web.Response:
        """Non-streaming: collect all chunks, return one JSON."""
        full_text = ""
        async for chunk in self._driver.send_and_stream(text, timeout=timeout):
            full_text += chunk.delta

        conv_id = self._driver._current_conv_id or ""
        self._last_conv_id = conv_id

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
        """Streaming: SSE chunks as they arrive."""
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
                    await self._send_sse(resp, {
                        "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                        "conversation_id": conv_id,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": chunk.finish_reason}],
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

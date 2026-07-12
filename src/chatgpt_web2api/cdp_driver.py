 a ChatGPT project by ID. Delegated to BackendClient (Phase 5 PR1)."""
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

    def _assert_owned_tab_required(self) -> None:
        """Fail-closed owned-tab enforcement for parallel mode.

        Raises ``OwnedTabRequiredError`` if ``parallel_tabs`` is on but the
        driver has no owned target. Called at the top of ``send_and_stream``
        as belt-and-suspenders (the resolver/drift guard at the lock site is
        the primary gate). Surfaces as REST 503 / MCP isError=True.
        """
        if self._parallel_tabs and not self.has_owned_target:
            raise OwnedTabRequiredError(
                "parallel_tabs=true requires an owned tab target, but the "
                f"driver has none (tab_mode={self.tab_mode!r}, "
                f"owns_target={self._owns_target}, "
                f"target_id={self._target_id!r})"
            )

    def _assert_reconnect_target_stable(self, pre_target_id: str | None) -> None:
        """Reconnect drift guard (PR4): raise if the owned target changed.

        Called after a successful reconnect. In parallel mode, a reconnect that
        ends on a DIFFERENT target than it started means any in-flight mutation
        holding the old target's lock no longer names the active tab. Fail
        retryably so the caller re-resolves and re-locks. Factored as a method
        so the guard is unit-testable without driving the full WS chain.
        """
        if (
            self._parallel_tabs
            and pre_target_id is not None
            and self._target_id is not None
            and self._target_id != pre_target_id
        ):
            raise OwnedTabRequiredError(
                f"Owned target changed during reconnect "
                f"({pre_target_id} -> {self._target_id}); retry the mutation "
                f"so it re-resolves the lock key"
            )

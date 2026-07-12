"""ChatGPT DOM interaction — composer/send-button/rate-limit DOM helpers.

Phase 5 PR3 extraction (no behavior change). Owns the ChatGPT-composer
interaction surface that was previously inlined in ``CDPDriver``:

  - composer selectors (``COMPOSER_SELECTOR`` etc.)
  - composer presence / send-readiness (``_has_composer``,
    ``_wait_for_composer``, ``_ensure_send_ready``)
  - message typing / verification (``type_message``,
    ``_detect_select_all_modifier``, ``_verify_composer_text``)
  - send button click (``click_send``)
  - rate-limit popup dismissal (``dismiss_rate_limit``)
  - selector-drift diagnostics (``_capture_selector_diagnostic``)

The driver-reference collaborator seam: ``ChatGPTDom`` holds a reference to
its owning ``CDPDriver`` and reaches through it for the CDP transport
(``_js`` / ``_js_strict`` / ``_cdp``), for shared mutable state
(``_breakers``, ``_current_conv_id``), and for navigation peers
(``navigate_new_chat``). None of that state migrates into this module — it
stays on the driver so external attribute reads and test stubs keep working
unchanged.

Boundary: this module is the ChatGPT page DOM-interaction layer. Connection
lifecycle, navigation (``navigate_new_chat`` / ``navigate_conversation`` /
``navigate_gpt`` / ``select_model``), ``send_and_stream`` orchestration, and
conversation-id ownership stay in ``cdp_driver.py``.

Breaker rule: three methods record ``BreakerKind.COMPOSER_SEND_READINESS``
failures/successes. The breaker registry STAYS on the driver; this module
only calls ``record_failure`` / ``record_success`` through
``self._driver._breakers``. No breaker-policy code moves.

Call-rule inside ChatGPTDom method bodies — every internal call routes
through ``self._driver`` (NOT ``self``) to preserve monkeypatch interception
on the driver-facing seam (the PR2 lesson):

  transport:        self._driver._js(...)
                    self._driver._js_strict(...)
                    self._driver._cdp(...)
  moved DOM peers:  self._driver._verify_composer_text(...)
                    self._driver._detect_select_all_modifier(...)
                    self._driver._has_composer(...)
                    self._driver._capture_selector_diagnostic(...)
  navigation:       self._driver.navigate_new_chat(...)
  breaker:          self._driver._breakers.record_failure(...)
                    self._driver._breakers.record_success(...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata

from .breakers import BreakerKind

logger = logging.getLogger(__name__)


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
# P2.5: broader fallback — a submit-type button inside the COMPOSER FORM
# (scoped to the form containing #prompt-textarea, not page-global, to avoid
# hitting login/search/feedback forms if the page is displaced). If ChatGPT
# changes the aria-label (selector drift), a submit-type button inside the
# composer form is still the send affordance. This is the LAST resort.
SEND_BUTTON_BROAD_SELECTOR = (
    'form:has(#prompt-textarea) button[type="submit"],'
    'form:has(.ProseMirror) button[type="submit"]'
)

# Send-button readiness poll. After a prior send completes (or under parallel
# mode, where the MutationLock releases the instant a send finishes), ChatGPT's
# composer can take several seconds to re-enable the send button — the composer
# stays in a "sending"/"regenerating" state with the button disabled or absent
# until the UI settles. The historical 3s budget (range(10) * 0.3s) was too
# tight for back-to-back same-tab sends and surfaced as intermittent
# "no send button" 500s. 10s covers observed reset times without hanging
# indefinitely on a genuinely broken composer.
SEND_BUTTON_POLL_INTERVAL_S = 0.3
SEND_BUTTON_POLL_MAX_WAIT_S = 10.0


class ChatGPTDom:
    """ChatGPT-composer DOM interaction, composed by ``CDPDriver``.

    Constructed once in ``CDPDriver.__init__`` and stored as
    ``self._dom``. The driver keeps thin delegating methods for every
    method here so its public/private API surface is byte-identical to
    pre-extraction.
    """

    def __init__(self, driver) -> None:
        self._driver = driver

    # ── Send readiness ────────────────────────────────────────

    async def _has_composer(self) -> bool:
        """Is a send-capable composer present on the live tab?

        True only when one of the known composer selectors matches. The home/
        landing page is auth-valid but NOT send-valid: it has a different,
        unnamed textarea that none of these selectors match, so a tab on
        ``chatgpt.com/`` (or any non-chat page) returns False. Authentication
        and send-readiness are separate invariants — this one is send-readiness.
        """
        # Imported lazily to avoid a module-load circular dependency.
        from .cdp_driver import CDPJSError

        d = self._driver
        try:
            result = await d._js(
                "(function(){"
                f"  return JSON.stringify({{"
                f"    ready: !!document.querySelector('{COMPOSER_SELECTOR}')"
                f"         || !!document.querySelector('{COMPOSER_FALLBACK_SELECTOR}')"
                "  });"  # {{ opens the object literal; a single } closes it
                "})()"
            )
            return json.loads(result).get("ready") is True
        except (json.JSONDecodeError, TypeError, CDPJSError):
            return False

    async def _wait_for_composer(self, timeout: float = 8) -> bool:
        """Poll until a composer appears, or *timeout* seconds elapse.

        Returns True if a composer is present within the window, False on
        timeout. Never raises — callers decide fail-closed behavior. The
        composer on the home shell hydrates a few seconds after the sidebar,
        so a single probe races it; this wait absorbs that render delay.
        """
        d = self._driver
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await d._has_composer():
                return True
            await asyncio.sleep(0.5)
        return False

    async def _ensure_send_ready(self) -> None:
        """Guarantee the live tab can accept a typed message.

        ``connect()`` may attach to a ``chatgpt.com/`` home/landing tab (or
        adopt an arbitrary existing ChatGPT tab). Such a tab is auth-valid but
        not send-valid: it lacks the real composer, so the very next
        ``type_message`` would raise "No composer found" and — through the REST
        layer — surface as an opaque "no close frame received or sent" 500.
        This normalizes the tab into a usable chat page before ``connect()``
        returns, establishing the invariant the rest of the driver assumes:
        a connected driver is a send-capable driver.

        The composer renders lazily on the home shell (the sidebar hydrates
        first, the composer seconds later), so a single ``_has_composer``
        probe races the render. We poll briefly first; only if that window
        passes without a composer do we navigate (to ``?model=auto``, which
        renders the real composer — see ``navigate_new_chat``) and re-check.
        """
        d = self._driver
        if await d._wait_for_composer(timeout=8):
            return
        logger.info(
            "Attached tab has no composer after waiting; navigating to a new chat "
            "to become send-ready"
        )
        await d.navigate_new_chat()  # navigates to ?model=auto + polls composer
        if not await d._wait_for_composer(timeout=5):
            await d._capture_selector_diagnostic("composer (connect send-ready)")
            if d._breakers:
                d._breakers.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
            from .cdp_driver import SendReadinessError

            raise SendReadinessError("No composer found after navigating to a new chat")

    # ── Message typing ────────────────────────────────────────

    async def type_message(self, text: str) -> None:
        """Type text into the ChatGPT composer.

        The new composer is a contenteditable ProseMirror div; the legacy
        composer was a <textarea id="prompt-textarea">. We focus the new
        textbox first (COMPOSER_SELECTOR), falling back to the textarea
        for older deployments. Once focused, ``Input.insertText`` routes
        the text to whichever element holds focus, so the same insert
        works for both layouts.
        """
        d = self._driver
        # Focus the composer. Try the ProseMirror textbox first, then the
        # legacy textarea fallback. Returns which one was focused (or
        # 'no composer') so the verify step reads the right element.
        focus_result = await d._js(
            "(function() {"
            f"  var el = document.querySelector('{COMPOSER_SELECTOR}');"
            "  if (el) { el.focus(); return 'composer'; }"
            f"  var fb = document.querySelector('{COMPOSER_FALLBACK_SELECTOR}');"
            "  if (fb) { fb.focus(); return 'fallback'; }"
            "  return 'no composer';"
            "})()"
        )
        if focus_result == "no composer":
            await d._capture_selector_diagnostic("composer (type_message)")
            if d._breakers:
                d._breakers.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
            from .cdp_driver import SendReadinessError

            raise SendReadinessError("No composer found")
        focused_target = focus_result  # 'composer' or 'fallback'

        # Clear existing text and insert. Prefer a platform-aware select-all
        # via keyboard events (modifiers: 2 = Ctrl on Win/Linux, 4 = Cmd on
        # macOS — detected at runtime so select-all doesn't silently no-op on
        # Mac). Insert via CDP, dispatched to the focused composer.
        select_all_mods = await d._detect_select_all_modifier()
        await d._cdp(
            "Input.dispatchKeyEvent",
            {
                "type": "rawKeyDown",
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "modifiers": select_all_mods,
            },
        )
        await d._cdp(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "modifiers": select_all_mods,
            },
        )
        await asyncio.sleep(0.1)
        await d._cdp("Input.insertText", {"text": text})
        await asyncio.sleep(0.5)

        # Verify the composer holds EXACTLY the intended input (canonicalized),
        # not just "is non-empty". With ProseMirror contenteditable, a stale or
        # partially-cleared composer passes a non-empty check while corrupting
        # the prompt — so we compare canonical editor-visible text. On mismatch,
        # retry once: clear via execCommand('selectAll') + delete (ProseMirror
        # sees editor-like input events), re-insert, re-verify. Only then raise.
        verify_selector = (
            COMPOSER_SELECTOR if focused_target == "composer" else COMPOSER_FALLBACK_SELECTOR
        )
        if not await d._verify_composer_text(verify_selector, text):
            logger.warning(
                "Composer text mismatch on first insert; retrying with execCommand clear"
            )
            await d._js_strict(
                "(function(){"
                f"  var el = document.querySelector('{verify_selector}');"
                "  if (el) {"
                "    if (el.tagName === 'TEXTAREA') {"
                "      el.focus(); el.select();"
                "      try { document.execCommand('delete'); } catch(e) {}"
                "    } else {"
                "      el.focus();"
                "      var sel = window.getSelection(); sel.removeAllRanges();"
                "      var range = document.createRange(); range.selectNodeContents(el);"
                "      sel.addRange(range);"
                "      try { document.execCommand('delete'); } catch(e) {}"
                "    }"
                "  }"
                "  return true;"
                "})()"
            )
            await asyncio.sleep(0.1)
            await d._cdp("Input.insertText", {"text": text})
            await asyncio.sleep(0.5)
            if not await d._verify_composer_text(verify_selector, text):
                if d._breakers:
                    d._breakers.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
                from .cdp_driver import SendReadinessError

                raise SendReadinessError(
                    f"Composer text verification failed after retry; expected {text[:60]!r}"
                )
        logger.info("Typed: %s", text[:80])

    async def _detect_select_all_modifier(self) -> int:
        """Return the CDP modifiers value for select-all on the live platform.

        ``2`` = Ctrl (Windows/Linux), ``4`` = Cmd (macOS). Probed once via
        ``navigator.userAgentData``/``navigator.platform`` so the keyboard
        select-all doesn't silently no-op on macOS (where Cmd, not Ctrl,
        selects all). Falls back to Ctrl (2) on any probe failure.
        """
        # Imported lazily to avoid a module-load circular dependency.
        from .cdp_driver import CDPJSError

        d = self._driver
        try:
            ua = await d._js_strict(
                "(navigator.userAgentData && navigator.userAgentData.platform)"
                " || navigator.platform || ''"
            )
            if ua and "mac" in ua.lower():
                return 4
        except CDPJSError:
            pass
        return 2

    async def _verify_composer_text(self, selector: str, expected: str) -> bool:
        """Canonical-equality check: does the composer hold *expected*?

        Compares editor-visible text with canonical normalization (NFC, CRLF→LF,
        NBSP→space) and tolerates at most one editor-added trailing newline.

        For a contenteditable ProseMirror div, neither ``innerText`` nor
        ``textContent`` reconstructs what the user typed across line breaks.
        The fix is a recursive DOM extractor that:
        - Walks all descendant nodes (not just immediate children)
        - Emits ``\\n`` for ``<br>`` elements
        - Joins top-level block children with ``\\n``
        - Does NOT strip the user's trailing newline (the Python-side
          tolerance handles editor-added trailing newlines)

        Legacy ``<textarea>`` still reads ``.value``, which is already exact.

        P0 (2026-07-12): ChatGPT code review (conv 6a52f0f3) found 4 defects:
        1. <br> inside <p> was invisible to textContent (lost newlines)
        2. Missing NFC normalization (combining accents failed)
        3. Unconditional trailing-newline strip corrupted user's trailing \\n
        4. Fixed 500ms delay instead of bounded polling
        """
        # Imported lazily to avoid a module-load circular dependency.
        from .cdp_driver import CDPJSError

        d = self._driver
        try:
            actual = await d._js_strict(
                "(function(){"
                f"  var el = document.querySelector('{selector}');"
                "  if (!el) return '';"
                "  if (el.tagName === 'TEXTAREA') return el.value;"
                # Recursive DOM extractor: walks all descendant nodes,
                # emitting \n for <br> elements. Correctly handles
                # <p>line1<br>line2</p> which textContent renders as
                # "line1line2". (ChatGPT review, conv 6a52f0f3.)
                #
                # Scope: handles root-level <p>/<div> blocks with inline
                # descendants (what Input.insertText produces). Does NOT
                # reconstruct nested block boundaries (blockquote, lists) —
                # if ChatGPT changes its composer to nest blocks, this needs
                # a block-aware recursion update.
                "  function isPlaceholderBreakBlock(node) {"
                "    return ("
                "      node.nodeType === 1 &&"
                "      node.childNodes.length === 1 &&"
                "      node.firstChild.nodeType === 1 &&"
                "      node.firstChild.tagName === 'BR'"
                "    );"
                "  }"
                "  function extractText(node) {"
                "    if (node.nodeType === 3) return (node.nodeValue || '').replace(/\\u00a0/g, ' ');"
                "    if (node.nodeType !== 1) return '';"
                "    if (node.tagName === 'BR') return '\\n';"
                "    var text = '';"
                "    for (var i = 0; i < node.childNodes.length; i++) {"
                "      text += extractText(node.childNodes[i]);"
                "    }"
                "    return text;"
                "  }"
                # Walk immediate children of the composer element.
                # Each block-level child contributes its extracted text.
                # A placeholder <br>-only block (<p><br></p>) is treated as
                # an empty string so the join produces the correct number of
                # newlines (one from the join, not one from the <br> too).
                "  var parts = [];"
                "  for (var i = 0; i < el.childNodes.length; i++) {"
                "    var child = el.childNodes[i];"
                "    if (child.nodeType === 3) {"
                "      var t = (child.nodeValue || '').replace(/\\u00a0/g, ' ');"
                "      if (t) parts.push(t);"
                "    } else if (child.nodeType === 1) {"
                "      parts.push(isPlaceholderBreakBlock(child) ? '' : extractText(child));"
                "    }"
                "  }"
                "  return parts.join('\\n');"
                "})()"
            )
        except CDPJSError:
            return False
        if not actual and expected:
            return False
        # NFC normalization: handles composed/decomposed Unicode sequences
        # (e.g., é as 'e'+U+0301 vs U+00E9). The turn-anchor matcher already
        # uses NFC; this brings the verifier to parity.
        canon_actual = unicodedata.normalize("NFC", actual.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "))
        canon_expected = unicodedata.normalize("NFC", expected.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "))
        # ProseMirror wraps input in a <p> and may append a trailing block
        # newline. Tolerate AT MOST ONE editor-added trailing newline — but
        # never strip a user-intended trailing newline. So accept an exact
        # match, OR actual == expected + one editor newline.
        return canon_actual == canon_expected or canon_actual == canon_expected + "\n"

    async def click_send(self) -> None:
        """Click the send button via JS MouseEvent sequence.

        The new composer has no ``data-testid="send-button"``; its send
        affordance is the submit ``<button aria-label="Send ...">`` inside
        the composer form. We prefer that, falling back to the legacy
        testid selector for older deployments. The stop button (which
        appears during generation) is explicitly excluded — it also
        carries an aria-label, but never "Send".
        """
        d = self._driver
        # Wait for the send button to appear and be enabled. Try the new
        # aria-label selector first, then the legacy testid fallback. Time-
        # budgeted (not a fixed iteration count) so the wait scales to the
        # composer-reset window after a prior send — see
        # SEND_BUTTON_POLL_MAX_WAIT_S for rationale.
        deadline = time.monotonic() + SEND_BUTTON_POLL_MAX_WAIT_S
        while time.monotonic() < deadline:
            has_btn = await d._js(
                "(function() {"
                f"  var btn = document.querySelector('{SEND_BUTTON_SELECTOR}')"
                f"       || document.querySelector('{SEND_BUTTON_FALLBACK_SELECTOR}')"
                f"       || document.querySelector('{SEND_BUTTON_BROAD_SELECTOR}');"
                "  return btn && !btn.disabled ? 'yes' : 'no';"
                "})()"
            )
            if has_btn == "yes":
                break
            await asyncio.sleep(SEND_BUTTON_POLL_INTERVAL_S)

        result = await d._js(
            "(function() {"
            f"  var btn = document.querySelector('{SEND_BUTTON_SELECTOR}')"
            f"       || document.querySelector('{SEND_BUTTON_FALLBACK_SELECTOR}')"
            f"       || document.querySelector('{SEND_BUTTON_BROAD_SELECTOR}');"
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
            await d._capture_selector_diagnostic("send-button (click_send)")
            if d._breakers:
                d._breakers.record_failure(BreakerKind.COMPOSER_SEND_READINESS)
            from .cdp_driver import SendReadinessError

            raise SendReadinessError(f"Send failed: {result}")
        logger.info("Message sent")
        # Success: clear composer failure history and recover a half-open
        # breaker. Only after the message is confirmed sent — not after
        # type_message alone, since a successful type can still fail to send.
        if d._breakers:
            d._breakers.record_success(BreakerKind.COMPOSER_SEND_READINESS)

    # ── Rate-limit popup ──────────────────────────────────────

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
        d = self._driver
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
            click_raw = await d._js_strict(click_js, timeout=10)
            clicked = json.loads(click_raw).get("clicked", False) if click_raw else False
        except Exception:  # best-effort: never raise
            logger.warning("dismiss_rate_limit: click failed", exc_info=True)
            return None  # unknown — don't trigger retry storm
        if not clicked:
            return False

        # Re-scan to confirm the pop-up cleared.
        try:
            scan = await d._js_strict(
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
        from .cdp_driver import is_rate_limited_text

        return not is_rate_limited_text(text)

    # ── Diagnostics ───────────────────────────────────────────

    async def _capture_selector_diagnostic(self, selector_name: str) -> None:
        """#5 / P2.5: Capture DOM state when a selector fails to match.

        Logs a rich diagnostic snapshot so selector drift and send-readiness
        failures are diagnosable without W2A_DIAGNOSE=1. Called at the point of
        selector failure (e.g. 'no send button', 'No textarea'). Best-effort —
        never raises.

        P2.5 (2026-07-09): expanded from url/title/body/button_count to include
        send-readiness-specific fields (ChatGPT's proposed readiness snapshot):
        - composer_found / composer_text_length / composer_enabled: distinguishes
          "text didn't land" (injection failed) from "selector drifted"
        - send_candidates_count / enabled_send_candidates_count: how many buttons
          match a broad send heuristic, and how many are enabled
        - stop_button_present / generating_indicator_present: is a generation
          in progress (the send button is replaced by stop during generation)
        """
        d = self._driver
        try:
            snapshot = await d._js_strict(
                "(function(){"
                "  var composer = document.querySelector('" + COMPOSER_SELECTOR + "')"
                "       || document.querySelector('" + COMPOSER_FALLBACK_SELECTOR + "');"
                "  var sendCandidates = document.querySelectorAll('button[type=\"submit\"], button[aria-label*=\"Send\" i], button[data-testid=\"send-button\"]');"
                "  var enabledSend = Array.prototype.filter.call(sendCandidates, function(b){ return !b.disabled; });"
                "  var stopBtn = document.querySelector('[data-testid=\"stop-button\"], button[aria-label*=\"Stop\" i]');"
                "  return JSON.stringify({"
                "    url: location.href,"
                "    title: document.title,"
                "    body_preview: (document.body && document.body.innerText || '').slice(0, 300),"
                "    button_count: document.querySelectorAll('button').length,"
                "    textarea_count: document.querySelectorAll('textarea').length,"
                "    composer_found: !!composer,"
                "    composer_text_length: composer ? (composer.innerText || composer.value || '').length : 0,"
                "    composer_enabled: composer ? !composer.disabled : false,"
                "    send_candidates_count: sendCandidates.length,"
                "    enabled_send_candidates_count: enabledSend.length,"
                "    stop_button_present: !!stopBtn,"
                "    generating_indicator_present: !!document.querySelector('[class*=\"result-thinking\"], [class*=\"generating\"]')"
                "  });"
                "})()",
                timeout=5,
            )
            logger.warning("Selector drift diagnostic (%s): %s", selector_name, snapshot)
        except Exception:
            logger.warning("Selector drift diagnostic (%s): capture failed", selector_name)

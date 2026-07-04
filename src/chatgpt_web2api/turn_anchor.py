"""A2 turn anchor — pure-Python turn-correlation types and selectors.

This module is CDP-free: it operates on the projected mapping dict produced by
``backend_projection.CONVERSATION_PROJECTION_JS`` and a ``TurnAnchor`` captured
by the driver/identity-listener. Everything here is unit-testable without a
real browser.

Two-tier correlation (peer-reviewed, conv ``6a482cfd``):

  Primary: observed client-message-ID.
    ``TurnAnchor.captured_user_message_id`` (from the IdentityListener) exactly
    identifies the submitted user node in the backend mapping. The selector
    finds ``mapping[message.id == captured_id]`` and walks to its terminal
    assistant response.

  Fallback: dual-anchor (when capture failed).
    - ``existing_conversation``: sent_text + pre-send backend node-id/time.
    - ``degraded_existing``: sent_text + wall-clock freshness (TOL=8s, lower-
      bound guard only — NOT a rapid-same-text discriminator).
    - ``fresh_chat``: sent_text-only until conv_id resolves.

Selector rules (ChatGPT round 4):
  - Resolve the user node first, then walk to its assistant response.
  - Terminal selection: NEWEST ``end_turn=true`` assistant text descendant
    (NOT first descendant — ChatGPT caught that "first" returns drafts).
  - Non-text completion only when ``had_non_text_content`` is True (DOM guard).
  - Bidirectional traversal: children-walk primary, parent-pointer fallback.

Internal status vocabulary collapses to tri-state for the detector:
  complete ← matched_text_complete, matched_non_text_complete (with DOM guard)
  not_ready ← not_ready, ambiguous, degraded_not_fresh
  fetch_failed ← transport_failed
  auth_failed propagates as AuthExpiredError (never degrades).
"""
from __future__ import annotations

# ── Constants ─────────────────────────────────────────────────────────────
# Empirically derived (Phase 0.5 + stress test 4): backend create_time leads
# local wall clock by ~5.1s (mean 5.17, std 0.119, all positive). The freshness
# floor is a lower-bound guard for degraded mode — it rejects sufficiently-old
# nodes but NOT rapid same-text repeats (a previous turn sent up to ~13s prior
# can still pass). For rapid same-text repeats, the selector returns ``ambiguous``
# or ``not_ready`` rather than silently picking. Env-overridable for canary tuning.
import os as _os
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Literal

try:
    SKEW_TOLERANCE_SECONDS = float(
        _os.getenv("W2A_TURN_DEGRADED_SKEW_TOLERANCE_SECONDS", "8.0")
    )
except (ValueError, TypeError):
    SKEW_TOLERANCE_SECONDS = 8.0

# Matcher thresholds (ChatGPT round 1). Full normalized equality for prompts
# up to this length; truncated-prefix acceptance for longer prompts (backend
# truncation). No tiny prefixes — agent prompts share boilerplate.
_PREFIX_MATCH_MIN = 1024


# ── Anchor mode ───────────────────────────────────────────────────────────

AnchorMode = Literal[
    "captured_id",          # primary: IdentityListener captured the UUID
    "existing_conversation",  # fallback: backend node-id/time anchor
    "degraded_existing",    # fallback: sent_text + wall-clock freshness
    "fresh_chat",           # fallback: sent_text-only until conv_id resolves
]


@dataclass(frozen=True)
class TurnAnchor:
    """Immutable snapshot of the conversation state around one send.

    Constructed by the driver in ``send_and_stream``. The ``captured_user_message_id``
    field is populated AFTER ``click_send`` (the UUID only exists in the POST
    that the send generates) via ``with_captured_id``. Selector priority: if
    ``captured_user_message_id`` is set, use ID-based correlation; else use the
    fallback mode.
    """

    sent_text: str
    mode: AnchorMode
    # Primary path: the client UUID observed by the IdentityListener. None
    # until the POST is captured (or None for the whole send if capture failed).
    captured_user_message_id: str | None = None
    # Fallback path: pre-send backend anchors (existing_conversation only).
    latest_user_node_id: str | None = None
    latest_user_create_time: float | None = None
    latest_assistant_node_id: str | None = None
    latest_assistant_create_time: float | None = None
    # Degraded path: local wall-clock captured before click_send.
    pre_send_wall_time: float | None = None
    # Common: which conversation this anchor belongs to (None on fresh chat).
    conversation_id_at_capture: str | None = None

    def with_captured_id(self, uuid: str | None) -> TurnAnchor:
        """Return an immutable copy with the captured UUID set.

        Called after ``click_send`` + ``wait_for_captured_uuid``. If ``uuid``
        is None (capture failed), the anchor is returned unchanged (fallback
        mode stays in effect).
        """
        if uuid is None:
            return self
        return replace(self, captured_user_message_id=uuid)


# ── Result types ──────────────────────────────────────────────────────────

# Full internal status vocabulary (kept rich for canary diagnostics).
TextStatus = Literal[
    "matched",              # terminal assistant text found, end_turn=true
    "not_ready",            # correlated node exists but not done, or no candidate yet
    "ambiguous",            # ≥2 matching user nodes; keep polling
    "degraded_not_fresh",   # single match but fails freshness floor; keep polling
    "non_text",             # correlated assistant is non-text (image/tool/etc.)
    "fetch_failed",         # transport error — detector may unlock DOM fallback
    "auth_failed",          # 401 — hard fail, never degrades
]


# Detector-facing tri-state (the completion detector's gate at
# completion_detector.py:446-487 stays 3-way).
EndTurnStatus = Literal["complete", "not_ready", "fetch_failed"]


@dataclass
class TurnTextResult:
    """Result of ``_fetch_text_for_turn``. Rich for canary diagnostics."""

    status: TextStatus
    text: str | None = None
    diagnostic: dict = field(default_factory=dict)


@dataclass
class TurnEndResult:
    """Result of ``_fetch_end_turn_for_turn``, before tri-state collapse."""

    status: TextStatus
    diagnostic: dict = field(default_factory=dict)


# ── Errors ────────────────────────────────────────────────────────────────

class TurnAnchorUnavailableError(RuntimeError):
    """Pre-send anchor capture failed on an existing conversation.

    Raised by ``_capture_pre_send_fallback_anchor`` when the backend anchor
    fetch fails after bounded retries on an existing conversation where
    degraded mode is not acceptable (e.g., the failure is auth, not transient).
    For transient failures, the driver enters ``degraded_existing`` mode
    instead of raising.
    """


class TurnReconciliationError(RuntimeError):
    """Post-send correlation timed out without resolving a terminal response.

    Raised by ``send_and_stream``'s final reconciliation loop when the
    anchored selector never returns ``matched`` within the polling budget.
    Carries a diagnostic summary (anchor mode, last status, candidate counts)
    — never the raw prompt or full assistant text.
    """

    def __init__(self, conversation_id: str, anchor_mode: str, last_status: str,
                 diagnostic: dict) -> None:
        self.conversation_id = conversation_id
        self.anchor_mode = anchor_mode
        self.last_status = last_status
        self.diagnostic = diagnostic
        super().__init__(
            f"Turn reconciliation failed for {conversation_id} "
            f"(mode={anchor_mode}, last_status={last_status})"
        )


# ── Text normalization + matching ─────────────────────────────────────────

def normalize_text(s: str) -> str:
    """Normalize text for matching: NFC, CRLF→LF, strip trailing ws + zero-width.

    The backend may store a slightly different representation of the sent
    prompt (Unicode normalization, line-ending differences, trailing
    whitespace, zero-width joiners). This normalization makes matching
    robust to those differences.
    """
    if not s:
        return ""
    # NFC normalization (compose decomposed characters).
    s = unicodedata.normalize("NFC", s)
    # CRLF / CR → LF.
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Strip zero-width characters (ZWSP, ZWJ, ZWNJ, BOM).
    s = "".join(ch for ch in s if ch not in ("\u200b", "\u200d", "\u200c", "\ufeff"))
    # Strip trailing whitespace per line + overall.
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return s.strip()


def user_text_matches_sent(parent_text: str, sent_text: str) -> bool:
    """Does a backend user-node's text match the sent prompt?

    - Full normalized equality for prompts ≤ ``_PREFIX_MATCH_MIN`` chars.
    - Truncated-prefix acceptance for longer prompts (backend truncation):
      if the parent text is ≥ ``_PREFIX_MATCH_MIN`` and the sent text starts
      with it (or vice versa), accept.
    - No tiny prefixes — agent prompts share long boilerplate prefixes, so a
      short shared prefix is not distinctive enough to match on.
    """
    p = normalize_text(parent_text)
    s = normalize_text(sent_text)
    if not p or not s:
        return False
    if p == s:
        return True
    # Truncated-prefix: backend stores a long prefix of the submitted prompt.
    # Accept only if the prefix is long enough to be distinctive.
    if len(p) >= _PREFIX_MATCH_MIN and s.startswith(p):
        return True
    # Defensive: sent may be the truncated one.
    if len(s) >= _PREFIX_MATCH_MIN and p.startswith(s):
        return True
    return False


# ── Selectors ─────────────────────────────────────────────────────────────

def _node_text(node: dict) -> str:
    """Extract joined text from a projected or raw node.

    Projected shape: ``node.text`` (already joined by the projection JS).
    Raw shape: ``node.message.content.parts`` (joined here).
    """
    # Projected: text is at top level.
    text = node.get("text")
    if text is not None:
        return text
    # Raw backend shape.
    msg = node.get("message") or {}
    content = msg.get("content") or {}
    parts = content.get("parts") or []
    return "\n".join(str(p) for p in parts if isinstance(p, str))


def _node_role(node: dict) -> str:
    """Role from projected (node.role) or raw (node.message.author.role)."""
    role = node.get("role")
    if role:
        return role
    return ((node.get("message") or {}).get("author") or {}).get("role", "")


def _node_create_time(node: dict) -> float:
    """create_time from projected (node.create_time) or raw."""
    ct = node.get("create_time")
    if ct is not None:
        return float(ct)
    return float((node.get("message") or {}).get("create_time") or 0.0)


def _node_end_turn(node: dict) -> bool:
    """end_turn from projected (node.end_turn) or raw."""
    if "end_turn" in node:
        return bool(node.get("end_turn"))
    return bool((node.get("message") or {}).get("end_turn"))


def _node_content_type(node: dict) -> str:
    """content_type from projected (node.content_type) or raw."""
    ct = node.get("content_type")
    if ct:
        return ct
    return ((node.get("message") or {}).get("content") or {}).get("content_type", "")


def _node_id(node: dict) -> str:
    """id from projected (node.id) or raw (node.message.id)."""
    nid = node.get("id")
    if nid:
        return nid
    return (node.get("message") or {}).get("id") or ""


def _resolve_user_node(mapping: dict, anchor: TurnAnchor) -> tuple[str | None, str]:
    """Resolve the submitted user node from the projected mapping.

    Returns ``(node_id, reason)`` where ``node_id`` is the matched user node's
    id (or None) and ``reason`` explains the outcome for diagnostics.

    Primary path: exact match on ``captured_user_message_id``.
    Fallback path: text match + freshness/anchor disambiguation.
    """
    nodes = mapping.get("nodes") or mapping.get("mapping") or {}

    # Primary: ID-based.
    if anchor.captured_user_message_id:
        target = anchor.captured_user_message_id
        for nid, node in nodes.items():
            if _node_role(node) != "user":
                continue
            if _node_id(node) == target or nid == target:
                return nid, "id_match"
        # ID was captured but not found in the projected window yet.
        return None, "id_not_yet_in_mapping"

    # Fallback: text-based.
    matching = []
    for nid, node in nodes.items():
        if _node_role(node) != "user":
            continue
        if user_text_matches_sent(_node_text(node), anchor.sent_text):
            matching.append((nid, node))

    if not matching:
        return None, "no_text_match"

    # Filter by anchor (existing_conversation: newer/different than pre-send).
    if anchor.mode == "existing_conversation":
        fresh = []
        for nid, node in matching:
            ct = _node_create_time(node)
            mid = _node_id(node)
            # Newer than pre-send latest user, OR different node id.
            if (anchor.latest_user_create_time is not None
                    and ct > anchor.latest_user_create_time):
                fresh.append((nid, node))
            elif (anchor.latest_user_node_id is not None
                  and mid != anchor.latest_user_node_id
                  and ct >= (anchor.latest_user_create_time or 0.0)):
                fresh.append((nid, node))
        if not fresh:
            return None, "no_fresh_text_match"
        if len(fresh) > 1:
            return None, "ambiguous"
        return fresh[0][0], "text_match_with_anchor"

    if anchor.mode == "degraded_existing":
        # Degraded mode is logged, not guessed. Without a captured UUID, a
        # text match + wall-clock freshness cannot distinguish "the new node
        # propagated" from "the previous same-text turn is still within the
        # freshness window." So degraded_existing NEVER resolves a user node —
        # it only classifies what it sees for diagnostics and returns not_ready.
        #
        # The caller keeps polling. When the new node propagates, either:
        # - the ID path resolves it (if the UUID arrives late), or
        # - a second match appears → the caller sees ≥2 candidates → ambiguous,
        #   and the newer-create-time one wins once both fully propagate.
        # If neither happens within the polling budget, the caller raises
        # TurnReconciliationError — never silently returns stale text.
        #
        # (PR #39 review: the prior implementation accepted a single fresh
        # match when no stale alternatives existed. That is the exact
        # rapid-repeat case where the previous turn's node is still within
        # the 8-second freshness tolerance and no new node has propagated yet.)
        fresh = []
        stale = []
        for nid, node in matching:
            ct = _node_create_time(node)
            if anchor.pre_send_wall_time is not None:
                if ct >= anchor.pre_send_wall_time - SKEW_TOLERANCE_SECONDS:
                    fresh.append((nid, node))
                else:
                    stale.append((nid, node))
            else:
                fresh.append((nid, node))  # no wall time; can't freshness-check

        if len(fresh) > 1:
            return None, "ambiguous"
        if fresh and stale:
            return None, "degraded_ambiguous_with_stale"
        if not fresh and stale:
            return None, "degraded_not_fresh"
        # Single fresh match, no stale alternatives. We still do NOT accept —
        # we cannot prove this is the new turn rather than the previous one
        # that happens to be fresh. Return not_ready; the caller keeps polling
        # and either the ID path resolves or TurnReconciliationError fires.
        return None, "degraded_insufficient_evidence"

    if anchor.mode == "fresh_chat":
        # Text-only; first/only match expected on a fresh chat.
        if len(matching) > 1:
            return None, "ambiguous"
        return matching[0][0], "fresh_chat_text_match"

    return None, f"unhandled_mode_{anchor.mode}"


def _find_assistant_descendants(
    mapping: dict, user_nid: str
) -> list[tuple[str, dict]]:
    """Find assistant nodes that are descendants of the user node.

    Bidirectional (ChatGPT round 3):
      Primary: walk ``children`` from the user node.
      Fallback: for each assistant candidate, walk ``parent`` pointers upward
      and accept if the user node is an ancestor.
    """
    nodes = mapping.get("nodes") or mapping.get("mapping") or {}  # type: ignore[assignment]

    # Primary: children-walk (BFS from user node).
    found: dict[str, dict] = {}
    visited: set[str] = set()
    queue = [user_nid]
    while queue:
        cur = queue.pop(0)
        if cur in visited:
            continue
        visited.add(cur)
        node = nodes.get(cur)
        if node is None:
            continue
        if cur != user_nid and _node_role(node) == "assistant":
            found[cur] = node
        children = node.get("children") or []
        for child_id in children:
            if child_id not in visited:
                queue.append(child_id)

    # Fallback: parent-pointer walk from each assistant candidate.
    if not found:
        for nid, node in nodes.items():
            if _node_role(node) != "assistant":
                continue
            # Walk parent pointers upward; if we reach user_nid, it's a descendant.
            cur = node.get("parent")
            seen: set[str] = set()
            while cur and cur not in seen:
                if cur == user_nid:
                    found[nid] = node
                    break
                seen.add(cur)
                parent_node = nodes.get(cur)
                if parent_node is None:
                    break
                cur = parent_node.get("parent")

    return list(found.items())


def select_text_for_turn(mapping: dict, anchor: TurnAnchor) -> TurnTextResult:
    """Resolve the terminal assistant text for the submitted turn.

    Returns a ``TurnTextResult`` with one of:
      - ``matched``: terminal assistant text found (newest ``end_turn=true``).
      - ``not_ready``: correlated user node resolved but no terminal text yet.
      - ``ambiguous``: ≥2 matching user nodes; caller keeps polling.
      - ``degraded_not_fresh``: single stale match; caller keeps polling.
      - ``non_text``: correlated assistant is non-text (image/tool).
    """
    user_nid, reason = _resolve_user_node(mapping, anchor)
    if user_nid is None:
        if reason == "ambiguous":
            return TurnTextResult("ambiguous", diagnostic={"reason": reason})
        if reason == "degraded_not_fresh":
            return TurnTextResult("degraded_not_fresh", diagnostic={"reason": reason})
        return TurnTextResult("not_ready", diagnostic={"reason": reason})

    descendants = _find_assistant_descendants(mapping, user_nid)
    if not descendants:
        return TurnTextResult("not_ready", diagnostic={
            "reason": "no_assistant_descendant", "user_node": user_nid,
        })

    # Terminal selection: NEWEST end_turn=true assistant TEXT descendant.
    text_candidates = [
        (nid, node) for nid, node in descendants
        if _node_content_type(node) == "text" and _node_text(node).strip()
    ]
    if text_candidates:
        end_turn_text = [
            (nid, node) for nid, node in text_candidates if _node_end_turn(node)
        ]
        if end_turn_text:
            # Newest by create_time.
            best = max(end_turn_text, key=lambda pair: _node_create_time(pair[1]))
            return TurnTextResult(
                "matched", text=_node_text(best[1]),
                diagnostic={"user_node": user_nid, "assistant_node": best[0],
                            "reason": "terminal_text_end_turn"},
            )
        # Text candidates exist but none end_turn yet.
        return TurnTextResult("not_ready", diagnostic={
            "reason": "text_not_end_turn", "user_node": user_nid,
            "candidate_count": len(text_candidates),
        })

    # No text candidates — check for non-text assistant (image/tool-use).
    non_text = [
        (nid, node) for nid, node in descendants
        if _node_content_type(node) != "text"
    ]
    if non_text:
        return TurnTextResult("non_text", diagnostic={
            "user_node": user_nid,
            "content_types": [_node_content_type(n) for _, n in non_text],
        })

    return TurnTextResult("not_ready", diagnostic={
        "reason": "descendants_exist_but_no_text", "user_node": user_nid,
    })


def select_end_turn_for_turn(
    mapping: dict, anchor: TurnAnchor, *, had_non_text_content: bool
) -> TurnEndResult:
    """Resolve completion status for the submitted turn.

    Returns a ``TurnEndResult`` with internal status. The caller (detector)
    collapses to tri-state: ``complete`` / ``not_ready`` / ``fetch_failed``.

    Completion rules (ChatGPT round 4):
      - ``matched`` (complete): correlated assistant text node has end_turn=true
        AND non-empty text.
      - ``non_text`` complete only if ``had_non_text_content`` is True.
      - ``not_ready`` / ``ambiguous`` / ``degraded_not_fresh`` → not_ready.
    """
    user_nid, reason = _resolve_user_node(mapping, anchor)
    if user_nid is None:
        if reason == "ambiguous":
            return TurnEndResult("ambiguous", diagnostic={"reason": reason})
        if reason == "degraded_not_fresh":
            return TurnEndResult("degraded_not_fresh", diagnostic={"reason": reason})
        return TurnEndResult("not_ready", diagnostic={"reason": reason})

    descendants = _find_assistant_descendants(mapping, user_nid)
    if not descendants:
        return TurnEndResult("not_ready", diagnostic={
            "reason": "no_assistant_descendant", "user_node": user_nid,
        })

    # Text completion: newest end_turn=true text descendant with non-empty text.
    text_end_turn = [
        (nid, node) for nid, node in descendants
        if _node_content_type(node) == "text"
        and _node_text(node).strip()
        and _node_end_turn(node)
    ]
    if text_end_turn:
        return TurnEndResult("matched", diagnostic={
            "user_node": user_nid,
            "assistant_node": text_end_turn[0][0],
            "reason": "text_end_turn",
        })

    # Non-text completion: correlated non-text assistant with end_turn=true,
    # gated by DOM had_non_text_content.
    non_text_end_turn = [
        (nid, node) for nid, node in descendants
        if _node_content_type(node) != "text" and _node_end_turn(node)
    ]
    if non_text_end_turn and had_non_text_content:
        return TurnEndResult("matched", diagnostic={
            "user_node": user_nid,
            "assistant_node": non_text_end_turn[0][0],
            "reason": "non_text_end_turn_with_dom_guard",
        })

    # Correlated node exists but not done.
    return TurnEndResult("not_ready", diagnostic={
        "reason": "not_end_turn", "user_node": user_nid,
        "descendant_count": len(descendants),
    })


def collapse_to_end_turn_status(result: TurnEndResult) -> EndTurnStatus:
    """Collapse a rich ``TurnEndResult`` to the detector-facing tri-state.

    Mapping (ChatGPT round 4):
      matched → complete
      not_ready, ambiguous, degraded_not_fresh, non_text → not_ready
      fetch_failed → fetch_failed
      auth_failed → propagates as exception upstream (never reaches here)
    """
    if result.status == "matched":
        return "complete"
    if result.status == "fetch_failed":
        return "fetch_failed"
    # not_ready, ambiguous, degraded_not_fresh, non_text → not_ready
    return "not_ready"

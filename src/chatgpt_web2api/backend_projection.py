"""A2 backend projection — the conversation-mapping fetch + projection JS.

Extracted from inline strings (Step 5 of the A2 build sequence). The
projection JS is a named, importable constant so it can be fixture-tested in
isolation (``tests/test_backend_projection_js.py``) without needing the driver.

Design (peer-reviewed, conv ``6a482cfd``):
  - Fetch ``/backend-api/conversation/{id}?offset=0&limit={TURN_PROJECTION_LIMIT}``.
  - Status-decode: 401 → AuthExpiredError+trip, 404 → _Transient404, other → RuntimeError.
  - Project to compact schema preserving ALL nodes as graph skeletons:
    drop heavy payload fields only (reasoning internals, tool metadata, citations,
    assets), NOT the nodes themselves. Intermediary nodes (reasoning_recap,
    tool, system, unknown) must remain traversable.
  - Schema: {nodes: {id: {id, parent, children, role, create_time, end_turn,
    content_type, text}}, current_node}.

The JS is executed via ``driver._js_with_data_strict(CONVERSATION_PROJECTION_JS, ...)``.
The ``__D.conv_id`` and ``__D.token`` data slots are threaded by the caller
(``BackendClient._fetch_recent_conversation_projection``).
"""
from __future__ import annotations

import os

# Empirically validated (Phase 1, stress test 2): the correlated user/assistant
# pair can fall outside the old limit=5 window for multi-step tool chains.
# 50 is the agreed default; env-overridable for canary tuning.
TURN_PROJECTION_LIMIT = int(os.getenv("W2A_TURN_PROJECTION_LIMIT", "50"))


# The projection JS. Runs inside the ChatGPT page via Runtime.evaluate.
#
# Data slots (threaded by the caller via _js_with_data_strict):
#   __D.conv_id  — the conversation id
#   __D.token    — the access token
#   __D.limit    — the node limit (TURN_PROJECTION_LIMIT)
#
# Returns a JSON string. On non-OK HTTP, returns ``{"__status": <code>}``
# so the Python caller can status-decode (the ``__status`` blob convention
# decoded in ``_fetch_recent_conversation_projection``).
CONVERSATION_PROJECTION_JS = """
(async function() {
  try {
    var r = await fetch('/backend-api/conversation/' + __D.conv_id + '?offset=0&limit=' + __D.limit, {
      headers: {'Authorization': 'Bearer ' + __D.token}
    });
    if (!r.ok) return JSON.stringify({__status: r.status});
    var conv = await r.json();
    var mapping = conv.mapping || {};
    var projected = {};
    for (var key in mapping) {
      var node = mapping[key];
      var msg = node.message || {};
      var author = msg.author || {};
      var content = msg.content || {};
      var parts = content.parts || [];
      // Join non-empty string parts for text nodes; drop for non-text.
      var text = '';
      if (content.content_type === 'text') {
        var textParts = [];
        for (var i = 0; i < parts.length; i++) {
          if (typeof parts[i] === 'string' && parts[i].trim()) {
            textParts.push(parts[i]);
          }
        }
        text = textParts.join('\\n');
      }
      projected[key] = {
        id: msg.id || key,
        parent: node.parent || null,
        children: node.children || [],
        role: author.role || 'unknown',
        create_time: msg.create_time || 0,
        end_turn: !!msg.end_turn,
        content_type: content.content_type || 'unknown',
        text: text
      };
    }
    return JSON.stringify({
      nodes: projected,
      current_node: conv.current_node || null
    });
  } catch(e) {
    return JSON.stringify({__error: String(e)});
  }
})()
""".strip()


# Schema documentation (for the fixture test + future maintainers).
PROJECTED_SCHEMA_FIELDS = {
    "id": "str — the message id (equals the mapping key in observed data)",
    "parent": "str | None — parent node id (for upward traversal)",
    "children": "list[str] — child node ids (for downward traversal)",
    "role": "str — user | assistant | tool | system | unknown",
    "create_time": "float — backend-assigned creation timestamp",
    "end_turn": "bool — terminal flag on assistant nodes",
    "content_type": "str — text | reasoning_recap | tool_use | tool_result | multimodal_text | unknown",
    "text": "str — joined non-empty text parts (text nodes only; empty for non-text)",
}

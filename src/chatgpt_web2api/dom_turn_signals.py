"""Current and legacy ChatGPT DOM turn signals.

On 2026-11-20 the live UI rendered one aria-label=Copy per completed
assistant turn and one aria-label=Edit message per user turn. Legacy
message role/search-unit attributes remain as compatibility fallbacks.

Counts are observations, not proof of remote acceptance or turn identity.
The caller still uses the anchored backend end_turn reconciliation.
"""

COPY_BUTTON_SELECTOR = 'button[aria-label="Copy"]'
EDIT_MESSAGE_BUTTON_SELECTOR = 'button[aria-label="Edit message"]'
LEGACY_ASSISTANT_SELECTOR = (
    '[data-message-author-role="assistant"], '
    '[data-content-search-unit-key$=":assistant"], '
    '[data-chatgpt-search-unit-key$=":assistant"]'
)
LEGACY_USER_SELECTOR = (
    '[data-message-author-role="user"], '
    '[data-content-search-unit-key$=":user"], '
    '[data-chatgpt-search-unit-key$=":user"]'
)

# Avoid double counting transitional pages that expose both representations.
ASSISTANT_TURN_COUNT_JS = (
    "Math.max("
    f"document.querySelectorAll('{COPY_BUTTON_SELECTOR}').length, "
    f"document.querySelectorAll('{LEGACY_ASSISTANT_SELECTOR}').length"
    ")"
)
USER_TURN_COUNT_JS = (
    "Math.max("
    f"document.querySelectorAll('{EDIT_MESSAGE_BUTTON_SELECTOR}').length, "
    f"document.querySelectorAll('{LEGACY_USER_SELECTOR}').length"
    ")"
)

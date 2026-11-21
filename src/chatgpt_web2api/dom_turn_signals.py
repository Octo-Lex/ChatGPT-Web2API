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


# R2: Math.max is ONLY a legacy numeric presentation. It is not suitable
# for observing new turns in a mixed-layout page: max(4 legacy, 0 Copy) and
# max(4 legacy, 1 Copy) are both 4. Sample the four representations together,
# then compare each signal to its *own* pre-send baseline.
TURN_SIGNAL_COUNTS_JS = (
    "(function(){return JSON.stringify({"
    f"copy:document.querySelectorAll('{COPY_BUTTON_SELECTOR}').length,"
    f"legacy_assistant:document.querySelectorAll('{LEGACY_ASSISTANT_SELECTOR}').length,"
    f"edit:document.querySelectorAll('{EDIT_MESSAGE_BUTTON_SELECTOR}').length,"
    f"legacy_user:document.querySelectorAll('{LEGACY_USER_SELECTOR}').length"
    "});})()"
)


def parse_turn_signal_counts(raw: str) -> dict[str, int]:
    """Reject partial/invalid DOM snapshots rather than assuming zero.

    The snapshot is an observation of four independent signals; a missing or
    malformed counter must NEVER manufacture a new-turn delta.
    """
    import json

    value = json.loads(raw)
    keys = ("copy", "legacy_assistant", "edit", "legacy_user")
    if not isinstance(value, dict) or any(
        type(value.get(key)) is not int or value[key] < 0 for key in keys
    ):
        raise ValueError("Invalid turn signal count snapshot")
    return {key: value[key] for key in keys}


def turn_signal_increased(
    current: dict[str, int], baseline: dict[str, int], role: str,
) -> bool:
    """Compare signals independently; no Math.max masking across layouts."""
    if role == "assistant":
        keys = ("copy", "legacy_assistant")
    elif role == "user":
        keys = ("edit", "legacy_user")
    else:
        raise ValueError(f"Unsupported turn-signal role: {role}")
    return any(current[key] > baseline[key] for key in keys)

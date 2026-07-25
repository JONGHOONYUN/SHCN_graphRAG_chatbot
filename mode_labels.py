"""Single source of truth for user-facing chatbot mode display labels.

Internal mode identifiers ("graphRAG" / "textRAG") are NOT renamed — they
remain the `chatbot_mode` session-state value, the `messages_by_mode` dict
key, and (in text_rag.py) the Neo4j chat-history namespace suffix, so
existing sessions and history stay compatible. Only the LABEL shown to the
user in the UI is remapped here (see
CLAUDE_CODE_TEXTRAG_UI_LABEL_TO_VECTORRAG.md): the internal "textRAG" mode
displays to users as "vectorRAG".

Kept in its own side-effect-free module (no streamlit import) so it is
unit-testable without triggering bot.py's page config / password auth code,
which run at import time.
"""

MODE_DISPLAY_LABELS = {
    "graphRAG": "graphRAG",
    "textRAG": "vectorRAG",
}


def mode_display_label(mode: str) -> str:
    """User-facing label for an internal mode key.

    Unknown keys pass through unchanged rather than raising, so a stale or
    unexpected mode value degrades gracefully instead of crashing the page."""
    return MODE_DISPLAY_LABELS.get(mode, mode)

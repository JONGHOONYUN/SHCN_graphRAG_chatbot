"""Streamlit-adjacent helpers.

`get_session_id()` is used by both `agent.py` and `text_rag.py` to namespace
the Neo4j chat history per Streamlit browser session. When the module is
imported in a test or CLI context there is no `ScriptRunContext`, so the
underlying accessor returns None; a naive `.session_id` lookup then crashes.

The safe accessor returns a stable-per-process UUID fallback so unit tests
and REPL usage can exercise agent code without a live Streamlit runtime.
The fallback is *not* considered a real session — it is only for the
tooling / test path, where cross-session isolation is not required.
"""

from __future__ import annotations

import uuid

import streamlit as st
from streamlit.runtime.scriptrunner.script_runner import get_script_run_ctx


# Cache the fallback across calls so a single test process always sees one
# stable session_id, avoiding a fresh Neo4j history namespace per call.
_FALLBACK_SESSION_ID = f"fallback-{uuid.uuid4().hex[:12]}"


def write_message(role, content, save=True):
    """
    This is a helper function that saves a message to the
     session state and then writes a message to the UI
    """
    # Append to session state
    if save:
        st.session_state.messages.append({"role": role, "content": content})

    # Write to UI
    with st.chat_message(role):
        st.markdown(content)


def get_session_id() -> str:
    """Return the Streamlit session id, or a stable-per-process fallback.

    Never raises: `get_script_run_ctx()` can return None in tests / CLI runs
    where no Streamlit runtime is attached. The fallback keeps Neo4j chat
    history writes and reads working in those environments without giving
    them a real user-facing session identity."""
    try:
        ctx = get_script_run_ctx()
    except Exception:
        ctx = None
    if ctx is None:
        return _FALLBACK_SESSION_ID
    sid = getattr(ctx, "session_id", None)
    return sid or _FALLBACK_SESSION_ID

"""Project-wide exception taxonomy.

A tight, purposeful set of exception classes so callers can reason about
recovery policy without inspecting string messages. Every class carries a
`correlation_id` so a user-facing message ("something failed — ref: xxxx")
can be traced to the exact log line that recorded the underlying cause,
without ever exposing the raw exception text.

Categories:
  * ConfigurationError    — invalid / missing configuration (secrets, model,
                            dimension). NEVER a legitimate reason to fall
                            back to a legacy retriever; the operator must
                            fix config.
  * TransientProviderError — a temporarily broken external dependency
                            (Gemini 5xx, Neo4j Aura connection drop,
                            rate-limit exhaustion after retries). The
                            legitimate fallback trigger.
  * UnsafeQueryError       — an LLM-produced query was blocked by the
                            application-layer validator. Never retried on
                            the legacy path; the user is told to rephrase.
  * RetrievalError         — a retriever failed in an unexpected way that
                            is neither config nor transient. Logged with
                            full context; user sees a safe message.
  * ModelResponseError     — the LLM produced an empty stream / malformed
                            output despite receiving a well-formed prompt.
                            May be retried by the caller policy.

Anything else (`Exception`) is treated as a coding bug: logged, converted
to a safe user message, but *not* used as a signal to fall back to legacy
paths (that would hide the bug forever).
"""

from __future__ import annotations

import uuid
from typing import Optional


class ChatbotError(Exception):
    """Base class for all project-defined errors. Carries a correlation_id."""

    def __init__(self, message: str, *,
                 correlation_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id or uuid.uuid4().hex[:8]


class ConfigurationError(ChatbotError):
    """Missing or invalid configuration — a human must fix it. Never a valid
    fallback trigger."""


class TransientProviderError(ChatbotError):
    """External provider is temporarily broken (Gemini 5xx, Neo4j Aura
    connection drop, network timeout). This is the only class that legitimately
    triggers the ReAct fallback path."""


class UnsafeQueryError(ChatbotError):
    """The LLM produced a query the validator blocked. The user is told to
    rephrase; the legacy path is NOT retried (it would just re-generate the
    same unsafe query)."""


class RetrievalError(ChatbotError):
    """A retriever raised something that is neither config nor transient — for
    example a schema mismatch. Logged with full context; user sees a safe
    message. Not a fallback trigger."""


class ModelResponseError(ChatbotError):
    """The LLM returned an empty stream or malformed output despite a well-
    formed prompt. Caller decides whether to retry."""


# ── Fallback-policy predicates ───────────────────────────────────────────────
# ReAct fallback is legitimate ONLY for TransientProviderError. Every other
# case is either a coding bug or an authorization / config error that must
# not be papered over by retrying with a different code path.
def is_fallback_eligible(exc: BaseException) -> bool:
    """Return True iff `exc` warrants running the legacy ReAct fallback.

    Explicitly rejects ConfigurationError, UnsafeQueryError, RetrievalError,
    ModelResponseError, plain TypeError/AttributeError (internal defects),
    and any subclass of KeyboardInterrupt / SystemExit."""
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return False
    if isinstance(exc, (ConfigurationError, UnsafeQueryError,
                        RetrievalError, ModelResponseError)):
        return False
    if isinstance(exc, TransientProviderError):
        return True
    # Everything else: treat as an internal defect, do NOT hide with fallback.
    return False

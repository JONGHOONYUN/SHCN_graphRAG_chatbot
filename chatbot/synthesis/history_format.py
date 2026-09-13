"""Conversation-history serialization for the final synthesis prompt.

책임: user/assistant 메시지만, message/total 문자 예산 안에서 직렬화.
tool trace·raw payload·내부 오류 텍스트는 marker 필터로 제외.
허용 의존성: 표준 라이브러리만. 외부 부작용: 없음.
기존 facade: tools/synthesis.py.

Moved verbatim from tools/synthesis.py (modularization work order Phase 3.2).
"""

from __future__ import annotations

from typing import Any

# Conversation-history bounds (work order §1): last N messages, per-message and
# total character budgets, so unlimited history never reaches Gemini.
HISTORY_MAX_MESSAGES = 8
HISTORY_MAX_MESSAGE_CHARS = 400
HISTORY_MAX_TOTAL_CHARS = 2400

# Content markers that identify tool traces / raw payloads / internal errors —
# such messages are never serialized into the history block.
_HISTORY_EXCLUDE_MARKERS = (
    "Observation:", "Action Input:", "Traceback", "MUST_NOT_ADD",
    "schema_hint", "CypherSyntaxError", "ClientError",
)


# ── Conversation-history serialization (work order §1) ────────────────────────
HISTORY_RULES = """\
# Conversation history rules (STRICT)
- Conversation history may resolve pronouns or ellipsis only ("그 인물", "그 작품",
  "the person mentioned earlier", ...).
- It is not evidence. Corpus and external facts must come only from the current
  evidence blocks; never repeat a prior assistant statement as a fact unless the
  current evidence also supports it.
- If several previous entities could plausibly match the reference, ask ONE
  concise clarification question instead of guessing.
- If the referent cannot be resolved from the history, say so and ask; do not
  infer a missing entity from pretraining.
"""


def _history_role(item: Any) -> str:
    """Map a message to 'user'/'assistant'; empty string means 'exclude'."""
    if isinstance(item, dict):
        role = (item.get("role") or "").lower()
    else:
        role = (getattr(item, "type", "") or "").lower()
    if role in ("user", "human"):
        return "user"
    if role in ("assistant", "ai"):
        return "assistant"
    return ""


def _history_content(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("content") or "")
    return str(getattr(item, "content", "") or "")


def serialize_chat_history(
    messages: Any,
    max_messages: int = HISTORY_MAX_MESSAGES,
    max_message_chars: int = HISTORY_MAX_MESSAGE_CHARS,
    max_total_chars: int = HISTORY_MAX_TOTAL_CHARS,
) -> str:
    """Bounded, user/assistant-only serialization of prior conversation.

    Accepts LangChain messages (with .type/.content) or plain dicts
    ({"role","content"}). Tool traces, raw authority payloads, and internal
    error text are excluded via marker filtering; roles other than
    user/assistant are dropped. Most recent messages win the budget."""
    lines: list = []
    for item in messages or []:
        role = _history_role(item)
        if not role:
            continue
        content = _history_content(item).strip()
        if not content:
            continue
        if any(marker in content for marker in _HISTORY_EXCLUDE_MARKERS):
            continue
        if len(content) > max_message_chars:
            content = content[:max_message_chars] + "…"
        lines.append(f"{role}: {content}")

    lines = lines[-max_messages:]
    # Enforce the total budget by dropping the OLDEST lines first.
    while lines and sum(len(l) + 1 for l in lines) > max_total_chars:
        lines.pop(0)
    return "\n".join(lines)

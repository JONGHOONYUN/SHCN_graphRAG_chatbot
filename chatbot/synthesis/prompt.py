"""Final-synthesis prompt assembly (prompt text only — no LLM invocation).

책임: 이번 턴의 응답 언어 지시문과, 근거 블록·이력을 감싸는 최종 합성
ChatPromptTemplate 구성. 여기서 LLM을 호출하지 않는다 — 체인 결합(`prompt |
llm | StrOutputParser()`)은 composition root(agent.py)의 몫이다.

허용 의존성: langchain_core.prompts(프롬프트 자료구조),
chatbot.synthesis.{source_policy, history_format}. streamlit / Neo4j client /
LLM client는 import하지 않는다.
외부 부작용: 없음.
기존 facade: agent.py (`LANGUAGE_LABEL`, `_build_language_directive`,
`synthesis_prompt`).

Moved verbatim from agent.py (modularization work order Phase 8.2).
언어 지시문은 정상 합성 경로와 레거시 ReAct 경로가 공유하는 유일한 프롬프트
조각이다 (§8.3이 허용하는 prompt 수준 공유).
"""

from __future__ import annotations

from langchain_core.prompts import ChatPromptTemplate

from chatbot.synthesis.history_format import HISTORY_RULES
from chatbot.synthesis.source_policy import SYNTHESIS_SYSTEM_RULES

LANGUAGE_LABEL = {
    "ko": "Korean (한국어)",
    "en": "English",
    "zh": "Chinese (中文)",
}


def _build_language_directive(user_language: str) -> str:
    label = LANGUAGE_LABEL.get(user_language, LANGUAGE_LABEL["ko"])
    return (
        "# Response Language For This Turn\n"
        f"The response for THIS turn MUST be written in: {label}.\n"
        f"You MUST write the entire 'Final Answer:' in {label}, regardless of the "
        "language of tool outputs, source documents, or earlier turns in the chat history.\n"
        "Exception: source text fields (textChi, textKor, textEng, descEng) must still "
        "be quoted verbatim in their original characters. Only your own commentary, "
        f"explanations, section labels, and tool-routing notes follow the {label} rule.\n"
    )


# ──────────────────────────────────────────────
# Final synthesis prompt (single LLM call over structured evidence)
#
# graphRAG mode runs: gather structured evidence (graph + vector + optional
# authority) → ONE synthesis LLM call. The retrievers never write the final
# prose; this prompt does. The legacy ReAct agent is retained only as a
# fallback and does not share this prompt.
# ──────────────────────────────────────────────
def build_synthesis_prompt() -> ChatPromptTemplate:
    """Assemble the final-synthesis ChatPromptTemplate.

    The system message carries the locked-language directive, the
    source/conflict policy, and the conversation-history rules; the human
    message frames the question, the bounded history, and the rendered
    evidence blocks, and states that Sources are system-owned."""
    return ChatPromptTemplate.from_messages(
        [
            ("system",
             "{language_directive}\n\n" + SYNTHESIS_SYSTEM_RULES + "\n\n" + HISTORY_RULES),
            (
                "human",
                "Prior conversation (for pronoun/ellipsis resolution only — NOT evidence):\n"
                "{chat_history}\n\n"
                "Question (original wording, do not translate before searching): {question}\n\n"
                "Below are the structured evidence blocks retrieved for this question. "
                "Use ONLY these blocks as factual sources:\n"
                "{evidence_blocks}\n\n"
                "Write ONLY the answer body, following every rule above. Do NOT "
                "write a Sources/References/출처/来源 section in any language — "
                "the system appends the finalized Sources deterministically after "
                "your text, and anything you write there will be discarded. "
                "When you mention entities or quote node ids in the body, preserve "
                "the markdown links provided in the evidence (e.g. "
                "[P553](https://poetrytalks.org/P553)) intact, and keep distinct "
                "node ids separate even when they share an external identifier.",
            ),
        ]
    )

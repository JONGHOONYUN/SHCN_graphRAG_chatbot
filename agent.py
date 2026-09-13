"""graphRAG 진입점 — compatibility facade, composition root, 최상위 오류 정책.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/synthesis/prompt.py               언어 지시문 · 최종 합성 프롬프트
    chatbot/application/graphrag_pipeline.py  정상 graphRAG 파이프라인 본체
    chatbot/legacy/react_prompt.py            레거시 ReAct 프롬프트 텍스트
    chatbot/legacy/react_agent.py             레거시 tool 구성 · executor · 폴백

이 모듈이 직접 소유하는 것은 세 가지다.

1. **Composition root** — 이 프로세스의 LLM/Neo4j 클라이언트로 합성 체인,
   ReAct tool/executor, `::graphRAG` 이력 핸들을 구성해 위 구현에 주입한다.
2. **Presentation 경계** — Streamlit session state에서 이번 턴의
   question/response 언어를 읽는다 (application/legacy 계층은 읽지 않는다).
3. **최상위 오류 정책**(§8.4) — transient provider 오류에서만 레거시 ReAct
   폴백을 호출하고, configuration/unsafe/programming 오류에서는 폴백하지
   않는다. 정상 파이프라인은 레거시를 import조차 하지 않으며, 레거시 호출은
   오직 이 정책 분기에서만 일어난다.
"""

import streamlit as st
from llm import llm
from graph import graph
from langchain_core.output_parsers import StrOutputParser
from langchain_neo4j import Neo4jChatMessageHistory
from utils import get_session_id

from chatbot.application import graphrag_pipeline as _pipeline
from chatbot.legacy import react_agent as _react
from chatbot.legacy.react_prompt import (  # noqa: F401  (public compatibility)
    ITERATION_LIMIT_FALLBACK,
    ITERATION_LIMIT_PLACEHOLDER,
)
from chatbot.synthesis.prompt import (  # noqa: F401  (public compatibility)
    LANGUAGE_LABEL,
    _build_language_directive,
    build_synthesis_prompt,
)

# 기존 public import 경로 보존 (agent 모듈 namespace에서 계속 접근 가능).
from tools.vector import get_poetry_plot
from tools.cypher import cypher_qa_safe
from tools.external_authority import external_authority_lookup
from tools.answer_renderer import (  # noqa: F401
    assemble_final_answer,
    derive_referenced_node_ids,
    strip_model_sources,
)
from tools.evidence import collect_node_references  # noqa: F401
from tools.orchestrator import gather_graphrag_evidence  # noqa: F401
from tools.synthesis import (  # noqa: F401
    HISTORY_RULES,
    SYNTHESIS_SYSTEM_RULES,
    both_retrievals_failed,
    build_citations,
    format_evidence_for_prompt,
    retrieval_failure_message,
    serialize_chat_history,
)

chat_prompt = _react.build_chat_prompt()

poetry_chat = chat_prompt | llm | StrOutputParser()


def general_chat(input_text: str) -> str:
    """General Chat tool (레거시 ReAct 전용). 세션의 응답 언어를 읽어 넘기고,
    실제 동적 prompt/chain 구성은 chatbot/legacy/react_agent.py가 소유한다."""
    user_language = st.session_state.get("effective_language", "ko")
    return _react.run_general_chat(input_text, llm=llm, user_language=user_language)


# 1. tools 정의 — 구현 콜러블은 여기서 주입하고, 라우팅 설명은 레거시 계층 소유.
tools = _react.build_tools(
    general_chat=general_chat,
    get_poetry_plot=get_poetry_plot,
    cypher_qa_safe=cypher_qa_safe,
    external_authority_lookup=external_authority_lookup,
)


def get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)


# 2~4. agent_prompt / agent / executor / 이력 래퍼 구성
agent_prompt = _react.build_agent_prompt()
agent = _react.build_agent(llm=llm, tools=tools)
agent_executor = _react.build_agent_executor(agent=agent, tools=tools)

chat_agent = _react.build_chat_agent(agent_executor, get_memory)


# ──────────────────────────────────────────────
# Final synthesis chain (single LLM call over structured evidence)
# 프롬프트 텍스트는 chatbot/synthesis/prompt.py 소유; 여기서는 이 프로세스의
# LLM과 결합만 한다.
# ──────────────────────────────────────────────
synthesis_prompt = build_synthesis_prompt()

synthesis_chain = synthesis_prompt | llm | StrOutputParser()


def _graphrag_history(session_id: str):
    """graphRAG 전용 Neo4j 이력 핸들 (textRAG 이력과 ::graphRAG suffix로 분리)."""
    return Neo4jChatMessageHistory(session_id=f"{session_id}::graphRAG", graph=graph)


def _load_bounded_history(session_id: str) -> str:
    """직전 대화를 bounded 직렬화. 이력 조회 실패는 답변을 막지 않는다(빈 이력)."""
    return _pipeline.load_bounded_history(_graphrag_history, session_id)


def synthesize_answer(user_input: str, response_language: str,
                      question_language: str = None) -> str:
    """graphRAG 최종 합성 진입점. 파이프라인 본체는
    `chatbot.application.graphrag_pipeline.synthesize_answer`가 소유하며,
    여기서는 합성 체인과 `::graphRAG` 이력 핸들, session_id를 주입한다.

    work order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §3:
    `question_language`(질문/검색 언어)와 `response_language`(최종 출력 언어)를
    분리한다. `question_language`가 생략되면 `response_language`와 같다고
    가정한다 — 기존 호출자(단일 언어 인자만 넘기는 코드)의 동작을 그대로
    보존하는 하위 호환 기본값이다."""
    question_language = question_language or response_language
    session_id = get_session_id()
    return _pipeline.synthesize_answer(
        user_input, response_language, question_language,
        synthesis_chain=synthesis_chain,
        history_factory=_graphrag_history,
        session_id=session_id,
    )


import logging as _logging  # noqa: E402
import uuid as _uuid  # noqa: E402

from errors import (  # noqa: E402
    ConfigurationError,
    RetrievalError,
    UnsafeQueryError,
    is_fallback_eligible,
)
from tools.cypher_safety import UnsafeCypherError  # noqa: E402

_logger = _logging.getLogger(__name__)


def generate_response(user_input):
    """graphRAG 응답 생성 엔트리포인트 (bot.py에서 호출).

    Fallback policy (Phase 3):
      * `synthesize_answer` succeeds with non-empty output → return that.
      * `synthesize_answer` returns empty → treat as `no_results`; NO fallback.
      * `TransientProviderError` (Gemini 5xx, Neo4j drop, etc.) → ReAct fallback.
      * `UnsafeCypherError` / `UnsafeQueryError` → user is told to rephrase;
        NO fallback (the ReAct path would just regenerate the same query).
      * `ConfigurationError` / `RetrievalError` → localized safe message;
        NO fallback (an operator must fix the underlying issue).
      * Bare `ValueError` matching Gemini's empty-stream signature → localized
        safe message; NO fallback (the ReAct path would repeat the same
        failure and burn tokens).
      * Anything else → log with correlation id, localized safe message,
        NO fallback (hiding a coding bug behind ReAct forever is exactly
        what the work order forbids).
    """
    # response_language: 최종 출력/오류 문구 언어. question_language: 검색·
    # index 선택 언어. bot.py(Phase 2)가 두 키를 모두 세팅하지만, 구버전
    # 세션이나 다른 호출자를 위해 `effective_language`(response_language의
    # 하위 호환 alias)로 폴백한다.
    response_language = (st.session_state.get("response_language")
                        or st.session_state.get("effective_language", "ko"))
    question_language = st.session_state.get("question_language") or response_language

    try:
        output = synthesize_answer(user_input, response_language, question_language)
        if output and output.strip():
            return output
        # Empty output is treated as no_results — safe message, not fallback.
        return ITERATION_LIMIT_FALLBACK.get(
            response_language, ITERATION_LIMIT_FALLBACK["ko"])
    except (UnsafeCypherError, UnsafeQueryError) as exc:
        _logger.warning(
            "graphRAG blocked unsafe query [%s]",
            getattr(exc, "correlation_id", "?"),
        )
        return ITERATION_LIMIT_FALLBACK.get(
            response_language, ITERATION_LIMIT_FALLBACK["ko"])
    except (ConfigurationError, RetrievalError) as exc:
        _logger.error(
            "graphRAG non-fallback error [%s] type=%s",
            getattr(exc, "correlation_id", "?"), type(exc).__name__,
        )
        return ITERATION_LIMIT_FALLBACK.get(
            response_language, ITERATION_LIMIT_FALLBACK["ko"])
    except ValueError as exc:
        if "No generation chunks were returned" in str(exc):
            _logger.info(
                "graphRAG Gemini empty stream — safe message, no fallback")
            return ITERATION_LIMIT_FALLBACK.get(
                response_language, ITERATION_LIMIT_FALLBACK["ko"])
        # Any other ValueError is unexpected; fall through to policy check.
        return _react_fallback_or_safe(exc, user_input, response_language)
    except Exception as exc:
        return _react_fallback_or_safe(exc, user_input, response_language)


def _react_fallback_or_safe(exc: BaseException, user_input: str,
                            response_language: str) -> str:
    """Policy: only TransientProviderError triggers the ReAct fallback. Any
    other exception is logged with a correlation id and converted to the
    localized safe message so a coding bug is not hidden behind a fallback."""
    correlation_id = getattr(exc, "correlation_id", None) or _uuid.uuid4().hex[:8]
    if is_fallback_eligible(exc):
        _logger.warning(
            "graphRAG transient failure [%s] type=%s — invoking ReAct fallback",
            correlation_id, type(exc).__name__,
        )
        return _generate_response_react(user_input, response_language)
    _logger.error(
        "graphRAG unexpected failure [%s] type=%s — NO fallback (%s)",
        correlation_id, type(exc).__name__, str(exc)[:200],
    )
    return ITERATION_LIMIT_FALLBACK.get(
        response_language, ITERATION_LIMIT_FALLBACK["ko"])


def _generate_response_react(user_input, response_language):
    """레거시 ReAct agent 경로 (폴백). 구현은
    `chatbot.legacy.react_agent.generate_response_react`; 여기서는 이력 래퍼와
    `::graphRAG` session namespace만 주입한다 — textRAG 이력과 완전 분리."""
    return _react.generate_response_react(
        user_input, response_language,
        chat_agent=chat_agent,
        session_id=f"{get_session_id()}::graphRAG",
    )

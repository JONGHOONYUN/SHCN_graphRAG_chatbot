"""textRAG 모드 — Entry.textKor/textChi/textEng 벡터 인덱스에 대한 의미 기반
검색만으로 답하는 단순 RAG 경로의 진입점 (compatibility facade + composition
root). bot.py가 mode=='textRAG'일 때 호출한다.

정확한 범위 (사용자 안내 문구와 일치해야 함):
- Entry 본문에 대한 의미 벡터 검색을 수행한다.
- 그래프 관계 추론·구조 질의(작자·관직·시대·비평 관계 등)는 수행하지 않는다.
- 단, Entry–Work 포함 관계([:HAS_PART])는 출처·인용 메타데이터(시화집명 등)를
  붙이기 위해서만 조회한다. "그래프를 전혀 사용하지 않는다"는 표현은 부정확하므로
  사용하지 말 것.
- ReAct agent를 사용하지 않고 create_retrieval_chain을 직접 호출한다.
- 질문 언어(question_language)에 맞는 in-language 인덱스를 선택한다.
- 대화 이력은 session_id에 `::textRAG` suffix를 붙여 graphRAG와 완전 분리한다.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/retrieval/vector_query.py          경량 retrieval_query 투영
    chatbot/retrieval/vector_retriever.py      Neo4jVector 생성 · 언어별 캐시
    chatbot/application/vectorrag_pipeline.py  prompt · chain · 이력 · 최종 조립

이 모듈이 직접 소유하는 것은 **composition root** 역할뿐이다: 이 프로세스의
llm/embeddings/graph를 주입하고, Streamlit session state에서 이번 턴의 언어를
읽고, 대화 이력 namespace(`::textRAG`)를 붙인다 — 이력은 session_id에
`::textRAG` suffix를 붙여 graphRAG와 완전 분리한다.
"""

import streamlit as st

from llm import llm, embeddings
from graph import graph

from langchain_neo4j import Neo4jChatMessageHistory

from utils import get_session_id
from tools.evidence import POETRYTALKS_BASE_URL  # noqa: F401  (single source)

# ──────────────────────────────────────────────
# 언어별 인덱스 라우팅
# `rag_config` 모듈이 유일한 소유자. 텍스트RAG와 그래프RAG(tools/vector.py) 모두
# 같은 dict를 참조하므로 라벨/인덱스명 변경 시 rag_config 한 곳만 편집한다.
# ──────────────────────────────────────────────
from rag_config import index_config_for  # noqa: F401

from chatbot.application import vectorrag_pipeline as _pipeline
from chatbot.observability import events as _obs
from chatbot.observability import telemetry as _telemetry
from chatbot.observability.callbacks import with_llm_purpose
from chatbot.application.vectorrag_pipeline import FALLBACK_HINT  # noqa: F401
from chatbot.retrieval import vector_retriever as _vector_retriever
from chatbot.retrieval.vector_query import _build_light_retrieval_query  # noqa: F401
from chatbot.retrieval.vector_retriever import TOP_K  # noqa: F401
from chatbot.retrieval.vector_retriever import (  # noqa: F401
    _textrag_retrievers as _retrievers,   # legacy name for the same cache
)


# Same model object; only the observability purpose label is added.
_answer_llm = with_llm_purpose(llm, _obs.LLM_TEXT_RAG_ANSWER)


def _get_text_retriever_for_lang(lang: str):
    """`lang` (= question_language)에 대응하는 캐시된 textRAG retriever.
    캐시 key와 top-k는 chatbot/retrieval/vector_retriever.py 소유."""
    return _vector_retriever.get_textrag_retriever(
        lang, embeddings=embeddings, graph=graph)


# ──────────────────────────────────────────────
# 대화 이력 (graphRAG와 분리 — session_id suffix)
# ──────────────────────────────────────────────
def _get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)


def generate_text_rag_response(user_input: str,
                               response_language: str = None,
                               question_language: str = None) -> str:
    """textRAG 모드의 사용자 응답 생성 엔트리포인트.
    bot.py에서 mode=='textRAG'일 때 호출된다.

    work order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md
    §4 Phase 4: `question_language`(검색 index 선택)와 `response_language`
    (최종 출력/인용 순서/Sources 언어)를 분리한다. 둘 다 생략하면 세션 상태
    (`question_language`/`response_language`, 없으면 `effective_language`
    하위 호환 alias)에서 읽는다 — 기존 `bot.py`가 `generate_text_rag_response(
    message)`처럼 인자 없이 호출하는 방식을 그대로 지원하는 하위 호환 기본값.
    `question_language`가 생략되고 세션에도 없으면 `response_language`와
    같다고 가정한다(분리 이전과 동일한 단일 언어 동작).

    `user_input`은 이미 언어 제어 문구가 제거된 question_text여야 한다.

    파이프라인 본체(prompt·chain·이력·최종 조립)는
    `chatbot.application.vectorrag_pipeline.run_vectorrag_pipeline`가 소유한다.
    """
    if response_language is None:
        response_language = (st.session_state.get("response_language")
                             or st.session_state.get("effective_language", "ko"))
    if question_language is None:
        question_language = st.session_state.get("question_language") or response_language

    # 관측: bot.py가 연 요청 문맥이 있으면 그대로 쓰고(같은 request_id), 없으면
    # 여기서 root 문맥을 연다. 반환값·예외는 그대로 통과한다.
    with _telemetry.request_scope(mode=_obs.MODE_VECTORRAG,
                                  route=_obs.ROUTE_VECTORRAG,
                                  question_language=question_language,
                                  response_language=response_language):
        base_retriever = _get_text_retriever_for_lang(question_language)

        # session_id에 ::textRAG suffix로 graphRAG와 완전 분리
        session_id = f"{get_session_id()}::textRAG"

        return _pipeline.run_vectorrag_pipeline(
            user_input, response_language,
            base_retriever=base_retriever,
            llm=_answer_llm,
            memory_factory=_get_memory,
            session_id=session_id,
        )

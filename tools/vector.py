"""Compatibility facade + composition root — Entry vector retrieval.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/retrieval/vector_query.py      retrieval_query 투영 (정적 텍스트)
    chatbot/retrieval/vector_retriever.py  Neo4jVector 생성 · 언어별 캐시 · Evidence
    chatbot/legacy/react_vector_tool.py    레거시 ReAct tool prompt + prose 생성

이 모듈이 직접 소유하는 것은 **composition root** 역할뿐이다: 이 프로세스의
embeddings/graph/llm 클라이언트를 주입하고, Streamlit session state에서
이번 턴의 언어를 읽어 넘긴다 (retrieval/legacy 계층은 session state를 읽지
않는다).

경계(§5.2/§5.4): `retrieve_sihwa_evidence()`는 구조화 Evidence만 반환하고 최종
prose를 만들지 않는다. `get_poetry_plot()`은 레거시 ReAct tool 전용이며 정상
graphRAG 경로는 이를 호출하지 않는다.
"""

from typing import Optional

import streamlit as st
from llm import llm, embeddings
from graph import graph

# Single source of truth for the Poetry Talks domain — see tools/evidence.py.
# Re-exported here because the retrieval_query projection and the legacy
# prompt both derive their URLs from this one constant.
from tools.evidence import POETRYTALKS_BASE_URL  # noqa: F401
from tools.evidence import Evidence, docs_to_evidence  # noqa: F401

# INDEX_BY_LANG is owned by `rag_config` — re-exported for backward
# compatibility so downstream imports keep working. Any change to the index
# map must be made in `rag_config.INDEX_BY_LANG` only.
from rag_config import INDEX_BY_LANG as INDEX_BY_LANG  # re-export
from rag_config import index_config_for  # noqa: F401

from chatbot.legacy import react_vector_tool as _react_vector_tool
from chatbot.legacy.react_vector_tool import instructions  # noqa: F401
from chatbot.retrieval import vector_retriever as _vector_retriever
from chatbot.retrieval.vector_query import _build_retrieval_query  # noqa: F401
from chatbot.retrieval.vector_retriever import (  # noqa: F401
    _graphrag_retrievers as _retrievers,   # legacy name for the same cache
)


def _get_retriever_for_lang(lang: str):
    """이 프로세스의 embeddings/graph를 주입해 언어별 rich retriever를 얻는다.
    캐시와 index 선택 자체는 chatbot/retrieval/vector_retriever.py 소유."""
    return _vector_retriever.get_graphrag_retriever(
        lang, embeddings=embeddings, graph=graph)


def _build_prompt():
    """레거시 ReAct tool용 prompt. 응답 언어(response_language)를 세션에서 읽어
    넘기며, 프롬프트 자체는 chatbot/legacy/react_vector_tool.py가 소유한다."""
    user_language = (st.session_state.get("response_language")
                    or st.session_state.get("effective_language", "ko"))
    return _react_vector_tool.build_prompt(user_language)


def get_poetry_plot(input):
    # 매 호출 시 세션의 question_language(검색 index 언어)를 읽어 그에 맞는
    # in-language 인덱스로 라우팅한다 — 응답 언어(response_language)와는
    # 분리된 값이다 (work order
    # CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §3 Phase 3
    # item 7). bot.py가 매 턴 갱신하는 키이며, 구세션 호환을 위해
    # effective_language로 폴백한 뒤 최종적으로 ko를 기본값으로 쓴다.
    # NOTE: 이 함수는 자체적으로 최종 답변 prose를 생성한다. graphRAG 파이프라인은
    # 대신 retrieve_sihwa_evidence()를 사용해 구조화된 근거만 수집하고, 최종 합성은
    # agent.synthesize_answer()에서 단 한 번 수행한다. get_poetry_plot는 하위 호환
    # (기존 ReAct tool)용으로만 남겨둔다.
    question_language = (st.session_state.get("question_language")
                        or st.session_state.get("effective_language", "ko"))
    response_language = (st.session_state.get("response_language")
                        or st.session_state.get("effective_language", "ko"))
    retriever = _get_retriever_for_lang(question_language)
    return _react_vector_tool.get_poetry_plot(
        input, retriever=retriever, llm=llm, response_language=response_language)


def retrieve_sihwa_evidence(query: str, language: Optional[str] = None) -> Evidence:
    """Retrieve structured vector evidence for the graphRAG pipeline.

    Returns an Evidence(kind='vector') with documents, Person entities (with
    authority IDs where present), and provenance. Does NOT call
    create_stuff_documents_chain and does NOT produce a final answer.

    `language` here is the QUERY/INDEX language — i.e. `question_language`
    in the work order
    CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §3 split, NOT
    the final response language. The orchestrator's default vector retriever
    always passes it explicitly (its `question_language`); the session-state
    fallback below only applies to legacy/direct callers that omit it, and
    prefers `question_language` over the response-language alias
    `effective_language` for that reason."""
    user_language = (language or st.session_state.get("question_language")
                    or st.session_state.get("effective_language", "ko"))
    retriever = _get_retriever_for_lang(user_language)
    return _vector_retriever.retrieve_sihwa_evidence(query, retriever=retriever)

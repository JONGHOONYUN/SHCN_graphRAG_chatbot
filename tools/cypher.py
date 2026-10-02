"""Compatibility facade + composition root — graph retrieval.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/retrieval/graph_prompt.py  CYPHER_GENERATION_TEMPLATE · 재시도 힌트
    chatbot/retrieval/graph_query.py   생성 Cypher 실행 · 결정론적 ranking 질의
    chatbot/legacy/graph_qa.py         레거시 prose graph QA (cypher_qa_safe)
    chatbot/retrieval/graph_rows.py    graph row → Evidence (tools/evidence 경유)

이 모듈이 유일하게 직접 소유하는 것은 **composition root** 역할이다: LLM과
Neo4j graph를 안전 wrapper로 감싸 체인을 만들고, 그 체인을 위 구현 함수에
주입한다. 나머지는 기존 import 경로를 보존하는 re-export / 얇은 위임이다.

안전 경계(§4.4): 모든 LLM 생성 Cypher는 `tools.cypher_safety.safe_graph`로
감싼 `_safe_graph`를 통해서만 실행되며, wrapper는 어느 신규 모듈에도 복제되지
않았다. `allow_dangerous_requests=True`는 LangChain이 요구하는 플래그일 뿐
읽기 전용 보장이 아니다 — 보장은 `_safe_graph`가 제공한다.
"""

from llm import llm
from graph import graph
# 프롬프트 템플릿 클래스
from langchain_core.prompts import PromptTemplate

# Read-only Cypher validator (Phase 1 hardening).
# Wraps the Neo4jGraph passed to GraphCypherQAChain so every LLM-generated
# Cypher is validated BEFORE Neo4j execution. Neo4jChatMessageHistory is NOT
# wrapped — it legitimately writes history nodes and must retain full access.
from tools.cypher_safety import safe_graph, UnsafeCypherError  # noqa: F401

from chatbot.legacy import graph_qa as _legacy_graph_qa
from chatbot.retrieval import graph_query as _graph_query
from chatbot.retrieval.graph_chain import build_graph_cypher_chain
from chatbot.retrieval.graph_prompt import (  # noqa: F401
    CYPHER_GENERATION_TEMPLATE,
    _SHAPE_RETRY_HINT,
    _SYNTAX_RETRY_HINT,
)
from chatbot.retrieval.graph_query import (  # noqa: F401  (re-exported helpers)
    RECOVERABLE_REASON_CODES,
    _extract_intermediate,
    _invalid_query_evidence,
    _status_evidence,
    logger,
)
# graph row → Evidence 변환은 domain/retrieval 계층 소유. 기존 이름 유지.
from tools.evidence import Evidence, graph_rows_to_evidence  # noqa: F401
from tools.graph_intent import (  # noqa: F401
    DEFAULT_RANKING_LIMIT,
    MENTION_COUNT_UNIT,
    ROLE_TYPE_CUES,
    build_role_ranking_query,
    rank_rows_by_mention_count,
)

_safe_graph = safe_graph(graph)

# 프롬프트 객체 생성 — 문자열 템플릿을 LangChain 프롬프트 템플릿 객체로 변환
cypher_prompt = PromptTemplate.from_template(CYPHER_GENERATION_TEMPLATE)

# 두 체인 모두 같은 `llm`을 내부 Cypher 생성·Graph QA 양쪽에 쓴다 (기존과
# 동일). builder는 두 내부 LLM에 관측 목적(cypher_generation / graph_qa)만 다르게
# 붙인다. 자연어 답변을 쓰는 레거시 체인의 Graph QA는 유지한다.
cypher_qa = build_graph_cypher_chain(
    llm, graph=_safe_graph, cypher_prompt=cypher_prompt)

# 구조화된 그래프 근거 수집용 체인.
# return_direct=True로 Graph QA를 생략하고 result에서 raw rows를 받는다.
# intermediate_steps에는 생성 Cypher만 남으므로 추출기는 두 반환 형식을 지원한다.
cypher_qa_structured = build_graph_cypher_chain(
    llm, graph=_safe_graph, cypher_prompt=cypher_prompt,
    return_intermediate_steps=True, return_direct=True)


def cypher_qa_safe(question: str) -> str:
    """레거시 prose graph QA (ReAct tool 전용). 구현은
    `chatbot.legacy.graph_qa.cypher_qa_safe`; 여기서는 이 프로세스의 레거시
    체인을 주입만 한다."""
    return _legacy_graph_qa.cypher_qa_safe(question, chain=cypher_qa)


def retrieve_graph_evidence(question: str,
                            history_text: str | None = None) -> Evidence:
    """정상 graphRAG용 구조화 graph 근거 수집. 구현은
    `chatbot.retrieval.graph_query.retrieve_graph_evidence`; 여기서는
    structured 체인을 주입만 한다."""
    return _graph_query.retrieve_graph_evidence(
        question, history_text, chain=cypher_qa_structured)


def retrieve_role_ranking_evidence(role: str,
                                   limit: int = DEFAULT_RANKING_LIMIT) -> Evidence:
    """결정론적 role ranking 근거. 구현은
    `chatbot.retrieval.graph_query.retrieve_role_ranking_evidence`; 여기서는
    read-only safe graph wrapper를 주입만 한다."""
    return _graph_query.retrieve_role_ranking_evidence(
        role, limit, graph=_safe_graph)

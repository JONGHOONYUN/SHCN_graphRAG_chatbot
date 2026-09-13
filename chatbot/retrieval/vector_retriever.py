"""Entry vector retriever construction, per-language cache, and evidence.

책임: 언어별 Neo4jVector retriever 생성과 캐시, 검색 결과 → Evidence 변환.
두 소비자(graphRAG rich 투영 / textRAG 경량 투영)는 retrieval_query와 top-k가
다르므로 캐시도 분리되어 있다 — 캐시 key는 둘 다 **question_language**(검색
index 언어)이며 응답 언어는 캐시에 굽지 않는다.

허용 의존성: langchain_neo4j(Neo4jVector), rag_config(index 설정 단일 소유자),
chatbot.retrieval.vector_query, tools.evidence(=domain evidence).
embeddings / graph 클라이언트는 인자로 주입받는다 — 여기서 생성하지 않으므로
이 모듈 import만으로는 Neo4j/Gemini 연결이 생기지 않는다.
외부 부작용: 주입된 클라이언트를 통한 index 조회/검색.
기존 facade: tools/vector.py (graphRAG), text_rag.py (textRAG).

Moved verbatim from tools/vector.py and text_rag.py (modularization work order
Phase 5.1/5.2).
"""

from __future__ import annotations

from langchain_neo4j import Neo4jVector

from chatbot.domain.evidence_models import Evidence
from chatbot.retrieval.vector_documents import docs_to_evidence
from chatbot.retrieval.vector_query import (
    _build_light_retrieval_query,
    _build_retrieval_query,
)
from rag_config import index_config_for

# textRAG 전용 top-k. 그래프 메타 확장이 없으므로 다양한 후보 확보 위해 상향.
TOP_K = 10

# 언어별 retriever를 lazy init 후 캐싱. 한 세션 안에서 같은 언어로 여러 번
# 질문해도 Neo4jVector 인스턴스는 한 번만 만든다. graphRAG(rich)와
# textRAG(light)는 서로 다른 retrieval_query/top-k를 쓰므로 캐시가 분리된다.
_graphrag_retrievers: dict = {}
_textrag_retrievers: dict = {}


def _new_vector_store(lang: str, retrieval_query: str, *, embeddings, graph):
    cfg = index_config_for(lang)
    return Neo4jVector.from_existing_index(
        embeddings,
        graph=graph,
        index_name=cfg["index_name"],
        node_label="Entry",
        text_node_property=cfg["text_property"],
        embedding_node_property=cfg["embedding_property"],
        retrieval_query=retrieval_query,
    )


def get_graphrag_retriever(lang: str, *, embeddings, graph):
    """graphRAG / 레거시 ReAct vector tool이 쓰는 rich-투영 retriever."""
    cfg = index_config_for(lang)
    if lang not in _graphrag_retrievers:
        neo4jvector = _new_vector_store(
            lang, _build_retrieval_query(cfg["text_property"]),
            embeddings=embeddings, graph=graph)
        _graphrag_retrievers[lang] = neo4jvector.as_retriever()
    return _graphrag_retrievers[lang]


def get_textrag_retriever(lang: str, *, embeddings, graph):
    """`lang` (= question_language)에 대응하는, 캐시된 "입력 문자열 추출 →
    벡터 retriever" 파이프라인만 반환한다. 문서 metadata 준비는 의도적으로
    포함하지 않는다 — 그건 response_language에 좌우되는 별도 단계다 (work
    order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §4
    Phase 4 item 2)."""
    cfg = index_config_for(lang)
    if lang not in _textrag_retrievers:
        neo4jvector = _new_vector_store(
            lang, _build_light_retrieval_query(cfg["text_property"]),
            embeddings=embeddings, graph=graph)
        base_retriever = neo4jvector.as_retriever(search_kwargs={"k": TOP_K})
        # `create_retrieval_chain` only auto-extracts `x["input"]` for a bare
        # `BaseRetriever`; this cached object is already a composite
        # Runnable (kept that way so a further step can be appended at call
        # time without re-wrapping), so the extraction step is included
        # here explicitly.
        _textrag_retrievers[lang] = (lambda x: x["input"]) | base_retriever
    return _textrag_retrievers[lang]


def retrieve_sihwa_evidence(query: str, *, retriever) -> Evidence:
    """Retrieve structured vector evidence for the graphRAG pipeline.

    Returns an Evidence(kind='vector') with documents, Person/Place entities
    (with authority IDs where present), and provenance. Does NOT call
    create_stuff_documents_chain and does NOT produce a final answer — the
    single final synthesis boundary owns all user-facing prose."""
    docs = retriever.invoke(query)
    return docs_to_evidence(docs)

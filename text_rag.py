"""textRAG 모드 — Entry.textKor/textChi/textEng 벡터 인덱스에 대한 의미 기반
검색만으로 답하는 단순 RAG chain.

정확한 범위 (사용자 안내 문구와 일치해야 함):
- Entry 본문에 대한 의미 벡터 검색을 수행한다.
- 그래프 관계 추론·구조 질의(작자·관직·시대·비평 관계 등)는 수행하지 않는다.
- 단, Entry–Work 포함 관계([:HAS_PART])는 출처·인용 메타데이터(시화집명 등)를
  붙이기 위해서만 조회한다. "그래프를 전혀 사용하지 않는다"는 표현은 부정확하므로
  사용하지 말 것.

- ReAct agent를 사용하지 않고 create_retrieval_chain을 직접 호출한다.
- 세션 언어(effective_language)에 맞는 in-language 인덱스를 선택한다.
- 인용을 위한 가벼운 메타(Entry.id, position, source_work_kor/eng, poetrytalks_link)만
  metadata로 반환한다.
- 결과가 없거나 질문에 부적합할 때 graphRAG 모드로 전환하도록 안내한다.
- 대화 이력은 session_id에 `::textRAG` suffix를 붙여 graphRAG와 완전 분리한다.
"""

import streamlit as st

from llm import llm, embeddings
from graph import graph

from langchain_neo4j import Neo4jVector, Neo4jChatMessageHistory
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.chains import create_retrieval_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.history import RunnableWithMessageHistory

from utils import get_session_id
from tools.evidence import POETRYTALKS_BASE_URL, collect_node_references, docs_to_evidence
from tools.answer_renderer import assemble_final_answer
from tools.synthesis import build_citations
from tools.vectorrag_prompt import document_prompt_for_lang, prepare_documents_for_prompt


# ──────────────────────────────────────────────
# 언어별 인덱스 라우팅
# `rag_config` 모듈이 유일한 소유자. 텍스트RAG와 그래프RAG(tools/vector.py) 모두
# 같은 dict를 참조하므로 라벨/인덱스명 변경 시 rag_config 한 곳만 편집한다.
# ──────────────────────────────────────────────
from rag_config import index_config_for

TOP_K = 10  # 그래프 메타 확장이 없으므로 다양한 후보 확보 위해 상향

_LANGUAGE_LABEL = {
    "ko": "Korean (한국어)",
    "en": "English",
    "zh": "Chinese (中文)",
}


def _build_light_retrieval_query(text_property: str) -> str:
    """textRAG 전용 가벼운 메타 retrieval_query.
    Entry.id + Entry.position + Entry/Work 이름 + Poetry Talks 링크에 더해,
    한자 원문·번역 병기 인용을 위해 Entry 자신의 세 언어 본문(textChi/textKor/textEng)
    도 metadata에 포함한다. 그래프 관계·인물·주제 확장은 여전히 제외.

    `entry_name_kor`/`entry_name_eng` (work order:
    CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §4 Phase 4 item 1)
    are consumed by `tools.evidence.node_references_from_vector_meta`, which
    already reads exactly these two keys for the Entry's own NodeReference —
    this projection is the only piece that was missing."""
    return f"""
RETURN
    node.{text_property} AS text,
    score,
    {{
        entry_id: node.ID,
        entry_position: node.position,
        entry_name_kor: node.nameKor,
        entry_name_eng: node.nameEng,
        original_chinese: node.textChi,
        korean_translation: node.textKor,
        english_translation: node.textEng,
        source_work_kor: [(w:Work)-[:HAS_PART]->(node) | w.nameKor][0],
        source_work_eng: [(w:Work)-[:HAS_PART]->(node) | w.nameEng][0],
        source_work_id: [(w:Work)-[:HAS_PART]->(node) | w.ID][0],
        poetrytalks_link: '{POETRYTALKS_BASE_URL}' + node.ID
    }} AS metadata
"""


# 언어별 retriever lazy 캐시 — 캐시 key와 index 선택은 항상
# question_language(검색 언어)다. 응답 언어(response_language)는 이 캐시된
# 객체와 무관하게 매 호출 시점에 별도로 적용된다 (work order
# CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §4 Phase 4
# item 2) — question_language != response_language인 턴에서 인용 순서가
# "검색에 쓰인 언어"로 고정되어 버리는 문제를 피하기 위해, 문서 준비
# (`prepare_documents_for_prompt`)를 더 이상 이 캐시된 lambda 안에 굽지 않는다.
_retrievers: dict = {}


def _get_text_retriever_for_lang(lang: str):
    """`lang` (= question_language)에 대응하는, 캐시된 "입력 문자열 추출 →
    벡터 retriever" 파이프라인만 반환한다. 문서 metadata 준비는 의도적으로
    포함하지 않는다 — 그건 response_language에 좌우되는 별도 단계다."""
    cfg = index_config_for(lang)
    if lang not in _retrievers:
        neo4jvector = Neo4jVector.from_existing_index(
            embeddings,
            graph=graph,
            index_name=cfg["index_name"],
            node_label="Entry",
            text_node_property=cfg["text_property"],
            embedding_node_property=cfg["embedding_property"],
            retrieval_query=_build_light_retrieval_query(cfg["text_property"]),
        )
        base_retriever = neo4jvector.as_retriever(search_kwargs={"k": TOP_K})
        # `create_retrieval_chain` only auto-extracts `x["input"]` for a bare
        # `BaseRetriever`; this cached object is already a composite
        # Runnable (kept that way so a further step can be appended at call
        # time without re-wrapping), so the extraction step is included
        # here explicitly — see `generate_text_rag_response`.
        _retrievers[lang] = (lambda x: x["input"]) | base_retriever
    return _retrievers[lang]


# ──────────────────────────────────────────────
# 언어별 fallback 안내문 (graphRAG로 유도)
# ──────────────────────────────────────────────
FALLBACK_HINT = {
    "ko": "이 질문은 텍스트 벡터 검색으로 적합한 결과를 찾지 못했습니다. 사이드바에서 graphRAG 모드로 전환한 뒤 다시 질문해 주세요.",
    "en": "This question could not be answered with text-only vector search. Please switch to graphRAG mode in the sidebar and try again.",
    "zh": "此问题在文本向量搜索模式下未能找到合适的答案。请在侧边栏切换到 graphRAG 模式后再试。",
}


def _build_prompt(response_language: str):
    """`response_language`(이번 턴 최종 출력 언어)를 반영한 ChatPromptTemplate
    생성. work order
    CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §5.2 라우팅
    계약에 따라 검색 index 언어(question_language)와는 독립적으로 전달받는다
    — 세션 상태를 직접 읽지 않아 호출부(`generate_text_rag_response`)가
    이미 확정한 값과 어긋날 일이 없다.

    work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §4
    Phase 4: the model writes the answer BODY only — Sources is assembled
    deterministically by `generate_text_rag_response()` via
    `tools.answer_renderer.assemble_final_answer()`, the SAME code path
    graphRAG uses, so any Sources-style section the model writes anyway is
    discarded and replaced exactly once (`strip_model_sources` +
    `render_sources_section`). Each `{context}` document already carries a
    `provenance_block` (Entry/Work id+name+link) and a `quoted_text_block`
    whose three parallel-language texts are PRE-ORDERED into this session's
    response-language priority (work order §3.1) — the model must not
    reorder them back."""
    label = _LANGUAGE_LABEL.get(response_language, _LANGUAGE_LABEL["ko"])
    fallback = FALLBACK_HINT.get(response_language, FALLBACK_HINT["ko"])

    system_msg = (
        f"이번 답변은 반드시 {label}로 작성하세요. "
        "당신은 시화총림(詩話叢林) 데이터베이스의 텍스트 벡터 검색 결과만을 근거로 "
        "답하는 어시스턴트입니다. 이 모드는 Entry 본문에 대한 의미 벡터 검색을 "
        "수행하며, 그래프 관계 추론이나 구조적 관계 질의는 수행하지 않습니다. "
        "Entry가 속한 시화집(Work) 포함 관계는 출처·인용 표기를 위해서만 사용됩니다.\n\n"

        "[답변 규칙]\n"
        "1. context에 있는 Entry 본문(원문·번역)만을 근거로 답하세요.\n"
        "2. 원문 인용은 그대로 유지하고, 절대 번역·요약·변형하지 마세요. 각 context "
        "항목의 인용 가능한 본문(quoted_text_block)은 이미 이번 답변 언어의 "
        "제시 순서로 정렬되어 있습니다 — 그 순서 그대로 인용하고 임의로 재배열하지 "
        "마세요.\n"
        "3. Sources / 출처 / References / 参考 섹션을 직접 작성하지 마세요. "
        "어떤 언어로도, 어떤 markdown 깊이로도 작성하지 마세요. 답변 본문을 쓰고 "
        "나면 시스템이 검증된 출처 목록(poetrytalks wikidata 링크 포함)을 "
        "결정론적으로 자동 첨부합니다. 직접 작성한 Sources 섹션은 모두 폐기되고 "
        "교체되므로, 답변 본문에만 집중하세요.\n"
        "4. context에 답에 필요한 근거가 없거나 검색 결과가 질문과 관련성이 낮으면, "
        f"다음 문구를 사용자에게 안내하세요:\n"
        f"     '{fallback}'\n"
        "5. 그래프 기반 사실(작자·시대·비평 관계 등)은 이 모드에서 알 수 없으므로, "
        "이런 질문을 받으면 위 안내 문구로 응답하세요.\n\n"

        "참고할 시화 자료(context):\n{context}"
    )

    return ChatPromptTemplate.from_messages(
        [
            ("system", system_msg),
            ("human", "{input}"),
        ]
    )


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

    work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §4
    Phase 4: the LLM writes ONLY the answer body (`_build_prompt` rule 3).
    `result["context"]` — the retrieved, prepared Documents — is normalized
    via `docs_to_evidence()` and run through the SAME deterministic
    assembly boundary graphRAG uses (`build_citations()` +
    `assemble_final_answer()`), so Sources is built by code exactly once,
    regardless of anything the model wrote in a Sources-shaped section."""
    if response_language is None:
        response_language = (st.session_state.get("response_language")
                             or st.session_state.get("effective_language", "ko"))
    if question_language is None:
        question_language = st.session_state.get("question_language") or response_language

    # 캐시된 base retriever는 question_language(검색 index)로 선택하고,
    # 문서 metadata 준비(`prepare_documents_for_prompt`)는 response_language로
    # 매 호출마다 새로 적용한다 — 캐시된 lambda 안에 굽지 않는다(work order
    # §4 Phase 4 item 2).
    base_retriever = _get_text_retriever_for_lang(question_language)
    retriever = base_retriever | RunnableLambda(
        lambda docs: prepare_documents_for_prompt(docs, response_language))

    doc_chain = create_stuff_documents_chain(
        llm, _build_prompt(response_language),
        document_prompt=document_prompt_for_lang(response_language))
    retrieval_chain = create_retrieval_chain(retriever, doc_chain)

    # 이력 유지가 필요하면 RunnableWithMessageHistory로 감싼다.
    # create_retrieval_chain의 출력은 'context'/'answer' 키를 갖는다.
    with_history = RunnableWithMessageHistory(
        retrieval_chain,
        _get_memory,
        input_messages_key="input",
        history_messages_key="chat_history",
        output_messages_key="answer",
    )

    # session_id에 ::textRAG suffix로 graphRAG와 완전 분리
    session_id = f"{get_session_id()}::textRAG"

    try:
        result = with_history.invoke(
            {"input": user_input},
            {"configurable": {"session_id": session_id}},
        )
    except ValueError as e:
        # Gemini 빈 스트림 응답 등 — graphRAG와 동일하게 graceful 처리
        if "No generation chunks were returned" in str(e):
            return FALLBACK_HINT.get(response_language, FALLBACK_HINT["ko"])
        raise

    body = result.get("answer") or ""
    if not body.strip():
        return FALLBACK_HINT.get(response_language, FALLBACK_HINT["ko"])

    evidence = docs_to_evidence(result.get("context") or [])
    evidence_dict = {"graph": None, "vector": evidence, "external": None}
    citations = build_citations(evidence_dict, response_language)
    node_refs = [r.to_dict() for r in collect_node_references(evidence)]
    link_targets = [e.to_dict() for e in evidence.entities] + node_refs

    return assemble_final_answer(body, citations, response_language, entities=link_targets)

"""textRAG (vectorRAG) application pipeline.

책임: Entry 본문 벡터 검색 결과로 답변 본문을 1회 생성하고, graphRAG와 동일한
결정론적 경계(build_citations → assemble_final_answer)로 최종 응답을 조립한다.

정확한 범위 (사용자 안내 문구와 일치해야 함):
- Entry 본문에 대한 의미 벡터 검색을 수행한다.
- 그래프 관계 추론·구조 질의(작자·관직·시대·비평 관계 등)는 수행하지 않는다.
- 단, Entry–Work 포함 관계([:HAS_PART])는 출처·인용 메타데이터(시화집명 등)를
  붙이기 위해서만 조회한다. "그래프를 전혀 사용하지 않는다"는 표현은 부정확하므로
  사용하지 말 것.

허용 의존성: langchain 체인 구성, chatbot.domain/retrieval/synthesis,
tools.answer_renderer, tools.vectorrag_prompt. llm·retriever·이력 핸들·
session_id·언어는 모두 주입받는다 — Streamlit session state를 읽지 않는다
(읽기는 composition root인 text_rag.py 담당).
외부 부작용: 주입된 retriever 검색, LLM 호출 1회, 이력 읽기/쓰기
(RunnableWithMessageHistory 소유).
기존 facade: text_rag.py의 `generate_text_rag_response`.

Moved verbatim from text_rag.py (modularization work order Phase 5.3).
graphRAG와 공유하는 것은 retriever와 evidence/citation helper이며 대화 이력
namespace는 공유하지 않는다 — `::textRAG` suffix는 호출자가 붙인다.
"""

from __future__ import annotations

from langchain_classic.chains import create_retrieval_chain
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from langchain_core.runnables.history import RunnableWithMessageHistory

from chatbot.domain.evidence_merge import collect_node_references
from chatbot.retrieval.vector_documents import docs_to_evidence
from chatbot.synthesis.citations import build_citations
from tools.answer_renderer import assemble_final_answer
from tools.vectorrag_prompt import document_prompt_for_lang, prepare_documents_for_prompt

_LANGUAGE_LABEL = {
    "ko": "Korean (한국어)",
    "en": "English",
    "zh": "Chinese (中文)",
}


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
    — 세션 상태를 직접 읽지 않아 호출부가 이미 확정한 값과 어긋날 일이 없다.

    work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §4
    Phase 4: the model writes the answer BODY only — Sources is assembled
    deterministically by `run_vectorrag_pipeline()` via
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


def run_vectorrag_pipeline(user_input: str,
                           response_language: str,
                           *,
                           base_retriever,
                           llm,
                           memory_factory,
                           session_id: str) -> str:
    """textRAG 한 턴: 검색 → 답변 본문 1회 생성 → 결정론적 Sources 조립.

    `base_retriever`는 question_language로 선택된 캐시된 retriever이고,
    `session_id`는 이미 `::textRAG` namespace가 붙은 값이다 (호출자 책임).
    `memory_factory(session_id)`가 이력 핸들을 만든다 — 이력 저장은
    RunnableWithMessageHistory가 단독 소유한다.

    work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §4
    Phase 4: the LLM writes ONLY the answer body (`_build_prompt` rule 3).
    `result["context"]` — the retrieved, prepared Documents — is normalized
    via `docs_to_evidence()` and run through the SAME deterministic
    assembly boundary graphRAG uses (`build_citations()` +
    `assemble_final_answer()`), so Sources is built by code exactly once,
    regardless of anything the model wrote in a Sources-shaped section."""
    # 캐시된 base retriever는 question_language(검색 index)로 선택되어 들어오고,
    # 문서 metadata 준비(`prepare_documents_for_prompt`)는 response_language로
    # 매 호출마다 새로 적용한다 — 캐시된 lambda 안에 굽지 않는다(work order
    # §4 Phase 4 item 2).
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
        memory_factory,
        input_messages_key="input",
        history_messages_key="chat_history",
        output_messages_key="answer",
    )

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

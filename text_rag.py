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
from langchain_core.runnables.history import RunnableWithMessageHistory

from utils import get_session_id


# ──────────────────────────────────────────────
# 언어별 인덱스 라우팅 (tools/vector.py의 INDEX_BY_LANG와 동일)
# ──────────────────────────────────────────────
INDEX_BY_LANG = {
    "ko": {
        "index_name": "EntryTextsKor",
        "text_property": "textKor",
        "embedding_property": "textEmbedding_Kor",
    },
    "en": {
        "index_name": "EntryTextsEng",
        "text_property": "textEng",
        "embedding_property": "textEmbedding_Eng",
    },
    "zh": {
        "index_name": "EntryTextsChi",
        "text_property": "textChi",
        "embedding_property": "textEmbedding_Chi",
    },
}

TOP_K = 10  # 그래프 메타 확장이 없으므로 다양한 후보 확보 위해 상향

_LANGUAGE_LABEL = {
    "ko": "Korean (한국어)",
    "en": "English",
    "zh": "Chinese (中文)",
}


def _build_light_retrieval_query(text_property: str) -> str:
    """textRAG 전용 가벼운 메타 retrieval_query.
    Entry.id + Entry.position + Work 이름 + Poetry Talks 링크에 더해,
    한자 원문·번역 병기 인용을 위해 Entry 자신의 세 언어 본문(textChi/textKor/textEng)
    도 metadata에 포함한다. 그래프 관계·인물·주제 확장은 여전히 제외."""
    return f"""
RETURN
    node.{text_property} AS text,
    score,
    {{
        entry_id: node.id,
        entry_position: node.position,
        original_chinese: node.textChi,
        korean_translation: node.textKor,
        english_translation: node.textEng,
        source_work_kor: [(w:Work)-[:HAS_PART]->(node) | w.nameKor][0],
        source_work_eng: [(w:Work)-[:HAS_PART]->(node) | w.nameEng][0],
        source_work_id: [(w:Work)-[:HAS_PART]->(node) | w.id][0],
        poetrytalks_link: 'https://poetrytalks.org/' + node.id
    }} AS metadata
"""


# 언어별 retriever lazy 캐시
_retrievers: dict = {}


def _get_text_retriever_for_lang(lang: str):
    cfg = INDEX_BY_LANG.get(lang, INDEX_BY_LANG["ko"])
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
        _retrievers[lang] = neo4jvector.as_retriever(search_kwargs={"k": TOP_K})
    return _retrievers[lang]


# ──────────────────────────────────────────────
# 언어별 fallback 안내문 (graphRAG로 유도)
# ──────────────────────────────────────────────
FALLBACK_HINT = {
    "ko": "이 질문은 텍스트 벡터 검색으로 적합한 결과를 찾지 못했습니다. 사이드바에서 graphRAG 모드로 전환한 뒤 다시 질문해 주세요.",
    "en": "This question could not be answered with text-only vector search. Please switch to graphRAG mode in the sidebar and try again.",
    "zh": "此问题在文本向量搜索模式下未能找到合适的答案。请在侧边栏切换到 graphRAG 模式后再试。",
}


def _build_prompt():
    """호출 시점의 effective_language를 반영한 ChatPromptTemplate 생성."""
    user_language = st.session_state.get("effective_language", "ko")
    label = _LANGUAGE_LABEL.get(user_language, _LANGUAGE_LABEL["ko"])
    fallback = FALLBACK_HINT.get(user_language, FALLBACK_HINT["ko"])

    system_msg = (
        f"이번 답변은 반드시 {label}로 작성하세요. "
        "당신은 시화총림(詩話叢林) 데이터베이스의 텍스트 벡터 검색 결과만을 근거로 "
        "답하는 어시스턴트입니다. 이 모드는 Entry 본문에 대한 의미 벡터 검색을 "
        "수행하며, 그래프 관계 추론이나 구조적 관계 질의는 수행하지 않습니다. "
        "Entry가 속한 시화집(Work) 포함 관계는 출처·인용 표기를 위해서만 사용됩니다.\n\n"

        "[답변 규칙]\n"
        "1. context에 있는 Entry 본문(textKor/textChi/textEng)만을 근거로 답하세요.\n"
        "2. 원문 인용은 그대로 유지하고, 절대 번역·요약·변형하지 마세요.\n"
        "3. 매 인용마다 다음 가벼운 출처를 명시하세요:\n"
        "     시화집명(source_work_kor / source_work_eng)\n"
        "     항목 위치: 제 [entry_position] 항목 (entry_id)\n"
        "     Poetry Talks 링크: poetrytalks_link\n"
        "   예: 지봉유설 > 제3항목 (E003) > https://poetrytalks.org/E003\n"
        "4. context에 답에 필요한 근거가 없거나 검색 결과가 질문과 관련성이 낮으면, "
        f"다음 문구를 사용자에게 안내하세요:\n"
        f"     '{fallback}'\n"
        "5. 그래프 기반 사실(작자·시대·비평 관계 등)은 이 모드에서 알 수 없으므로, "
        f"이런 질문을 받으면 위 안내 문구로 응답하세요.\n\n"

        # ────────────────────────────────────────────
        # 6. 한자 원문·번역 병기 지시 (Bilingual quotation rule)
        # ────────────────────────────────────────────
        "6. [한자 원문·번역 병기]\n"
        "   시화 자료를 인용할 때 metadata의 original_chinese(textChi), "
        "korean_translation(textKor), english_translation(textEng)이 모두 존재하면 "
        "다음 순서·형식으로 병기하세요:\n"
        "     ① 한자 원문(original_chinese)을 먼저, 원문 그대로(구두점 · 줄바꿈 포함)\n"
        "     ② 그 아래 사용자 언어에 맞는 번역:\n"
        f"        - 답변 언어가 Korean이면 korean_translation\n"
        f"        - 답변 언어가 English이면 english_translation\n"
        f"        - 답변 언어가 Chinese이면 original_chinese만으로 충분 (번역 병기 생략 가능)\n"
        "     ③ 원문과 번역은 반드시 blockquote(>) 또는 코드 블록으로 시각적 구분\n"
        "     ④ 병기 뒤에 출처(위 규칙 3) 명시\n"
        "   예시 형식 (사용자 언어가 Korean일 때):\n"
        "     > **[漢文原文]**\n"
        "     > 兩兩佳人弄夕暉。\n"
        "     > 青樓朱箔共依依。\n"
        "     >\n"
        "     > **[한국어 번역]**\n"
        "     > 쌍쌍의 가인들이 저녁 햇살 속에 노니는데,\n"
        "     > 청루의 붉은 발 속에서 함께 가련히 비치네.\n"
        "     — 출처: 어우야담 > 제N항목 (E###) > https://poetrytalks.org/E###\n"
        "   주의사항:\n"
        "   - 세 언어 중 일부만 존재(예: textEng가 null)하면 있는 것만 병기.\n"
        "   - textChi가 없고 textKor/textEng만 있으면 병기 없이 사용자 언어 번역만 인용.\n"
        "   - textChi의 원문 문자·구두점(。「」 등)을 절대 정규화하지 말고 그대로 유지.\n"
        "   - 번역문도 절대 변형하지 말고 데이터베이스 저장 형태 그대로 인용.\n"
        "   - 사용자 언어가 Japanese 등이라면 textChi를 우선 인용하고 필요 시 textKor을 부가.\n\n"

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


def generate_text_rag_response(user_input: str) -> str:
    """textRAG 모드의 사용자 응답 생성 엔트리포인트.
    bot.py에서 mode=='textRAG'일 때 호출된다."""
    user_language = st.session_state.get("effective_language", "ko")

    retriever = _get_text_retriever_for_lang(user_language)
    doc_chain = create_stuff_documents_chain(llm, _build_prompt())
    retrieval_chain = create_retrieval_chain(retriever, doc_chain)

    # 이력 유지가 필요하면 RunnableWithMessageHistory로 감싼다.
    # create_retrieval_chain의 출력은 'answer' 키를 갖는다.
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
            return FALLBACK_HINT.get(user_language, FALLBACK_HINT["ko"])
        raise

    return result.get("answer") or FALLBACK_HINT.get(user_language, FALLBACK_HINT["ko"])

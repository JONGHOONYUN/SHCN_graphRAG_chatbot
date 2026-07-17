from typing import Optional

import streamlit as st
from llm import llm, embeddings
from graph import graph

from langchain_neo4j import Neo4jVector
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.chains import create_retrieval_chain

from langchain_core.prompts import ChatPromptTemplate


instructions = (
    "당신은 시화총림(詩話叢林) 전문가입니다. "
    "주어진 context의 시화 자료만을 근거로 답하세요. "
    "context에 없는 내용은 '제공된 자료에 없습니다'라고 답하세요. "

    # Source text rule
    "한문 원문(textChi), 한국어 번역(textKor), 영문 번역(textEng)은 "
    "절대로 번역·요약·변형하지 마세요. 원문 그대로 제시하고, "
    "별도로 해설이나 맥락을 덧붙이세요. "

    # Provenance — Work label (not Book)
    "답변할 때 반드시 출처를 명시하세요: "
    "시화집명(Work.nameKor / Work.nameEng), 항목 번호(Entry.position), 항목 ID(Entry.id). "
    

    # Entity links
    "언급된 모든 개체에 Poetry Talks 링크를 포함하세요: "
    "https://poetrytalks.org/ + node id (예: https://poetrytalks.org/P027). "
    "사용자 언어에 맞는 uselang 파라미터를 추가하세요 "
    "(en / ko / zh / fr). "

    # External authority IDs — link-only in this legacy path (no HTTP fetching here)
    "Person·Place 노드는 여러 외부 authority ID를 보유합니다. 단, 이 검색 경로는 "
    "외부 API를 호출하지 않습니다. 따라서 ID가 있어도 그 사이트의 '내용'을 아는 것처럼 "
    "쓰지 말고, 오직 참고 링크로만 제시하세요 ('~에 따르면' 금지). "
    "검증된 링크 패턴만 사용하고, 아래 목록에 없는 ID는 링크를 만들지 마세요:\n"
    "  · idWikidata      → https://www.wikidata.org/wiki/{{id}}\n"
    "  · idAKSency       → https://encykorea.aks.ac.kr/Article/{{id}}\n"
    "  · idLOC           → https://id.loc.gov/authorities/names/{{id}}\n"
    "  · idOpenLibrary   → https://openlibrary.org/authors/{{id}}\n"
    "  · idBritannica    → https://www.britannica.com/{{id}}\n"
    "  · idBNF           → https://data.bnf.fr/ark:/12148/cb{{id}}\n"
    "  · idWorldHistory  → https://www.worldhistory.org/{{id}}/\n"
    "  · idAKSdigerati   → 링크를 직접 만들지 마세요. 이 값(koreanPerson_*/koreanPlace_*)은 "
    "API 요청용이며, 공개 링크는 API 응답의 canonical link로만 얻을 수 있습니다.\n"
    "  · idAKSsillok / idAKSkdp / idNLK / idEncyChina / idAcademiaSinica / "
    "idBritishMuseum / idAKSmap → 검증된 공개 URL 패턴이 없으므로 링크를 만들지 마세요.\n"
    "  · 구조적 사실·외부 전기 정보가 필요한 질문은 graphRAG 근거 파이프라인이 "
    "담당합니다(이 경로가 아님). "

    # Place geographic data (gis 문자열, 예: '37° 56\\' 17.50\" N, 126° 35\\' 16.06\" E')
    "Place 노드에 gis 좌표가 있으면 지리 정보로 활용하세요. "
    "image 필드가 있으면 이미지 URL로 활용 가능. "
    "Place는 idAKSdigerati / idAKSmap / idAKSency의 세 가지 authority가 "
    "각각 다른 하위 집합에 채워져 있으니 존재하는 것을 우선 인용하세요. "

    # Language
    "사용자가 쓰는 언어로 답변하세요. "
    "인물명은 데이터베이스에 저장된 nameEng, nameMR, namePY, nameRR를 그대로 사용하고 "
    "다시 로마자화하지 마세요. "
    "일본어 등 한자 사용 언어 사용자에게는 nameChi를 우선 사용하세요. "
    "프랑스어 사용자에게 Topic은 nameFra가 있으면 우선 사용하세요."

    # ────────────────────────────────────────────────────────────
    # Graph reasoning enhancements (added below — how to interpret the retrieved context)
    # ────────────────────────────────────────────────────────────

    # A. Metadata dict — 필드별 밀도와 해석 (실측 스키마 기반)
    "\n\n[Retrieved context metadata 해석 가이드]\n"
    "매 검색 결과는 Entry 노드 하나와 metadata dict로 구성됩니다. 필드별로 밀도가 "
    "다르니 존재 여부부터 확인한 뒤 인용하세요:\n"
    "  · entry_id / entry_position / source_work_* : 거의 항상 존재. 인용 필수.\n"
    "  · korean_translation / original_chinese / english_translation : 대부분 존재.\n"
    "    (수사·기교 해설은 원문 옆에 별도로 붙이고, 원문 자체는 변형 금지.)\n"
    "  · creator·creator_eng·creator_chi : Entry 작성자(=서술의 저자). "
    "    Poem/Critique의 저자와 혼동하지 마세요 (아래 B 참조).\n"
    "  · creator_year_birth/year_death : 부분 존재. 없으면 creator_era 사용 폴백.\n"
    "  · creator_era : 대부분 존재 (Person 591건). nameEng + yearStart~yearEnd로 표기.\n"
    "  · creator_external_ids : 15종 authority ID 사전. 값이 있는 것만 골라 인용.\n"
    "  · mentioned_persons / audiences / topics / forms_types / places / "
    "critical_terms / era : 태그 밀도가 다양. 없으면 '기록되지 않음'으로 처리.\n"
    "  · contained_poems / contained_critiques : Entry에 속한 시/비평. 정문 인용용.\n"
    "  · places.gis : 좌표 문자열(예: 37° 56' 17.50\" N, 126° 35' 16.06\" E) — "
    "값이 있는 경우 지리 정보로 활용, 없으면 조용히 생략.\n"

    # B. Entry 하나에 등장할 수 있는 세 가지 Person 역할
    "\n[한 Entry에 등장하는 Person의 세 가지 역할 — 절대 혼동 금지]\n"
    "  1) AUTHOR (creator/creator_eng/creator_chi): Entry 서술을 지은 사람. "
    "     보통 시화집의 저자와 동일. HAS_CREATOR 관계.\n"
    "  2) SUBJECTS (mentioned_persons): 서술 안에서 평가·언급되는 대상. "
    "     비평의 대상이 될 수 있음. HAS_SUBJECT_PERSON 관계.\n"
    "  3) ADDRESSEES (audiences): 시가 헌정·수신된 인물. HAS_AUDIENCE 관계는 "
    "     Poem에만 존재하므로 audiences는 contained_poems를 경유한 결과.\n"
    "예) 홍만종(AUTHOR)이 '허균(SUBJECT)이 이백(TEXT SUBJECT)의 시를 논하며 "
    "    권필(AUDIENCE)에게 보낸 편지'를 서술 → 네 사람의 역할이 다름.\n"

    # C. Work의 두 종류 (실측 116개 중 두 유형)
    "\n[Work 두 종류 구분 — 인용 방식이 다름]\n"
    "  · 시화 원전 (B001~B025 대: position 있음, descEng 상세): 이 챗봇의 "
    "    1차 사료. 파한집(B001), 지봉유설(B016), 성수시화(B018), 호곡시화(B023) 등. "
    "    전체 출처 경로(Work → Entry → Poem/Critique)로 인용.\n"
    "  · 외부 참조 서적 (B026~B131: position 없거나 descEng 없음): 시화 안에서 "
    "    인용·언급되는 다른 문헌. 예: 당서예문지(B028), 시경(B035), 논어(B067), "
    "    태평광기(B077). 답변에서는 배경·컨텍스트로만 언급, 1차 인용 대상 아님.\n"

    # D. 시대 정보 우선순위 (Era 계층)
    "\n[시대(Era) 정보 해석 우선순위]\n"
    "  1) creator_year_birth / creator_year_death — 정확한 연도 우선.\n"
    "  2) creator_era.yearStart / yearEnd — 시대 범위로 폴백.\n"
    "  3) creator_era.nameKor / nameEng — 시대명만 표기.\n"
    "  Era는 계층 구조(예: 조선 → 조선 후기)를 가지므로 하위 시대가 태그된 "
    "  경우가 있음. 상위 시대 질의라면 하위 시대 결과도 그 상위에 속함.\n"

    # E. Multi-hop 종합 추론 워크플로우
    "\n[복합 질문을 만났을 때 종합 추론 순서]\n"
    "  Step 1: Entry 본문(text)에서 핵심 사실 확인.\n"
    "  Step 2: metadata.mentioned_persons / topics / places / critical_terms 로 "
    "          질문의 엔티티가 실제로 태그되었는지 검증.\n"
    "  Step 3: contained_poems / contained_critiques 에서 인용 가능한 원문 발췌.\n"
    "  Step 4: creator_era + creator_external_ids로 작자를 학술 authority에 링크.\n"
    "  Step 5: source_work_* 로 시화집 출처를 명시.\n"
    "  Step 6: 답변은 락 언어로, 원문·인용문은 원어 그대로.\n"

    # F. 빈 필드 처리
    "\n[비어 있는 metadata 필드 처리 원칙]\n"
    "  · null/빈 값은 절대 지어내지 마세요. 학술 챗봇의 신뢰성이 우선.\n"
    "  · '기록되지 않음' / 'not recorded in the database' 로 명시하거나 언급 생략.\n"
    "  · 특히 external ID가 없으면 링크를 지어내지 말고 poetrytalks.org 링크만 제공.\n"
    "  · creator_year_birth/death가 없으면 creator_era의 yearStart~yearEnd로 폴백.\n"

    # G. Authority linking 활용
    "\n[Cross-lingual authority linking — 참고 링크 전용]\n"
    "  · creator_external_ids.wikidata가 있으면 참고 링크로 함께 제시 (교차 검색 유용).\n"
    "  · 이 경로에서는 어떤 authority도 조회하지 않으므로, 링크만 제시하고 "
    "그 사이트의 내용을 사실로 서술하지 마세요.\n"
    "  · 위 '검증된 링크 패턴' 목록에 없는 ID는 링크를 만들지 말고 생략.\n"
    "  · authority ID 값이 아예 없는 경우 이 섹션은 통째로 건너뛰기.\n"
)

_LANGUAGE_LABEL = {
    "ko": "Korean (한국어)",
    "en": "English",
    "zh": "Chinese (中文)",
}


def _build_prompt():
    """매 호출 시 이번 턴 적용 언어(effective_language)를 반영한 prompt를 새로 생성.
    bot.py가 매 턴 'effective_language'를 갱신하므로 그 값을 그대로 사용."""
    user_language = st.session_state.get("effective_language", "ko")
    label = _LANGUAGE_LABEL.get(user_language, _LANGUAGE_LABEL["ko"])
    language_clause = (
        f"이번 답변은 반드시 {label}로 작성하세요. "
        "단, textChi/textKor/textEng/descEng의 인용은 원문 그대로 유지하세요. "
    )
    return ChatPromptTemplate.from_messages(
        [
            ("system", language_clause + instructions + "\n\n참고할 시화 자료(context):\n{context}"),
            ("human", "{input}"),
        ]
    )

# ──────────────────────────────────────────────
# 다국어 벡터 인덱스 라우팅
# Entry.textKor / textChi / textEng 각각에 대해 별도 임베딩과 vector index를
# 생성해 두었으므로(EntryTextsKor / EntryTextsChi / EntryTextsEng), 사용자 질문
# 언어(effective_language)에 맞는 in-language 인덱스로 매칭하여 검색 정확도를 높임.
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


def _build_retrieval_query(text_property: str) -> str:
    """언어별 retrieval_query를 생성. 'text' 필드만 해당 언어 속성으로 바꾸고
    metadata는 동일하게 세 언어의 본문·인물·주제·외부 authority ID까지 포함.

    실제 스키마 반영:
    - Work 라벨(구 :Book 오류 수정), HAS_SUBJECT_CRITICAL_TERM(언더스코어) 수정
    - Person은 idAKSdigerati 외 idWikidata, idCBDB, idAKSsillok 등 15종 authority ID 보유
    - Place는 latitude/longitude/gis 지리 정보 보유
    - Work/Topic은 descEng·descChi(Work) 설명 보유
    - Entry는 nameKor/Chi/Eng 이름 속성도 보유
    """
    return f"""
RETURN
    node.{text_property} AS text,
    score,
    {{
        entry_id: node.id,
        entry_position: node.position,
        entry_name_kor: node.nameKor,
        entry_name_chi: node.nameChi,
        entry_name_eng: node.nameEng,
        original_chinese: node.textChi,
        english_translation: node.textEng,
        korean_translation: node.textKor,
        poetrytalks_link: 'https://poetrytalks.org/' + node.id,
        source_work_kor: [(w:Work)-[:HAS_PART]->(node) | w.nameKor][0],
        source_work_eng: [(w:Work)-[:HAS_PART]->(node) | w.nameEng][0],
        source_work_chi: [(w:Work)-[:HAS_PART]->(node) | w.nameChi][0],
        source_work_id: [(w:Work)-[:HAS_PART]->(node) | w.id][0],
        source_work_desc: [(w:Work)-[:HAS_PART]->(node) | w.descEng][0],
        creator: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameKor][0],
        creator_eng: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameEng][0],
        creator_chi: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameChi][0],
        creator_mr: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameMR][0],
        creator_py: [(node)-[:HAS_CREATOR]->(p:Person) | p.namePY][0],
        creator_rr: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameRR][0],
        creator_id: [(node)-[:HAS_CREATOR]->(p:Person) | p.id][0],
        creator_year_birth: [(node)-[:HAS_CREATOR]->(p:Person) | p.yearBirth][0],
        creator_year_death: [(node)-[:HAS_CREATOR]->(p:Person) | p.yearDeath][0],
        creator_image: [(node)-[:HAS_CREATOR]->(p:Person) | p.image][0],
        creator_desc: [(node)-[:HAS_CREATOR]->(p:Person) | p.descEng][0],
        creator_external_ids: [(node)-[:HAS_CREATOR]->(p:Person) |
            {{aks_digerati: p.idAKSdigerati, aks_ency: p.idAKSency,
              aks_sillok: p.idAKSsillok, aks_kdp: p.idAKSkdp,
              cbdb: p.idCBDB, academia_sinica: p.idAcademiaSinica,
              wikidata: p.idWikidata, ency_china: p.idEncyChina,
              nlk: p.idNLK, loc: p.idLOC, bnf: p.idBNF,
              britannica: p.idBritannica, british_museum: p.idBritishMuseum,
              open_library: p.idOpenLibrary, world_history: p.idWorldHistory,
              yale_lux: p.idYaleLux}}][0],
        creator_era: [(node)-[:HAS_CREATOR]->(p:Person)-[:HAS_ERA]->(e:Era) |
            {{nameKor: e.nameKor, nameEng: e.nameEng,
             yearStart: e.yearStart, yearEnd: e.yearEnd}}][0],
        creator_gender: [(node)-[:HAS_CREATOR]->(p:Person)-[:HAS_GENDER]->(g:Topic) |
            g.nameEng][0],
        creator_office: [(node)-[:HAS_CREATOR]->(p:Person)-[:HAS_OFFICE]->(o:Topic) |
            {{nameKor: o.nameKor, nameEng: o.nameEng}}][0..3],
        creator_clan: [(node)-[:HAS_CREATOR]->(p:Person)-[:HAS_CLAN]->(cl:Topic) |
            {{nameKor: cl.nameKor, nameEng: cl.nameEng}}][0],
        mentioned_persons: [(node)-[:HAS_SUBJECT_PERSON]->(p:Person) |
            {{nameKor: p.nameKor, nameEng: p.nameEng, nameChi: p.nameChi,
              nameMR: p.nameMR, namePY: p.namePY, nameRR: p.nameRR,
              id: p.id,
              wikidata: p.idWikidata, aks_digerati: p.idAKSdigerati,
              aks_ency: p.idAKSency, aks_sillok: p.idAKSsillok,
              aks_kdp: p.idAKSkdp, cbdb: p.idCBDB,
              academia_sinica: p.idAcademiaSinica, ency_china: p.idEncyChina,
              nlk: p.idNLK, loc: p.idLOC, bnf: p.idBNF,
              britannica: p.idBritannica, british_museum: p.idBritishMuseum,
              open_library: p.idOpenLibrary, world_history: p.idWorldHistory,
              yale_lux: p.idYaleLux}}][0..5],
        audiences: [(node)-[:HAS_PART]->(pm:Poem)-[:HAS_AUDIENCE]->(a:Person) |
            {{nameKor: a.nameKor, nameEng: a.nameEng, nameChi: a.nameChi,
              id: a.id,
              wikidata: a.idWikidata, aks_digerati: a.idAKSdigerati,
              aks_ency: a.idAKSency, aks_sillok: a.idAKSsillok,
              aks_kdp: a.idAKSkdp, cbdb: a.idCBDB,
              academia_sinica: a.idAcademiaSinica, ency_china: a.idEncyChina,
              nlk: a.idNLK, loc: a.idLOC, bnf: a.idBNF,
              britannica: a.idBritannica, british_museum: a.idBritishMuseum,
              open_library: a.idOpenLibrary, world_history: a.idWorldHistory,
              yale_lux: a.idYaleLux}}][0..3],
        topics: [(node)-[:HAS_SUBJECT_TOPIC]->(t:Topic) |
            {{nameKor: t.nameKor, nameEng: t.nameEng, nameChi: t.nameChi,
              nameFra: t.nameFra, descEng: t.descEng}}][0..5],
        forms_types: [(node)-[:HAS_TYPE]->(t:Topic) |
            {{nameKor: t.nameKor, nameEng: t.nameEng, nameChi: t.nameChi}}][0..3],
        places: [(node)-[:HAS_SUBJECT_PLACE]->(pl:Place) |
            {{nameKor: pl.nameKor, nameEng: pl.nameEng, nameChi: pl.nameChi,
              id: pl.id, gis: pl.gis, image: pl.image,
              aks_digerati: pl.idAKSdigerati, aks_map: pl.idAKSmap,
              aks_ency: pl.idAKSency}}][0..3],
        critical_terms: [(node)-[:HAS_SUBJECT_CRITICAL_TERM]->(ct:CriticalTerm) |
            {{nameKor: ct.nameKor, nameEng: ct.nameEng, nameChi: ct.nameChi,
              descEng: ct.descEng}}][0..5],
        era: [(node)-[:HAS_SUBJECT_ERA]->(e:Era) |
            {{nameKor: e.nameKor, nameEng: e.nameEng,
              yearStart: e.yearStart, yearEnd: e.yearEnd}}][0],
        contained_poems: [(node)-[:HAS_PART]->(pm:Poem) |
            {{id: pm.id, position: pm.position,
              nameKor: pm.nameKor, nameChi: pm.nameChi, nameEng: pm.nameEng,
              textKor: pm.textKor, textChi: pm.textChi, textEng: pm.textEng}}][0..3],
        contained_critiques: [(node)-[:HAS_PART]->(c:Critique) |
            {{id: c.id, position: c.position,
              textKor: c.textKor, textChi: c.textChi, textEng: c.textEng}}][0..3]
    }} AS metadata
"""


# 언어별 retriever를 lazy init 후 캐싱. 한 세션 안에서 같은 언어로 여러 번 질문해도
# Neo4jVector 인스턴스는 한 번만 만든다.
_retrievers: dict = {}


def _get_retriever_for_lang(lang: str):
    cfg = INDEX_BY_LANG.get(lang, INDEX_BY_LANG["ko"])
    if lang not in _retrievers:
        neo4jvector = Neo4jVector.from_existing_index(
            embeddings,
            graph=graph,
            index_name=cfg["index_name"],
            node_label="Entry",
            text_node_property=cfg["text_property"],
            embedding_node_property=cfg["embedding_property"],
            retrieval_query=_build_retrieval_query(cfg["text_property"]),
        )
        _retrievers[lang] = neo4jvector.as_retriever()
    return _retrievers[lang]


def get_poetry_plot(input):
    # 매 호출 시 세션의 effective_language를 읽어 그에 맞는 in-language 인덱스로 라우팅.
    # bot.py가 매 턴 갱신하는 키이며, 없으면 ko로 폴백.
    # NOTE: 이 함수는 자체적으로 최종 답변 prose를 생성한다. graphRAG 파이프라인은
    # 대신 retrieve_sihwa_evidence()를 사용해 구조화된 근거만 수집하고, 최종 합성은
    # agent.synthesize_answer()에서 단 한 번 수행한다. get_poetry_plot는 하위 호환
    # (기존 ReAct tool)용으로만 남겨둔다.
    user_language = st.session_state.get("effective_language", "ko")
    retriever = _get_retriever_for_lang(user_language)
    question_answer_chain = create_stuff_documents_chain(llm, _build_prompt())
    plot_retriever = create_retrieval_chain(retriever, question_answer_chain)
    return plot_retriever.invoke({"input": input})


# ──────────────────────────────────────────────
# Structured retrieval for the graphRAG evidence pipeline
#
# retrieve_sihwa_evidence() returns an Evidence bundle (documents + entities +
# provenance) and NEVER generates a user-facing answer. The document→evidence
# normalization lives in tools/evidence.py (docs_to_evidence) so it can be
# unit-tested with hand-built Documents, no Neo4j required.
# ──────────────────────────────────────────────
from tools.evidence import Evidence, docs_to_evidence  # noqa: E402


def retrieve_sihwa_evidence(query: str, language: Optional[str] = None) -> Evidence:
    """Retrieve structured vector evidence for the graphRAG pipeline.

    Returns an Evidence(kind='vector') with documents, Person entities (with
    authority IDs where present), and provenance. Does NOT call
    create_stuff_documents_chain and does NOT produce a final answer.

    Retains the existing multilingual index routing via effective_language."""
    user_language = language or st.session_state.get("effective_language", "ko")
    retriever = _get_retriever_for_lang(user_language)
    docs = retriever.invoke(query)
    return docs_to_evidence(docs)
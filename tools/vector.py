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

    # External authority IDs — Person nodes carry up to 15 different external IDs
    "Person·Place 노드는 여러 외부 authority ID를 보유합니다. context에 존재하는 "
    "ID가 있으면 답변에 링크·정보를 함께 인용하세요. 사용자 언어·문화권에 따라 "
    "우선순위를 조정하세요:\n"
    "  · 한국어 사용자: idAKSdigerati, idAKSency, idAKSsillok, idAKSkdp, idNLK 우선\n"
    "  · 중국어/일본어 사용자: idCBDB, idAcademiaSinica, idEncyChina 우선\n"
    "  · 영어/유럽 사용자: idWikidata, idLOC, idBritannica, idBNF, idOpenLibrary, "
    "idBritishMuseum, idYaleLux 우선\n"
    "  · idWikidata는 모든 언어에서 유용한 크로스링구얼 authority이므로 "
    "존재하면 항상 함께 인용 권장. "
    "외부 링크 base URL: "
    "https://www.wikidata.org/wiki/{id} (Wikidata), "
    "https://encykorea.aks.ac.kr/Article/{id} (AKS 한국민족문화대백과), "
    "https://digerati.aks.ac.kr:85/api/IdValues/{id} (AKS Digerati Person), "
    "https://sillok.history.go.kr/{id} (조선왕조실록). "

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
              id: p.id, wikidata: p.idWikidata,
              aks_digerati: p.idAKSdigerati}}][0..5],
        audiences: [(node)-[:HAS_PART]->(pm:Poem)-[:HAS_AUDIENCE]->(a:Person) |
            {{nameKor: a.nameKor, nameEng: a.nameEng, nameChi: a.nameChi,
              id: a.id, wikidata: a.idWikidata}}][0..3],
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
    user_language = st.session_state.get("effective_language", "ko")
    retriever = _get_retriever_for_lang(user_language)
    question_answer_chain = create_stuff_documents_chain(llm, _build_prompt())
    plot_retriever = create_retrieval_chain(retriever, question_answer_chain)
    return plot_retriever.invoke({"input": input})
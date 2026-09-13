"""Entry vector-index retrieval_query builders (static Cypher projections).

책임: 언어별 vector 검색이 Neo4jVector에 넘길 `retrieval_query` 텍스트 생성.
두 가지 투영이 있으며 서로 다른 소비자를 가진다:

  * `_build_retrieval_query`        graphRAG / 레거시 ReAct vector tool용 rich
                                    투영 (인물·주제·장소·외부 authority ID 포함)
  * `_build_light_retrieval_query`  textRAG용 경량 투영 (Entry 본문 + 인용 메타)

허용 의존성: chatbot.domain.node_identity (POETRYTALKS_BASE_URL)만.
langchain / neo4j / streamlit을 import하지 않는다.
외부 부작용: 없음.
기존 facade: tools/vector.py, text_rag.py.

Moved verbatim from tools/vector.py and text_rag.py (modularization work order
Phase 5.1). index 설정의 단일 source of truth는 계속 `rag_config`다 — 이
모듈은 text_property 이름만 인자로 받는다.
"""

from __future__ import annotations

from chatbot.domain.node_identity import POETRYTALKS_BASE_URL


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
        entry_id: node.ID,
        entry_position: node.position,
        entry_name_kor: node.nameKor,
        entry_name_chi: node.nameChi,
        entry_name_eng: node.nameEng,
        original_chinese: node.textChi,
        english_translation: node.textEng,
        korean_translation: node.textKor,
        poetrytalks_link: '{POETRYTALKS_BASE_URL}' + node.ID,
        source_work_kor: [(w:Work)-[:HAS_PART]->(node) | w.nameKor][0],
        source_work_eng: [(w:Work)-[:HAS_PART]->(node) | w.nameEng][0],
        source_work_chi: [(w:Work)-[:HAS_PART]->(node) | w.nameChi][0],
        source_work_id: [(w:Work)-[:HAS_PART]->(node) | w.ID][0],
        source_work_desc: [(w:Work)-[:HAS_PART]->(node) | w.descEng][0],
        source_work_mr: [(w:Work)-[:HAS_PART]->(node) | w.nameMR][0],
        creator: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameKor][0],
        creator_eng: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameEng][0],
        creator_chi: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameChi][0],
        creator_mr: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameMR][0],
        creator_py: [(node)-[:HAS_CREATOR]->(p:Person) | p.namePY][0],
        creator_id: [(node)-[:HAS_CREATOR]->(p:Person) | p.ID][0],
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
              nameMR: p.nameMR, namePY: p.namePY,
              id: p.ID,
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
              nameMR: a.nameMR, id: a.ID,
              wikidata: a.idWikidata, aks_digerati: a.idAKSdigerati,
              aks_ency: a.idAKSency, aks_sillok: a.idAKSsillok,
              aks_kdp: a.idAKSkdp, cbdb: a.idCBDB,
              academia_sinica: a.idAcademiaSinica, ency_china: a.idEncyChina,
              nlk: a.idNLK, loc: a.idLOC, bnf: a.idBNF,
              britannica: a.idBritannica, british_museum: a.idBritishMuseum,
              open_library: a.idOpenLibrary, world_history: a.idWorldHistory,
              yale_lux: a.idYaleLux}}][0..3],
        topics: [(node)-[:HAS_SUBJECT_TOPIC]->(t:Topic) |
            {{id: t.ID, nameKor: t.nameKor, nameEng: t.nameEng, nameChi: t.nameChi,
              nameMR: t.nameMR, nameFra: t.nameFra, descEng: t.descEng}}][0..5],
        forms_types: [(node)-[:HAS_TYPE]->(t:Topic) |
            {{id: t.ID, nameKor: t.nameKor, nameEng: t.nameEng, nameChi: t.nameChi,
              nameMR: t.nameMR}}][0..3],
        places: [(node)-[:HAS_SUBJECT_PLACE]->(pl:Place) |
            {{nameKor: pl.nameKor, nameEng: pl.nameEng, nameChi: pl.nameChi,
              nameMR: pl.nameMR,
              id: pl.ID, gis: pl.gis, image: pl.image,
              aks_digerati: pl.idAKSdigerati, aks_map: pl.idAKSmap,
              aks_ency: pl.idAKSency}}][0..3],
        critical_terms: [(node)-[:HAS_SUBJECT_CRITICAL_TERM]->(ct:CriticalTerm) |
            {{id: ct.ID, nameKor: ct.nameKor, nameEng: ct.nameEng, nameChi: ct.nameChi,
              nameMR: ct.nameMR, descEng: ct.descEng}}][0..5],
        era: [(node)-[:HAS_SUBJECT_ERA]->(e:Era) |
            {{id: e.ID, nameKor: e.nameKor, nameEng: e.nameEng, nameMR: e.nameMR,
              yearStart: e.yearStart, yearEnd: e.yearEnd}}][0],
        contained_poems: [(node)-[:HAS_PART]->(pm:Poem) |
            {{id: pm.ID, position: pm.position,
              nameKor: pm.nameKor, nameChi: pm.nameChi, nameEng: pm.nameEng,
              textKor: pm.textKor, textChi: pm.textChi, textEng: pm.textEng}}][0..3],
        contained_critiques: [(node)-[:HAS_PART]->(c:Critique) |
            {{id: c.ID, position: c.position,
              textKor: c.textKor, textChi: c.textChi, textEng: c.textEng}}][0..3]
    }} AS metadata
"""


def _build_light_retrieval_query(text_property: str) -> str:
    """textRAG 전용 가벼운 메타 retrieval_query.
    Entry.id + Entry.position + Entry/Work 이름 + Poetry Talks 링크에 더해,
    한자 원문·번역 병기 인용을 위해 Entry 자신의 세 언어 본문(textChi/textKor/textEng)
    도 metadata에 포함한다. 그래프 관계·인물·주제 확장은 여전히 제외.

    `entry_name_kor`/`entry_name_eng` (work order:
    CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §4 Phase 4 item 1)
    are consumed by `node_references_from_vector_meta`, which already reads
    exactly these two keys for the Entry's own NodeReference — this projection
    is the only piece that was missing."""
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

import streamlit as st
from llm import llm, embeddings
from graph import graph

from langchain_neo4j import Neo4jVector
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.chains import create_retrieval_chain

from langchain_core.prompts import ChatPromptTemplate


instructions = (
  "당신은 시화총림(詩話叢林) 전문가입니다."
    "주어진 context의 시화 자료만을 근거로 답하세요. "
    "context에 없는 내용은 '제공된 자료에 없습니다'라고 답하세요. "

    # Source text rule
    "한문 원문(textChi), 한국어 번역(textKor), 영문 번역(textEng)은 "
    "절대로 번역·요약·변형하지 마세요. 원문 그대로 제시하고, "
    "별도로 해설이나 맥락을 덧붙이세요. "

    # Provenance
    "답변할 때 반드시 출처를 명시하세요: "
    "시화집명(Book.nameKor), 항목 번호(Entry.position), 항목 ID(Entry.id). "
    "예: 파한집(B001) 제3항목(E003). "
    "시(Poem)나 비평(Critique)을 인용할 때는 전체 출처 경로를 제시하세요: "
    "예: 파한집(B001) > 제3항목(E003) > 제2시(M012). "

    # Entity links
    "언급된 모든 개체에 Poetry Talks 링크를 포함하세요: "
    "https://poetrytalks.org/ + node id (예: https://poetrytalks.org/P027). "
    "사용자 언어에 맞는 uselang 파라미터를 추가하세요 "
    "(en / ko / zh / fr). "

    # External data
    "idAKSdigerati 또는 idAKSency가 있는 개체는 외부 API 데이터를 "
    "조회하여 답변에 포함하고 해당 링크도 제공하세요. "

    # Language
    "사용자가 쓰는 언어로 답변하세요. "
    "인물명은 데이터베이스에 저장된 nameEng, nameMR, namePY를 그대로 사용하고 "
    "재로마자화하지 마세요. "
    "일본어 등 한자 사용 언어 사용자에게는 nameChi를 우선 사용하세요."
)

prompt = ChatPromptTemplate.from_messages(
    [
        ("system", instructions + "\n\n참고할 시화 자료(context):\n{context}"),
        ("human", "{input}"),
    ]
)

# 모듈 import 시점이 아닌 실제 호출 시점에 초기화 (Lazy Initialization)
_retriever = None

def _get_retriever():
    global _retriever
    if _retriever is None:
        neo4jvector = Neo4jVector.from_existing_index(
            embeddings,
            graph=graph,
            index_name="EntryTexts",                    # ← 새 인덱스명
            node_label="Entry",                         # ← Book → Entry
            text_node_property="textKor",               # ← Entry의 한국어 본문
            embedding_node_property="textEmbedding",    # ← 새 임베딩 속성
            retrieval_query="""
RETURN
 node.textKor AS text,
    score,
    {
        entry_id: node.id,
        entry_position: node.position,
        original_chinese: node.textChi,
        english_translation: node.textEng,
        source_book_kor: [(b:Book)-[:HAS_PART]->(node) | b.nameKor][0],
        source_book_eng: [(b:Book)-[:HAS_PART]->(node) | b.nameEng][0],
        source_book_id: [(b:Book)-[:HAS_PART]->(node) | b.id][0],
        poetrytalks_link: 'https://poetrytalks.org/' + node.id,
        creator: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameKor][0],
        creator_eng: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameEng][0],
        creator_id: [(node)-[:HAS_CREATOR]->(p:Person) | p.id][0],
        creator_aks_id: [(node)-[:HAS_CREATOR]->(p:Person) | p.idAKSdigerati][0],
        creator_year_birth: [(node)-[:HAS_CREATOR]->(p:Person) | p.yearBirth][0],
        creator_year_death: [(node)-[:HAS_CREATOR]->(p:Person) | p.yearDeath][0],
        creator_era: [(node)-[:HAS_CREATOR]->(p:Person)-[:HAS_ERA]->(e:Era) |
            {nameKor: e.nameKor, nameEng: e.nameEng,
             yearStart: e.yearStart, yearEnd: e.yearEnd}][0],
        mentioned_persons: [(node)-[:HAS_SUBJECT_PERSON]->(p:Person) |
            {nameKor: p.nameKor, nameEng: p.nameEng, id: p.id}][0..5],
        topics: [(node)-[:HAS_SUBJECT_TOPIC]->(t:Topic) |
            {nameKor: t.nameKor, nameEng: t.nameEng}][0..5],
        places: [(node)-[:HAS_SUBJECT_PLACE]->(pl:Place) |
            {nameKor: pl.nameKor, nameEng: pl.nameEng, id: pl.id}][0..3],
        critical_terms: [(node)-[:HAS_SUBJECT_CRITICALTERM]->(ct:CriticalTerm) |
            {nameKor: ct.nameKor, nameEng: ct.nameEng}][0..5],
        era: [(node)-[:HAS_SUBJECT_ERA]->(e:Era) | e.nameKor][0],
        contained_poems: [(node)-[:HAS_PART]->(pm:Poem) |
            {id: pm.id, position: pm.position,
             textKor: pm.textKor, textChi: pm.textChi, textEng: pm.textEng}][0..3],
        contained_critiques: [(node)-[:HAS_PART]->(c:Critique) |
            {id: c.id, position: c.position,
             textKor: c.textKor, textEng: c.textEng}][0..3]
    } AS metadata

"""
        )
        _retriever = neo4jvector.as_retriever()
    return _retriever

def get_poetry_plot(input):
    retriever = _get_retriever()
    question_answer_chain = create_stuff_documents_chain(llm, prompt)
    plot_retriever = create_retrieval_chain(retriever, question_answer_chain)
    return plot_retriever.invoke({"input": input})
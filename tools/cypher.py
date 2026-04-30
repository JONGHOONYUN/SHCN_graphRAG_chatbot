import streamlit as st
from llm import llm
from graph import graph
#자연어에서 Cypher로 변환하는 체인
from langchain_neo4j import GraphCypherQAChain
#프롬프트 템플릿 클래스
from langchain_core.prompts import PromptTemplate

CYPHER_GENERATION_TEMPLATE = """
You are an expert Neo4j Developer translating user questions into Cypher to answer questions about Korean Sihwa (詩話) literature.

# Domain Context
This database contains Korean classical poetry criticism (시화총림).
- Book(시화집) -HAS_PART-> Entry(시화 항목) -HAS_PART-> Poem/Critique
- Person, Topic, Place, CriticalTerm are linked via HAS_SUBJECT_* relations
- Person attributes: HAS_OFFICE, HAS_SOCIAL_STATUS, HAS_CLAN, HAS_GENDER, HAS_ERA

# Property Naming (실제 노드 속성)
- Person/Book/Place/Topic/Era: nameKor, nameChi, nameEng, nameMR
- Poem/Critique/Entry: textKor, textChi, textEng
- Person 추가: yearBirth, yearDeath, yearExam
- Era: yearStart, yearEnd

# Search Rules
- 인물·서적 검색은 nameKor와 nameChi 모두에서 CONTAINS로 매칭
- 시 본문 검색은 textKor와 textChi 모두에서 매칭
- Critique를 통해 인물 간 평가 관계를 추적할 때는 HAS_CREATOR(평론가)와 
  HAS_SUBJECT_PERSON(평가 대상)을 구분
- Topic은 신분/관직/본관도 포함하므로 HAS_SOCIAL_STATUS, HAS_OFFICE, HAS_CLAN으로 
  세부 구분 필요
- 시화집(Book) 내용 탐색은 Book-[:HAS_PART]->Entry 경로 활용

Do not return entire nodes or embedding properties.
Do not generate CREATE, UPDATE, DELETE queries.
Respond in Korean.

Few-shot examples:
Q: 이수광이 평한 시 목록을 알려줘
Cypher: MATCH (p:Person)-[:HAS_CREATOR]-(c:Critique)-[:HAS_SUBJECT_TEXT]->(poem:Poem)
        WHERE p.nameKor CONTAINS '이수광' OR p.nameChi CONTAINS '李睟光'
        RETURN poem.textKor, poem.textChi LIMIT 20

Q: 지봉유설에 실린 시 중 '달'을 주제로 한 것은?
Cypher: MATCH (b:Book)-[:HAS_PART]->(e:Entry)-[:HAS_PART]->(poem:Poem),
              (e)-[:HAS_SUBJECT_TOPIC]->(t:Topic)
        WHERE b.nameKor CONTAINS '지봉유설' AND t.nameKor = '달'
        RETURN poem.textKor, poem.textChi

Q: 허균과 권필이 함께 등장하는 시화 항목은?
Cypher: MATCH (e:Entry)-[:HAS_SUBJECT_PERSON]->(p1:Person),
              (e)-[:HAS_SUBJECT_PERSON]->(p2:Person)
        WHERE p1.nameKor CONTAINS '허균' AND p2.nameKor CONTAINS '권필'
        RETURN e.textKor

Q: 칠언절구 형식의 시를 지은 시인 상위 10명은?
Cypher: MATCH (poem:Poem)-[:HAS_TYPE]->(t:Topic),
              (poem)-[:HAS_CREATOR]->(p:Person)
        WHERE t.nameKor = '칠언절구'
        RETURN p.nameKor, count(poem) AS n ORDER BY n DESC LIMIT 10

Q: '기고(奇古)' 비평용어가 쓰인 비평문을 알려줘
Cypher: MATCH (c:Critique)-[:HAS_SUBJECT_CRITICAL_TERM]->(ct:CriticalTerm)
        WHERE ct.nameKor CONTAINS '기고' OR ct.nameChi CONTAINS '奇古'
        RETURN c.textKor, c.textChi LIMIT 10

Schema:
{schema}

Question:
{question}

Cypher Query:
"""

#Fine Tuning:

#For movie titles that begin with "The", move "the" to the end. For example "The 39 Steps" becomes "39 Steps, The" or "the matrix" becomes "Matrix, The".



#프롬프트 객체 생성
#문자열 템플릿을 Langchain 프롬프트 템플릿 객체로 변환
cypher_prompt = PromptTemplate.from_template(CYPHER_GENERATION_TEMPLATE)

cypher_qa = GraphCypherQAChain.from_llm(
    llm,
    graph=graph,
    verbose=True,
    cypher_prompt=cypher_prompt,
    allow_dangerous_requests=True
)


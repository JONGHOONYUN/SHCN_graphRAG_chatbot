import streamlit as st
from llm import llm
from graph import graph
#자연어에서 Cypher로 변환하는 체인
from langchain_neo4j import GraphCypherQAChain
#프롬프트 템플릿 클래스
from langchain_core.prompts import PromptTemplate

CYPHER_GENERATION_TEMPLATE = """
You are an expert Neo4j Developer translating user questions into Cypher to answer questions about classical Chinese poetry, East Asian humanities and provide recommendations.
Convert the user's question based on the schema.

Use only the provided relationship types and properties in the schema.
Do not use any other relationship types or properties that are not provided.

Do not return entire nodes or embedding properties.

Fine Tuning:

Search using both the chiname and the korname for the Person name.
Search using both the chiname and the korname for the Book name.
Search using both chiname and korname properties for Person and Book nodes.
Respond in Korean.
Do not generate CREATE, UPDATE, DELETE queries.

Few-shot examples:
Question: 이백의 시 목록을 알려줘
Cypher: MATCH (p:Person)-[:isWrittenBy]-(poem:poem)
        WHERE p.korname CONTAINS '이백' OR p.chiname CONTAINS '李白'

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


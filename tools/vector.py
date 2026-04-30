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
    "답변할 때 반드시 출처 시화집명과 관련 인물을 함께 제시하세요. "
    "한문 원문이 있으면 한국어 번역과 함께 보여주세요. "
    "Context에 없는 내용은 '제공된 자료에 없습니다'라고 답하세요. "
    "Context: {context}"
)

prompt = ChatPromptTemplate.from_messages(
    [
        ("system", instructions),
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
            index_name="BookPlots",
            node_label="Book",
            text_node_property="nameKor",
            embedding_node_property="BookplotEmbedding",
            retrieval_query="""
RETURN
    node.nameKor AS text,
    score,
    {
        title_kor: node.nameKor,
        title_chi: node.nameChi,
        author: [(node)-[:HAS_CREATOR]->(p:Person) | p.nameKor],
        entry_count: size([(node)-[:HAS_PART]->(e:Entry) | e]),
        main_topics: [(node)-[:HAS_PART]->(:Entry)-[:HAS_SUBJECT_TOPIC]->(t:Topic) | t.nameKor][0..10],
        featured_persons: [(node)-[:HAS_PART]->(:Entry)-[:HAS_SUBJECT_PERSON]->(p:Person) | p.nameKor][0..10]
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

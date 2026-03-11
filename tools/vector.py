import streamlit as st
from llm import llm, embeddings
from graph import graph

from langchain_neo4j import Neo4jVector
from langchain_classic.chains.combine_documents import create_stuff_documents_chain
from langchain_classic.chains import create_retrieval_chain

from langchain_core.prompts import ChatPromptTemplate


instructions = (
    "Use the given context to answer the question."
    "If you don't know the answer, say you don't know."
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
            index_name="poetryPlots",
            node_label="poem",
            text_node_property="plot",
            embedding_node_property="plotEmbedding",
            retrieval_query="""
RETURN
    node.plot AS text,
    score,
    {
        title: node.name,
        Book: [ (Book)-[:iswrittenBy]->(node) | Book.name],
        BookEdition: [ (BookEdition)-[:isOriginalOf]->(node) | BookEdition.name],
        Person: [ (node)-[:isWrittenBy]->(Person) | Person.name]
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

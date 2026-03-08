import streamlit as st
from llm import llm, embeddings
from graph import graph

from langchain_neo4j import Neo4jVector
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain.chains import create_retrieval_chain

from langchain_core.prompts import ChatPromptTemplate


neo4jvector = Neo4jVector.from_existing_index(
    embeddings,                              # (1)
    graph=graph,                             # (2)
    index_name="poetryPlots",                 # (3)
    node_label="poem",                      # (4)
    text_node_property="plot",               # (5)
    embedding_node_property="plotEmbedding", # (6)
    retrieval_query="""
RETURN
    node.plot AS text,
    score,
    {
        title: node.name,
        Book: [ (Book)-[:iswrittenBy]->(node) | Book.name],
        BookEdition: [ (BookEdition)-[:isOriginalOf]->(node) | [BookEdition.name],
        Person: [ (node)-[:isWrittenBy]->(Person) | Person.name],
    } AS metadata
"""
)

retriever = neo4jvector.as_retriever()

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

question_answer_chain = create_stuff_documents_chain(llm, prompt)
plot_retriever = create_retrieval_chain(
    retriever, 
    question_answer_chain
)

def get_movie_plot(input):
    return plot_retriever.invoke({"input": input})
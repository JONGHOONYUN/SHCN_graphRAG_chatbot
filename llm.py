import streamlit as st

# Create the LLM
from langchain_google_genai import ChatGoogleGenerativeAI

llm = ChatGoogleGenerativeAI(
    google_api_key=st.secrets["GOOGLE_API_KEY"],
    model=st.secrets["GOOGLE_MODEL"],
)


# Create the Embedding model
from langchain_google_genai import GoogleGenerativeAIEmbeddings

embeddings = GoogleGenerativeAIEmbeddings(
    google_api_key=st.secrets["GOOGLE_API_KEY"],
    model="models/embedding-001"
)

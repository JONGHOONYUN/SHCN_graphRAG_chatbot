import streamlit as st

# Create the LLM
from langchain_google_genai import ChatGoogleGenerativeAI

llm = ChatGoogleGenerativeAI(
    google_api_key=st.secrets["GOOGLE_API_KEY"],
    model=st.secrets["GOOGLE_MODEL"],
)


# Create the Embedding model
# 두 SDK 모두 v1beta만 지원하므로 REST API v1 직접 호출
import requests
from langchain_core.embeddings import Embeddings
from typing import List

class GoogleEmbeddings(Embeddings):
    def __init__(self, api_key: str, model: str = "models/text-embedding-004"):
        self.api_key = api_key
        self.model = model
        self.url = f"https://generativelanguage.googleapis.com/v1/{model}:embedContent"

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        response = requests.post(
            self.url,
            params={"key": self.api_key},
            json={
                "model": self.model,
                "content": {"parts": [{"text": text}]}
            }
        )
        response.raise_for_status()
        return response.json()["embedding"]["values"]

embeddings = GoogleEmbeddings(api_key=st.secrets["GOOGLE_API_KEY"])

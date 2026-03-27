import streamlit as st

# ──────────────────────────────────────────────
# LLM 모델 초기화
# ChatGoogleGenerativeAI: LangChain에서 Google Gemini 모델을 사용하기 위한 클래스
# ──────────────────────────────────────────────
from langchain_google_genai import ChatGoogleGenerativeAI

llm = ChatGoogleGenerativeAI(
    google_api_key=st.secrets["GOOGLE_API_KEY"],  # Google AI Studio API 키
    model=st.secrets["GOOGLE_MODEL"],              # 사용할 Gemini 모델명 (secrets.toml에서 관리)
    temperature=0,                                 # 0: 가장 결정론적 응답 → 할루시네이션 최소화
    convert_system_message_to_human=True           # Gemini는 system role 미지원 → human role로 자동 변환
)


# ──────────────────────────────────────────────
# 임베딩 모델 초기화
# langchain_google_genai 2.x / google-generativeai 0.8.x 모두 v1beta API만 사용하여
# text-embedding-004가 404를 반환하는 문제 발생.
# 두 SDK를 우회하여 Google REST API v1 엔드포인트를 직접 호출하는 커스텀 클래스 사용.
# ──────────────────────────────────────────────
import requests
from langchain_core.embeddings import Embeddings  # LangChain 표준 임베딩 인터페이스
from typing import List

# 모델 우선순위 목록: 앞에서부터 순서대로 시도하여 최초 성공한 모델을 사용
# Google이 모델을 폐기하더라도 다음 모델로 자동 전환
EMBEDDING_MODEL_CANDIDATES = [
    "models/gemini-embedding-001",        # 1순위: Gemini 기본 임베딩 모델 (안정 버전)
    "models/gemini-embedding-2-preview",  # 2순위: Gemini 임베딩 2세대 프리뷰 (최신 버전)
]

def _find_available_model(api_key: str) -> str:
    """
    후보 모델 목록을 순서대로 시도하여 실제로 응답하는 모델명을 반환.
    모든 모델이 실패할 경우 예외를 발생시킴.
    """
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    test_payload = {
        "content": {"parts": [{"text": "test"}]}
    }
    for model in EMBEDDING_MODEL_CANDIDATES:
        test_payload["model"] = model
        url = f"{base_url}/{model}:embedContent"
        try:
            resp = requests.post(url, params={"key": api_key}, json=test_payload, timeout=10)
            if resp.status_code == 200:
                return model  # 정상 응답한 첫 번째 모델 반환
        except requests.RequestException:
            continue  # 네트워크 오류 시 다음 모델로 시도
    raise RuntimeError(
        f"사용 가능한 임베딩 모델을 찾을 수 없습니다. 확인된 후보: {EMBEDDING_MODEL_CANDIDATES}\n"
        "Google AI Studio에서 API 키 권한 및 사용 가능한 모델 목록을 확인하세요."
    )


class GoogleEmbeddings(Embeddings):
    """
    Google Generative AI REST API v1을 직접 호출하는 LangChain 호환 임베딩 클래스.
    - embed_query: 단일 텍스트 임베딩 (벡터 검색 질의 시 사용)
    - embed_documents: 다수 텍스트 일괄 임베딩 (Neo4j 인덱스 구축 시 사용)
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"

        # 앱 시작 시 사용 가능한 모델을 자동 탐색하여 고정
        self.model = _find_available_model(api_key)
        self.embed_url = f"{self.base_url}/{self.model}:embedContent"
        self.batch_url = f"{self.base_url}/{self.model}:batchEmbedContents"

    def embed_query(self, text: str) -> List[float]:
        """
        단일 텍스트를 벡터로 변환.
        사용자 질문을 Neo4j 벡터 인덱스 검색에 사용할 수 있는 형태로 변환.
        반환값: float 리스트 (예: 768차원 벡터)
        """
        response = requests.post(
            self.embed_url,
            params={"key": self.api_key},
            json={
                "model": self.model,
                "content": {"parts": [{"text": text}]}
            },
            timeout=30
        )
        response.raise_for_status()
        return response.json()["embedding"]["values"]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        다수의 텍스트를 한 번의 API 호출로 일괄 임베딩.
        batchEmbedContents 엔드포인트 사용으로 순차 호출 대비 속도 대폭 향상.
        Neo4j 벡터 인덱스 생성 또는 갱신 시 사용됨.
        반환값: float 리스트의 리스트 (각 텍스트에 대한 벡터)
        """
        response = requests.post(
            self.batch_url,
            params={"key": self.api_key},
            json={
                # 각 텍스트를 독립 요청으로 구성하여 배열로 전송
                "requests": [
                    {
                        "model": self.model,
                        "content": {"parts": [{"text": text}]}
                    }
                    for text in texts
                ]
            },
            timeout=60  # 대량 문서 처리 시 여유 있는 타임아웃 설정
        )
        response.raise_for_status()
        # 응답에서 각 텍스트의 임베딩 벡터만 추출하여 반환
        return [item["values"] for item in response.json()["embeddings"]]


# 앱 전역에서 사용할 임베딩 인스턴스 생성
# _find_available_model()이 여기서 실행되어 사용 가능한 모델을 자동 탐색
embeddings = GoogleEmbeddings(api_key=st.secrets["GOOGLE_API_KEY"])

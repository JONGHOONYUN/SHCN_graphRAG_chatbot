"""Retrieval request/retriever contracts — the extension seam for new sources.

책임: 현재 실제로 존재하는 정보만 담은 검색 요청 객체와, graph/vector/external
구현이 **이미 구조적으로 만족하는** retriever 프로토콜을 명시한다. 미래 소스를
위한 빈 클래스나 추측 필드는 두지 않는다 (§3.4).

이 모듈은 런타임 계층을 하나 더 만들지 않는다 — 기존 호출 경로는 그대로
위치 인자를 쓰고, 여기 선언은 (a) 확장 지점을 문서화하고 (b) 테스트에서
기존 구현이 계약을 만족하는지 검사하는 데 쓰인다.

허용 의존성: 표준 라이브러리 + chatbot.domain.evidence_models.
외부 부작용: 없음.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from chatbot.domain.evidence_models import Evidence


@dataclass(frozen=True)
class RetrievalRequest:
    """한 턴의 검색 요청. 필드는 현재 파이프라인이 실제로 가지고 있는 값뿐이다.

    `question_language`는 검색 index 선택에, `response_language`는 출력·인용
    순서에 쓰인다 (두 값의 분리는 기존 라우팅 계약 그대로). `history_text`는
    지시어·생략 해석 전용의 bounded 직렬화 이력이며 근거가 아니다."""

    question: str
    question_language: str
    response_language: str
    history_text: Optional[str] = None


@runtime_checkable
class EvidenceRetriever(Protocol):
    """새 근거 소스를 application layer에 등록하기 위한 최소 계약.

    현재 graph/vector 기본 retriever는 `retrieve(question, language,
    history_text)` 형태의 호출 가능 객체이며, 오케스트레이터의 arity dispatch가
    2-인자 레거시 형태도 함께 받아들인다. 새 소스는 같은 형태의 호출 가능
    객체를 넘기기만 하면 되고, 기존 graph/vector 코드를 수정할 필요가 없다."""

    source_key: str

    def retrieve(self, request: RetrievalRequest) -> Evidence:
        ...

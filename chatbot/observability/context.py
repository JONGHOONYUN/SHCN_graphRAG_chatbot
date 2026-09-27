"""Request context — one `RequestContext` per user request, held in a ContextVar.

책임: 요청 식별자(request_id)·mode·route·언어와 요청 단위 누계(LLM 호출 수,
목적별 호출 수, provider token 합계, embedding 호출 수, 폴백 여부, citation 수,
답변 길이, outcome)를 보관한다. 이벤트 발행과 수명주기(시작/종료 이벤트,
callback 설치)는 telemetry.request_scope가 담당한다 — 이 모듈은 저장소일 뿐이다.

동기 호출 경로에는 ContextVar를 쓰므로 공개 함수 시그니처를 바꾸지 않고도
하위 함수가 같은 문맥을 읽는다. Streamlit은 세션마다 별도 스레드에서 스크립트를
실행하므로 스레드 간 누출이 없다. 누계 갱신은 lock으로 보호되어, 문맥 참조를 쥔
callback이 다른 스레드에서 실행돼도 안전하다.

허용 의존성: 표준 라이브러리 + chatbot.observability.events.
"""

from __future__ import annotations

import threading
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Optional

from chatbot.observability.events import (
    LLM_PURPOSES,
    MODE_GRAPHRAG,
    MODE_VECTORRAG,
    OUTCOMES,
    ROUTE_GRAPHRAG,
    ROUTE_VECTORRAG,
    ROUTES,
    normalize_llm_purpose,
    normalize_mode,
    utc_timestamp,
)

_UNSET = object()


def _lang(value) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _nonneg_int(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass
class RequestContext:
    request_id: str
    mode: str = "unknown"
    route: str = "unknown"
    question_language: str = "unknown"
    response_language: str = "unknown"
    started_at: str = ""
    started_perf: float = 0.0
    # ── cumulative counters ──────────────────────────────────────────────
    llm_call_count: int = 0
    llm_calls_by_purpose: dict = field(
        default_factory=lambda: {p: 0 for p in LLM_PURPOSES})
    usage_available_call_count: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    embedding_call_count: int = 0
    fallback_used: bool = False
    citation_count: Optional[int] = None
    answer_chars: Optional[int] = None
    outcome: Optional[str] = None
    exception_type: Optional[str] = None
    error_correlation_id: Optional[str] = None
    completed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock,
                                  repr=False, compare=False)

    @classmethod
    def new(cls, *, mode=None, route=None, question_language=None,
            response_language=None) -> "RequestContext":
        norm_mode = normalize_mode(mode)
        if route in ROUTES:
            norm_route = route
        elif norm_mode == MODE_GRAPHRAG:
            norm_route = ROUTE_GRAPHRAG
        elif norm_mode == MODE_VECTORRAG:
            norm_route = ROUTE_VECTORRAG
        else:
            norm_route = "unknown"
        return cls(
            request_id=uuid.uuid4().hex,
            mode=norm_mode,
            route=norm_route,
            question_language=_lang(question_language),
            response_language=_lang(response_language),
            started_at=utc_timestamp(),
            started_perf=time.perf_counter(),
        )

    # ── common fields for every event emitted inside this request ────────
    def common_fields(self) -> dict:
        return {
            "request_id": self.request_id,
            "mode": self.mode,
            "route": self.route,
            "question_language": self.question_language,
            "response_language": self.response_language,
        }

    # ── updates ──────────────────────────────────────────────────────────
    def update(self, *, route=_UNSET, question_language=_UNSET,
               response_language=_UNSET) -> None:
        with self._lock:
            if route is not _UNSET and route in ROUTES:
                self.route = route
            if question_language is not _UNSET:
                self.question_language = _lang(question_language)
            if response_language is not _UNSET:
                self.response_language = _lang(response_language)

    def record_llm_call(self, purpose, *, input_tokens=None, output_tokens=None,
                        total_tokens=None, usage_available=False) -> None:
        """Count ONE real provider call. Token sums add only values the
        provider actually reported — a call without usage adds nothing."""
        purpose = normalize_llm_purpose(purpose)
        with self._lock:
            self.llm_call_count += 1
            self.llm_calls_by_purpose[purpose] = \
                self.llm_calls_by_purpose.get(purpose, 0) + 1
            if usage_available:
                self.usage_available_call_count += 1
            for attr, value in (("input_tokens", input_tokens),
                                ("output_tokens", output_tokens),
                                ("total_tokens", total_tokens)):
                value = _nonneg_int(value)
                if value is not None:
                    setattr(self, attr, (getattr(self, attr) or 0) + value)

    def record_embedding_call(self) -> None:
        with self._lock:
            self.embedding_call_count += 1

    def mark_fallback(self) -> None:
        with self._lock:
            self.fallback_used = True

    def set_outcome(self, outcome) -> None:
        if outcome in OUTCOMES:
            with self._lock:
                self.outcome = outcome

    def set_answer_stats(self, *, answer_chars=_UNSET,
                         citation_count=_UNSET) -> None:
        with self._lock:
            if answer_chars is not _UNSET:
                self.answer_chars = _nonneg_int(answer_chars)
            if citation_count is not _UNSET:
                self.citation_count = _nonneg_int(citation_count)

    def note_exception(self, exc: BaseException,
                       correlation_id: Optional[str] = None) -> None:
        """Class name only; `correlation_id` links the event to the existing
        server log line for this failure (it is never replaced)."""
        with self._lock:
            self.exception_type = type(exc).__name__
            if isinstance(correlation_id, str):
                self.error_correlation_id = correlation_id

    def snapshot_counters(self) -> dict:
        with self._lock:
            return {
                "llm_call_count": self.llm_call_count,
                "llm_calls_by_purpose": dict(self.llm_calls_by_purpose),
                "usage_available_call_count": self.usage_available_call_count,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
                "embedding_call_count": self.embedding_call_count,
                "fallback_used": self.fallback_used,
                "citation_count": self.citation_count,
                "answer_chars": self.answer_chars,
            }


_CURRENT: ContextVar[Optional[RequestContext]] = ContextVar(
    "chatbot_observability_request", default=None)


def current() -> Optional[RequestContext]:
    return _CURRENT.get()


def set_current(ctx: Optional[RequestContext]) -> Token:
    return _CURRENT.set(ctx)


def reset(token: Token) -> None:
    _CURRENT.reset(token)

"""Public observability facade — the one module application code imports.

책임: emit / span / annotate / note_attempt / Neo4j operation 라벨(재노출)과
요청 수명주기(request_scope)·요청 누계 API. 모든 공개 함수는 예외를 전파하지
않는다(관측 실패 격리). 단, `span`과 `request_scope`는 감싼 코드의 원래 예외를
그대로 재발생시킨다 — 관측이 예외를 삼키거나 바꾸지 않는다.

attempt_count 의미(모든 계측에서 동일): "우리 코드가 직접 수행한 시도 횟수".
재시도가 우리 코드 밖(provider SDK 내부 등)에서 일어나 관찰할 수 없으면 null이다.
시도가 없었음이 확실하면(캐시 hit, 안전 검증 거부) 0이다.

outcome 의미(request.completed):
  success          정상 답변
  short_circuit    모든 검색이 일시 불가하여 LLM 호출 없이 안내문 반환
  error            정상 답변을 만들지 못함 (예외가 전파됐거나, 오류 정책이
                   지역화된 안전 메시지로 변환해 반환한 경우 모두)
  fallback_success ReAct 폴백으로 답변
  fallback_error   폴백까지 실패

구현: emitter.py(sink·발행), spans.py(구간 측정), context.py(문맥 저장소).
허용 의존성: 표준 라이브러리 + chatbot.observability.* (callbacks는 요청 시작
시점에만 지연 로드 — langchain_core가 없는 환경에서도 core는 동작한다).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from chatbot.observability import context as _context
from chatbot.observability import events as ev
from chatbot.observability.context import RequestContext
from chatbot.observability.emitter import (  # noqa: F401  (public facade)
    active_sink,
    configure,
    emit,
    internal_error_counts,
    is_enabled,
    report_internal as _report_internal,
    use_sink,
)
from chatbot.observability.spans import (  # noqa: F401  (public facade)
    NOOP_SPAN,
    Span,
    annotate,
    current_neo4j_operation,
    neo4j_operation,
    note_attempt,
    span,
    start_span,
)


# ── request lifecycle ────────────────────────────────────────────────────────
def current_request() -> Optional[RequestContext]:
    try:
        return _context.current()
    except Exception:  # pragma: no cover - defensive
        return None


def _install_llm_callback(ctx: RequestContext):
    try:
        from chatbot.observability import callbacks
    except Exception:          # langchain_core unavailable — LLM calls unobserved
        return None
    return callbacks.install(ctx)


def _uninstall_llm_callback(token) -> None:
    if token is None:
        return
    from chatbot.observability import callbacks
    callbacks.uninstall(token)


def _final_outcome(ctx: RequestContext) -> str:
    fallback = ctx.fallback_used
    if ctx.exception_type is not None:
        return ev.OUTCOME_FALLBACK_ERROR if fallback else ev.OUTCOME_ERROR
    outcome = ctx.outcome or ev.OUTCOME_SUCCESS
    if fallback and outcome == ev.OUTCOME_SUCCESS:
        return ev.OUTCOME_FALLBACK_SUCCESS
    if fallback and outcome == ev.OUTCOME_ERROR:
        return ev.OUTCOME_FALLBACK_ERROR
    return outcome


_OUTCOME_STATUS = {
    ev.OUTCOME_SUCCESS: ev.STATUS_SUCCESS,
    ev.OUTCOME_FALLBACK_SUCCESS: ev.STATUS_SUCCESS,
    ev.OUTCOME_ERROR: ev.STATUS_ERROR,
    ev.OUTCOME_FALLBACK_ERROR: ev.STATUS_ERROR,
    ev.OUTCOME_SHORT_CIRCUIT: ev.STATUS_ERROR,
}


def _complete_request(ctx: RequestContext) -> None:
    if ctx.completed:
        return
    ctx.completed = True
    outcome = _final_outcome(ctx)
    fields = ctx.snapshot_counters()
    fields.update({
        "outcome": outcome,
        "status": _OUTCOME_STATUS[outcome],
        "started_at": ctx.started_at,
        "duration_ms": (time.perf_counter() - ctx.started_perf) * 1000.0,
    })
    if ctx.exception_type is not None:
        fields["error_type"] = ctx.exception_type
    if ctx.error_correlation_id is not None:
        fields["correlation_id"] = ctx.error_correlation_id
    emit(ev.REQUEST_COMPLETED, _ctx=ctx, **fields)


@contextmanager
def request_scope(*, mode: Any = None, route: Optional[str] = None,
                  question_language: Optional[str] = None,
                  response_language: Optional[str] = None
                  ) -> Iterator[RequestContext]:
    """Root request context. Emits `request.started` and exactly one
    `request.completed` — including when the body raises (the exception is
    re-raised unchanged).

    Nested use is ownership-aware: if a request is already active (e.g. bot.py
    opened it and agent.py opens it again), the existing context is yielded
    untouched — no second start/complete event, no early reset."""
    existing = current_request()
    if existing is not None:
        yield existing
        return

    ctx: Optional[RequestContext] = None
    token = None
    callback_token = None
    try:
        ctx = RequestContext.new(mode=mode, route=route,
                                 question_language=question_language,
                                 response_language=response_language)
        token = _context.set_current(ctx)
        if is_enabled():
            callback_token = _install_llm_callback(ctx)
        emit(ev.REQUEST_STARTED, _ctx=ctx, started_at=ctx.started_at)
    except Exception as exc:
        _report_internal("context", exc)

    if ctx is None:
        # Context creation failed: run the request unobserved, never blocked.
        yield RequestContext(request_id="0" * 32)
        return

    try:
        yield ctx
    except BaseException as exc:
        try:
            ctx.note_exception(exc)
        except Exception as internal:  # pragma: no cover - defensive
            _report_internal("context", internal)
        raise
    finally:
        try:
            _complete_request(ctx)
        except Exception as exc:  # pragma: no cover - defensive
            _report_internal("context", exc)
        try:
            _uninstall_llm_callback(callback_token)
        except Exception as exc:  # pragma: no cover - defensive
            _report_internal("callback", exc)
        try:
            if token is not None:
                _context.reset(token)
        except Exception as exc:  # pragma: no cover - defensive
            _report_internal("context", exc)


def _with_ctx(fn) -> None:
    try:
        ctx = _context.current()
        if ctx is not None:
            fn(ctx)
    except Exception as exc:  # pragma: no cover - defensive
        _report_internal("context", exc)


def update_request(**fields: Any) -> None:
    """Supplement route / languages once they become known."""
    _with_ctx(lambda ctx: ctx.update(**fields))


def mark_fallback() -> None:
    _with_ctx(lambda ctx: ctx.mark_fallback())


def set_outcome(outcome: str) -> None:
    _with_ctx(lambda ctx: ctx.set_outcome(outcome))


def record_request_error(exc: BaseException,
                         correlation_id: Optional[str] = None) -> None:
    """A backend failure that the caller HANDLED (e.g. bot.py's catch-all that
    shows a safe message): outcome becomes error/fallback_error and the
    existing log correlation id is linked. The exception is not re-raised."""
    _with_ctx(lambda ctx: ctx.note_exception(exc, correlation_id))


def set_answer_stats(**fields: Any) -> None:
    _with_ctx(lambda ctx: ctx.set_answer_stats(**fields))


def count_embedding_call() -> None:
    _with_ctx(lambda ctx: ctx.record_embedding_call())

"""Timed spans, span annotation, attempt counting, and Neo4j operation labels.

책임: `perf_counter()` 기반 구간 측정과 `<name>` 완료 이벤트 1건 발행. span은
감싼 코드의 예외를 삼키거나 바꾸지 않는다 — 예외가 빠져나가면 `status=error`와
`error_type`(클래스 이름만)을 기록한 뒤 그대로 재발생시킨다.

  * annotate(**f)     가장 안쪽 열린 span에 필드를 더한다 (자체 이벤트 없음).
  * note_attempt()    가장 안쪽 attempt-counting span의 시도 수를 1 올린다.
  * neo4j_operation   호출자가 Neo4j 호출의 operation/origin을 라벨링한다 —
                      DB 경계는 query 문자열을 보지 않고 이 라벨만 읽는다.

비활성 상태에서는 공유 no-op span을 돌려준다.

허용 의존성: 표준 라이브러리 + chatbot.observability.{events, context, emitter}.
공개 진입점: chatbot.observability.telemetry.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from chatbot.observability import context as _context
from chatbot.observability import events as ev
from chatbot.observability.context import RequestContext
from chatbot.observability.emitter import emit, is_enabled, report_internal

_current_span: ContextVar[Optional["Span"]] = ContextVar(
    "chatbot_observability_span", default=None)
_attempt_span: ContextVar[Optional["Span"]] = ContextVar(
    "chatbot_observability_attempt_span", default=None)


class Span:
    """A timed operation that emits exactly one `<name>` event when finished.

    Usable as a context manager (`with span(...) as sp:`) or manually
    (`sp = start_span(...); ...; sp.finish()`). Status defaults to `success`;
    an exception escaping the `with` block records `status=error` (unless the
    caller already set a status) and `error_type`, then propagates unchanged."""

    __slots__ = ("_name", "_fields", "_t0", "_count_attempts", "_attempts",
                 "_attempts_explicit", "_status_explicit", "_finished", "_ctx",
                 "_tokens")

    def __init__(self, name: str, count_attempts: bool, fields: dict,
                 ctx: Optional[RequestContext]) -> None:
        self._name = name
        self._fields = dict(fields)
        self._status_explicit = "status" in fields
        self._t0 = time.perf_counter()
        self._count_attempts = count_attempts
        self._attempts_explicit = "attempt_count" in self._fields
        self._attempts: Optional[int] = self._fields.pop("attempt_count", None)
        self._finished = False
        self._ctx = ctx
        self._tokens: tuple = ()

    def set(self, **fields: Any) -> "Span":
        try:
            if "status" in fields:
                self._status_explicit = True
            if "attempt_count" in fields:
                self._attempts = fields.pop("attempt_count")
                self._attempts_explicit = True
            self._fields.update(fields)
        except Exception as exc:  # pragma: no cover - defensive
            report_internal("span", exc)
        return self

    def note_attempt(self) -> None:
        self._attempts = (self._attempts or 0) + 1

    def finish(self, **fields: Any) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            if fields:
                self.set(**fields)
            payload = dict(self._fields)
            payload.setdefault("status", ev.STATUS_SUCCESS)
            payload["duration_ms"] = (time.perf_counter() - self._t0) * 1000.0
            if (self._count_attempts or self._attempts_explicit
                    or self._attempts is not None):
                payload["attempt_count"] = self._attempts
            emit(self._name, _ctx=self._ctx, **payload)
        except Exception as exc:  # pragma: no cover - defensive
            report_internal("span", exc)

    def fail(self, exc: BaseException) -> None:
        try:
            if not self._status_explicit:
                self._fields["status"] = ev.STATUS_ERROR
            self._fields.setdefault("error_type", type(exc).__name__)
        except Exception as internal:  # pragma: no cover - defensive
            report_internal("span", internal)
        self.finish()

    def __enter__(self) -> "Span":
        try:
            tokens = [(_current_span, _current_span.set(self))]
            if self._count_attempts:
                tokens.append((_attempt_span, _attempt_span.set(self)))
            self._tokens = tuple(tokens)
        except Exception as exc:  # pragma: no cover - defensive
            report_internal("span", exc)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            for var, token in reversed(self._tokens):
                var.reset(token)
        except Exception as internal:  # pragma: no cover - defensive
            report_internal("span", internal)
        if exc is not None:
            self.fail(exc)
        else:
            self.finish()
        return False          # never swallow the caller's exception


class _NoopSpan:
    """Shared do-nothing span used while observability is disabled."""

    __slots__ = ()

    def set(self, **fields: Any) -> "_NoopSpan":
        return self

    def note_attempt(self) -> None:
        return None

    def finish(self, **fields: Any) -> None:
        return None

    def fail(self, exc: BaseException) -> None:
        return None

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


NOOP_SPAN = _NoopSpan()


def start_span(name: str, *, count_attempts: bool = False, **fields: Any):
    """Start a span WITHOUT making it the current span (manual `finish()`)."""
    if not is_enabled():
        return NOOP_SPAN
    try:
        return Span(name, count_attempts, fields, _context.current())
    except Exception as exc:  # pragma: no cover - defensive
        report_internal("span", exc)
        return NOOP_SPAN


def span(name: str, *, count_attempts: bool = False, **fields: Any):
    """Context-manager span; while open it is the target of `annotate()` and,
    with `count_attempts=True`, of `note_attempt()`."""
    return start_span(name, count_attempts=count_attempts, **fields)


def annotate(**fields: Any) -> None:
    """Set fields on the innermost open span (no event of its own)."""
    try:
        current = _current_span.get()
        if current is not None:
            current.set(**fields)
    except Exception as exc:  # pragma: no cover - defensive
        report_internal("span", exc)


def note_attempt() -> None:
    """Record one attempt on the innermost attempt-counting span."""
    try:
        target = _attempt_span.get()
        if target is not None:
            target.note_attempt()
    except Exception as exc:  # pragma: no cover - defensive
        report_internal("span", exc)


_neo4j_operation: ContextVar[tuple] = ContextVar(
    "chatbot_observability_neo4j_operation",
    default=(ev.NEO4J_OTHER, ev.ORIGIN_UNKNOWN))


@contextmanager
def neo4j_operation(operation: str, origin: str) -> Iterator[None]:
    """Label the Neo4j calls made inside this block. The query string itself
    is never inspected to infer the operation."""
    token = _neo4j_operation.set((operation, origin))
    try:
        yield
    finally:
        _neo4j_operation.reset(token)


def current_neo4j_operation() -> tuple:
    return _neo4j_operation.get()

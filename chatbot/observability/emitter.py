"""Sink configuration, event delivery, and internal-failure reporting.

책임: 프로세스 전역 sink 선택(환경변수 또는 명시 주입), 문맥 단위 sink override,
이벤트 검증·전달, 관측 내부 오류의 제한적 보고. 모든 함수는 예외를 전파하지
않는다. 관측 내부 오류는 sink를 거치지 않고 전용 내부 logger로만 남기며
(재귀 방지), 같은 종류는 최대 5회까지만 경고한다.

설정(환경변수 — Streamlit을 import하지 않으므로 st.secrets는 읽지 않는다):
    CHATBOT_OBSERVABILITY            log(기본) | off
    CHATBOT_OBSERVABILITY_LOG_LEVEL  INFO(기본) | DEBUG | WARNING ...
    CHATBOT_OBSERVABILITY_CONSOLE    1 이면 이벤트 logger에만 stderr handler를
                                     붙인다 (root logger는 건드리지 않음)

허용 의존성: 표준 라이브러리 + chatbot.observability.{events, sinks, context}.
공개 진입점: chatbot.observability.telemetry.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from chatbot.observability import context as _context
from chatbot.observability import events as ev
from chatbot.observability.context import RequestContext
from chatbot.observability.sinks import (
    EVENTS_LOGGER_NAME,
    EventSink,
    LoggingSink,
    NullSink,
)

_internal_logger = logging.getLogger("chatbot.observability.internal")


# ── sink configuration ───────────────────────────────────────────────────────
class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sink: Optional[EventSink] = None     # None = not configured yet


_state = _State()
_sink_override: ContextVar[Optional[EventSink]] = ContextVar(
    "chatbot_observability_sink_override", default=None)


def _sink_from_env() -> EventSink:
    mode = (os.environ.get("CHATBOT_OBSERVABILITY") or "log").strip().lower()
    if mode in ("off", "none", "null", "0", "false", "disabled"):
        return NullSink()
    level_name = (os.environ.get("CHATBOT_OBSERVABILITY_LOG_LEVEL") or "INFO")
    level = logging.getLevelName(level_name.strip().upper())
    if not isinstance(level, int):
        level = logging.INFO
    console = (os.environ.get("CHATBOT_OBSERVABILITY_CONSOLE") or "").strip().lower()
    if console in ("1", "true", "yes"):
        _attach_console_handler(level)
    return LoggingSink(level=level)


def _attach_console_handler(level: int) -> None:
    """Opt-in local viewing: a stderr handler on the EVENTS logger only."""
    events_logger = logging.getLogger(EVENTS_LOGGER_NAME)
    if events_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    events_logger.addHandler(handler)
    events_logger.setLevel(level)
    events_logger.propagate = False


def configure(*, sink: Optional[EventSink] = None, enabled: Optional[bool] = None,
              from_env: bool = False) -> None:
    """Set the process-wide sink. `enabled=False` selects NullSink."""
    try:
        with _state.lock:
            if from_env:
                _state.sink = _sink_from_env()
            elif enabled is False:
                _state.sink = NullSink()
            elif sink is not None:
                _state.sink = sink
            elif enabled is True:
                _state.sink = LoggingSink()
    except Exception as exc:  # pragma: no cover - defensive
        report_internal("sink", exc)


def active_sink() -> EventSink:
    override = _sink_override.get()
    if override is not None:
        return override
    sink = _state.sink
    if sink is None:
        with _state.lock:
            if _state.sink is None:
                try:
                    _state.sink = _sink_from_env()
                except Exception:
                    _state.sink = NullSink()
            sink = _state.sink
    return sink


def is_enabled() -> bool:
    try:
        return not isinstance(active_sink(), NullSink)
    except Exception:  # pragma: no cover - defensive
        return False


@contextmanager
def use_sink(sink: EventSink) -> Iterator[EventSink]:
    """Context-local sink override (tests, dry runs). Thread/async safe."""
    token = _sink_override.set(sink)
    try:
        yield sink
    finally:
        _sink_override.reset(token)


# ── internal-failure reporting (never through the sink → no recursion) ─────
_internal_guard = threading.local()
_internal_counts: dict = {}
_INTERNAL_WARNING_LIMIT = 5


def report_internal(component: str, exc: BaseException) -> None:
    if getattr(_internal_guard, "active", False):
        return
    _internal_guard.active = True
    try:
        key = (component, type(exc).__name__)
        count = _internal_counts.get(key, 0) + 1
        _internal_counts[key] = count
        if count <= _INTERNAL_WARNING_LIMIT:
            event = ev.build_event(ev.OBSERVABILITY_ERROR, {
                "component": component, "error_type": type(exc).__name__,
                "status": ev.STATUS_ERROR})
            _internal_logger.warning(ev.serialize(event) if event else
                                     "observability.error")
    except Exception:
        pass
    finally:
        _internal_guard.active = False


def internal_error_counts() -> dict:
    """Test/ops helper: how many internal observability failures occurred."""
    return dict(_internal_counts)


# ── emit ─────────────────────────────────────────────────────────────────────
def _common_for(ctx: Optional[RequestContext]) -> dict:
    if ctx is None:
        return {"request_id": None, "mode": "unknown", "route": "unknown"}
    return ctx.common_fields()


def emit(event_name: str, _ctx: Optional[RequestContext] = None,
         **fields: Any) -> None:
    """Build, validate and deliver one event. Never raises."""
    try:
        sink = active_sink()
        if isinstance(sink, NullSink):
            return
        ctx = _ctx if _ctx is not None else _context.current()
        event = ev.build_event(event_name, fields, _common_for(ctx))
        if event is None:
            raise ValueError("unknown observability event name")
    except Exception as exc:
        report_internal("event", exc)
        return
    try:
        sink.emit(event)
    except Exception as exc:
        report_internal("sink", exc)

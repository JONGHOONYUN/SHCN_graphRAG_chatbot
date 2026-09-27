"""Event sinks — where validated events go.

`EventSink`는 `emit(event)` 하나만 가진 작은 protocol이다. 특정 관측 플랫폼
SDK에 결합하지 않으며, 표준 `logging`만 사용한다.

  * NullSink     — 아무것도 하지 않는다 (비활성 상태의 기본값).
  * LoggingSink  — 한 줄 JSON을 전용 logger로 출력한다. handler를 스스로
                   설치하지 않고 root logger를 건드리지 않는다. logger가 해당
                   레벨을 받지 않으면 직렬화 비용조차 들이지 않는다.
  * MemorySink   — 테스트용. 이벤트 순서를 보존한다.

허용 의존성: 표준 라이브러리 + chatbot.observability.events.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable, List, Mapping, Protocol, runtime_checkable

from chatbot.observability.events import serialize

EVENTS_LOGGER_NAME = "chatbot.observability.events"


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: Mapping[str, object]) -> None:
        ...


class NullSink:
    """Discards every event. Selected when observability is disabled."""

    def emit(self, event: Mapping[str, object]) -> None:
        return None


class LoggingSink:
    """Emits each event as one JSON line on a dedicated logger.

    The sink never configures handlers or levels: whether the line is shown is
    decided entirely by the application's logging configuration (see
    docs/OBSERVABILITY.md for the opt-in console handler)."""

    def __init__(self, logger_name: str = EVENTS_LOGGER_NAME,
                 level: int = logging.INFO) -> None:
        self._logger = logging.getLogger(logger_name)
        self._level = level

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    @property
    def level(self) -> int:
        return self._level

    def emit(self, event: Mapping[str, object]) -> None:
        if not self._logger.isEnabledFor(self._level):
            return
        self._logger.log(self._level, serialize(event))


class MemorySink:
    """In-memory, order-preserving sink for tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: List[dict] = []

    def emit(self, event: Mapping[str, object]) -> None:
        with self._lock:
            self._events.append(dict(event))

    @property
    def events(self) -> List[dict]:
        with self._lock:
            return list(self._events)

    def names(self) -> List[str]:
        return [e["event_name"] for e in self.events]

    def of(self, event_name: str) -> List[dict]:
        return [e for e in self.events if e["event_name"] == event_name]

    def where(self, event_name: str, **match: Any) -> List[dict]:
        return [e for e in self.of(event_name)
                if all(e.get(k) == v for k, v in match.items())]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def extend(self, events: Iterable[Mapping[str, object]]) -> None:
        for event in events:
            self.emit(event)

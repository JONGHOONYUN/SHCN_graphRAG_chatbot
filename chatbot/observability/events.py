"""Event schema — names, allowed fields, bounded enums, validation, JSON.

이 모듈이 관측 이벤트의 **유일한 스키마 계약**이다. 이벤트는 여기 선언된
필드만 가질 수 있고, 각 필드는 타입·범위·enum 검증을 통과해야 한다. 선언되지
않은 키나 검증에 실패한 값은 조용히 생략된다 — 임의 객체를 `str()`/`repr()`로
직렬화하지 않고, 원문(질문·prompt·Cypher·evidence·URL·응답 body)을 담을 수
있는 필드는 스키마에 아예 존재하지 않는다.

허용 의존성: 표준 라이브러리만.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

SCHEMA_VERSION = 1

# ── Event names (stable public observability contract) ──────────────────────
REQUEST_STARTED = "request.started"
REQUEST_COMPLETED = "request.completed"

HISTORY_READ = "history.read.completed"
EVIDENCE_GATHER = "evidence.gather.completed"
EVIDENCE_FORMAT = "evidence.format.completed"
SYNTHESIS = "synthesis.completed"
CITATIONS_BUILD = "citations.build.completed"
ANSWER_RENDER = "answer.render.completed"
HISTORY_WRITE = "history.write.completed"
PIPELINE_GRAPHRAG = "pipeline.graphrag.completed"
PIPELINE_VECTORRAG = "pipeline.vectorrag.completed"
FALLBACK = "fallback.completed"

RETRIEVAL_GRAPH = "retrieval.graph.completed"
RETRIEVAL_VECTOR = "retrieval.vector.completed"
RETRIEVAL_AUTHORITY = "retrieval.authority.completed"
RETRIEVAL_AUTHORITY_SOURCE = "retrieval.authority_source.completed"
AUTHORITY_HTTP = "authority.http.completed"

NEO4J_QUERY = "neo4j.query.completed"
EMBEDDING = "embedding.completed"
LLM = "llm.completed"

OBSERVABILITY_ERROR = "observability.error"

EVENT_NAMES = frozenset({
    REQUEST_STARTED, REQUEST_COMPLETED,
    HISTORY_READ, EVIDENCE_GATHER, EVIDENCE_FORMAT, SYNTHESIS, CITATIONS_BUILD,
    ANSWER_RENDER, HISTORY_WRITE, PIPELINE_GRAPHRAG, PIPELINE_VECTORRAG, FALLBACK,
    RETRIEVAL_GRAPH, RETRIEVAL_VECTOR, RETRIEVAL_AUTHORITY,
    RETRIEVAL_AUTHORITY_SOURCE, AUTHORITY_HTTP,
    NEO4J_QUERY, EMBEDDING, LLM,
    OBSERVABILITY_ERROR,
})

# ── Bounded enums (low-cardinality, aggregation-safe) ────────────────────────
MODE_GRAPHRAG = "graphrag"
MODE_VECTORRAG = "vectorrag"
MODES = (MODE_GRAPHRAG, MODE_VECTORRAG, "unknown")

ROUTE_GRAPHRAG = "graphrag"
ROUTE_VECTORRAG = "vectorrag"
ROUTE_GENERAL_CHAT = "general_chat"
ROUTE_REACT_FALLBACK = "react_fallback"
ROUTES = (ROUTE_GRAPHRAG, ROUTE_VECTORRAG, ROUTE_GENERAL_CHAT,
          ROUTE_REACT_FALLBACK, "unknown")

STATUS_SUCCESS = "success"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_SUCCESS, STATUS_EMPTY, STATUS_ERROR, STATUS_SKIPPED)

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_SHORT_CIRCUIT = "short_circuit"
OUTCOME_FALLBACK_SUCCESS = "fallback_success"
OUTCOME_FALLBACK_ERROR = "fallback_error"
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_ERROR, OUTCOME_SHORT_CIRCUIT,
            OUTCOME_FALLBACK_SUCCESS, OUTCOME_FALLBACK_ERROR)

LLM_CYPHER_GENERATION = "cypher_generation"
LLM_GRAPH_QA = "graph_qa"
LLM_FINAL_SYNTHESIS = "final_synthesis"
LLM_TEXT_RAG_ANSWER = "text_rag_answer"
LLM_GENERAL_CHAT = "general_chat"
LLM_REACT_ITERATION = "react_iteration"
LLM_LEGACY_VECTOR_ANSWER = "legacy_vector_answer"
LLM_LANGUAGE_DETECTION = "language_detection"
LLM_OTHER = "other"
LLM_PURPOSES = (
    LLM_CYPHER_GENERATION, LLM_GRAPH_QA, LLM_FINAL_SYNTHESIS,
    LLM_TEXT_RAG_ANSWER, LLM_GENERAL_CHAT, LLM_REACT_ITERATION,
    LLM_LEGACY_VECTOR_ANSWER, LLM_LANGUAGE_DETECTION, LLM_OTHER,
)

EMBEDDING_VECTOR_QUERY = "vector_query"
EMBEDDING_DOCUMENT_INDEXING = "document_indexing"
EMBEDDING_PURPOSES = (EMBEDDING_VECTOR_QUERY, EMBEDDING_DOCUMENT_INDEXING, "other")

NEO4J_GENERATED_GRAPH_QUERY = "generated_graph_query"
NEO4J_VECTOR_QUERY = "vector_query"
NEO4J_HISTORY_READ = "history_read"
NEO4J_HISTORY_WRITE = "history_write"
NEO4J_DETERMINISTIC_LOOKUP = "deterministic_lookup"
NEO4J_OTHER = "other"
NEO4J_OPERATIONS = (NEO4J_GENERATED_GRAPH_QUERY, NEO4J_VECTOR_QUERY,
                    NEO4J_HISTORY_READ, NEO4J_HISTORY_WRITE,
                    NEO4J_DETERMINISTIC_LOOKUP, NEO4J_OTHER)

ORIGIN_GENERATED = "generated"
ORIGIN_DETERMINISTIC = "deterministic"
ORIGIN_FRAMEWORK = "framework"
ORIGIN_UNKNOWN = "unknown"
QUERY_ORIGINS = (ORIGIN_GENERATED, ORIGIN_DETERMINISTIC, ORIGIN_FRAMEWORK,
                 ORIGIN_UNKNOWN)

SAFETY_PASSED = "passed"
SAFETY_REJECTED = "rejected"
SAFETY_NOT_APPLICABLE = "not_applicable"
SAFETY_UNKNOWN = "unknown"
SAFETY_RESULTS = (SAFETY_PASSED, SAFETY_REJECTED, SAFETY_NOT_APPLICABLE,
                  SAFETY_UNKNOWN)

RETRIEVER_GRAPH = "graph"
RETRIEVER_ROLE_RANKING = "role_ranking"
RETRIEVER_VECTOR = "vector"
RETRIEVER_AUTHORITY = "authority"
RETRIEVERS = (RETRIEVER_GRAPH, RETRIEVER_ROLE_RANKING, RETRIEVER_VECTOR,
              RETRIEVER_AUTHORITY)

COMPONENTS = ("sink", "callback", "context", "span", "usage", "event")

# Field NAMES that must never appear in any event — raw content or secrets.
# Kept here so the privacy test and the allowlist share one definition.
FORBIDDEN_FIELD_NAMES = frozenset({
    "question", "prompt", "prompts", "messages", "message", "answer", "body",
    "cypher", "query", "query_text", "evidence", "evidence_text", "text",
    "content", "response_body", "response", "url", "api_key", "password",
    "token", "access_token", "cookie", "embedding_vector", "vector",
    "session_id", "session_state", "history", "chat_history", "external_id",
    "node_id", "error_message", "stack_trace", "traceback",
})

# ── Validators ───────────────────────────────────────────────────────────────
_DROP = object()   # sentinel: omit the field entirely

_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
_CLASS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_NODE_TYPE_RE = re.compile(r"^[A-Z][A-Za-z]{0,30}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_STATUS_CLASS_RE = re.compile(r"^[1-5]xx$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _enum(allowed, default=_DROP) -> Callable:
    allowed = tuple(allowed)

    def _v(value, _event):
        return value if isinstance(value, str) and value in allowed else default
    return _v


def _count(value, _event):
    """Non-negative int, or explicit null ("unknown" is not a false 0)."""
    if value is None:
        return None
    return value if _is_int(value) and value >= 0 else _DROP


def _required_count(value, _event):
    return value if _is_int(value) and value >= 0 else _DROP


def _duration(value, _event):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DROP
    if not math.isfinite(value) or value < 0:
        return _DROP
    return round(float(value), 3)


def _bool(value, _event):
    return value if isinstance(value, bool) else _DROP


def _opt_bool(value, _event):
    if value is None:
        return None
    return value if isinstance(value, bool) else _DROP


def _pattern(regex, default=_DROP, allow_none=False) -> Callable:
    def _v(value, _event):
        if value is None and allow_none:
            return None
        if isinstance(value, str) and regex.match(value):
            return value
        return default
    return _v


def _language(value, _event):
    if isinstance(value, str) and _LANG_RE.match(value):
        return value
    return "unknown"


def _purpose(value, event_name):
    if event_name == EMBEDDING:
        return value if value in EMBEDDING_PURPOSES else "other"
    return value if value in LLM_PURPOSES else LLM_OTHER


def _http_status_code(value, _event):
    return value if _is_int(value) and 100 <= value <= 599 else _DROP


def _purpose_counts(value, _event):
    if not isinstance(value, Mapping):
        return _DROP
    out = {}
    for purpose in LLM_PURPOSES:
        count = value.get(purpose, 0)
        out[purpose] = count if _is_int(count) and count >= 0 else 0
    return out


def _internal_str(value, _event):
    return value if isinstance(value, str) else _DROP


FIELD_VALIDATORS = {
    # envelope (set by build_event itself)
    "schema_version": lambda v, _e: v if v == SCHEMA_VERSION else _DROP,
    "timestamp": _pattern(_ISO_RE),
    "event_name": _internal_str,
    # common request fields
    "request_id": _pattern(_HEX_ID_RE, default=None, allow_none=True),
    "mode": _enum(MODES, default="unknown"),
    "route": _enum(ROUTES, default="unknown"),
    "question_language": _language,
    "response_language": _language,
    "status": _enum(STATUSES),
    "duration_ms": _duration,
    # request summary
    "started_at": _pattern(_ISO_RE),
    "outcome": _enum(OUTCOMES),
    "llm_call_count": _required_count,
    "llm_calls_by_purpose": _purpose_counts,
    "usage_available_call_count": _required_count,
    "embedding_call_count": _required_count,
    "fallback_used": _bool,
    "citation_count": _count,
    "answer_chars": _count,
    # pipeline spans
    "history_message_count": _count,
    "evidence_graph_count": _count,
    "evidence_vector_count": _count,
    "evidence_external_count": _count,
    "evidence_chars": _count,
    "synthesis_executed": _bool,
    "short_circuit": _bool,
    # fallback
    "trigger_error_type": _pattern(_CLASS_NAME_RE),
    # retrieval
    "retriever": _enum(RETRIEVERS),
    "result_count": _count,
    "attempt_count": _count,
    # neo4j
    "operation": _enum(NEO4J_OPERATIONS, default=NEO4J_OTHER),
    "query_origin": _enum(QUERY_ORIGINS, default=ORIGIN_UNKNOWN),
    "row_count": _count,
    "safety_result": _enum(SAFETY_RESULTS, default=SAFETY_UNKNOWN),
    # embedding / llm
    "purpose": _purpose,
    "provider": _pattern(_SLUG_RE, default="unknown"),
    "model": _pattern(_SLUG_RE, default="unknown"),
    "input_count": _count,
    "input_chars": _count,
    "output_chars": _count,
    "dimension": _count,
    "usage_available": _bool,
    "input_tokens": _count,
    "output_tokens": _count,
    "total_tokens": _count,
    # authority
    "source": _pattern(_SOURCE_RE, default="unknown"),
    "node_type": _pattern(_NODE_TYPE_RE, default="unknown"),
    "cache_hit": _opt_bool,
    "http_fetched": _opt_bool,
    "http_status_code": _http_status_code,
    "http_status_class": _pattern(_STATUS_CLASS_RE),
    "response_bytes": _count,
    # errors
    "error_type": _pattern(_CLASS_NAME_RE),
    "correlation_id": _pattern(_CORRELATION_RE),
    "component": _enum(COMPONENTS, default="event"),
}

ALLOWED_FIELDS = frozenset(FIELD_VALIDATORS)

assert not (ALLOWED_FIELDS & FORBIDDEN_FIELD_NAMES), \
    "observability schema must never declare a raw-content field"


# ── Construction / serialization ─────────────────────────────────────────────
def utc_timestamp() -> str:
    """Timezone-aware UTC ISO-8601 with millisecond precision and a `Z`."""
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def http_status_class(code: Any) -> Optional[str]:
    if _is_int(code) and 100 <= code <= 599:
        return f"{code // 100}xx"
    return None


def build_event(event_name: str, fields: Mapping[str, Any],
                common: Optional[Mapping[str, Any]] = None) -> Optional[dict]:
    """Return a validated, JSON-safe event dict, or None for an unknown name.

    `common` supplies request-scoped fields (request_id/mode/route/languages);
    caller `fields` cannot override `request_id` so an event can never be
    re-attributed to another request."""
    if event_name not in EVENT_NAMES:
        return None
    event = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": utc_timestamp(),
        "event_name": event_name,
    }
    merged = dict(common or {})
    for key, value in fields.items():
        if key == "request_id" and "request_id" in merged:
            continue
        merged[key] = value
    for key, value in merged.items():
        validator = FIELD_VALIDATORS.get(key)
        if validator is None or key in event:
            continue
        clean = validator(value, event_name)
        if clean is _DROP:
            continue
        event[key] = clean
    return event


def serialize(event: Mapping[str, Any]) -> str:
    """One-line, deterministic JSON. Never falls back to `repr()`."""
    return json.dumps(event, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def normalize_mode(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    lowered = value.strip().lower()
    if lowered in ("graphrag", "graph"):
        return MODE_GRAPHRAG
    if lowered in ("vectorrag", "textrag", "text_rag", "vector"):
        return MODE_VECTORRAG
    return "unknown"


def normalize_llm_purpose(value: Any) -> str:
    return value if isinstance(value, str) and value in LLM_PURPOSES else LLM_OTHER

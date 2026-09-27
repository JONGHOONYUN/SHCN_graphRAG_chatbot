"""Authority HTTP client — the ONLY chatbot.authority module importing requests.

책임: content-type/status/response-size 검증이 붙은 JSON GET. 실패 시 None
(예외를 올리지 않고, 응답 본문을 로그에 남기지 않는다). 정책 값 불변.
허용 의존성: requests + chatbot.authority.registry(타임아웃 기본값).
외부 부작용: HTTP GET.
기존 facade: tools/external_authority.py.

Moved verbatim from tools/external_authority.py (work order Phase 6.3).
"""

from __future__ import annotations

from typing import Optional

import requests

from chatbot.authority.registry import DEFAULT_TIMEOUT_SEC
from chatbot.observability import events as obs
from chatbot.observability import telemetry

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # reject oversized payloads unparsed
USER_AGENT = "SihwaGraphRAG/0.2 (academic research chatbot; https://poetrytalks.org)"
_JSON_CONTENT_TYPES = ("application/json", "application/ld+json", "text/json")


def _fetch(url: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> Optional[dict]:
    """GET + JSON parse. Returns None on ANY failure (timeout, non-200, wrong
    content-type, oversized body, invalid JSON). Never raises, never logs the
    response body.

    Observability: one `authority.http.completed` with the status code/class
    and response SIZE only — never the URL, identifier, or body."""
    with telemetry.span(obs.AUTHORITY_HTTP, attempt_count=1) as span:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            code = resp.status_code
            span.set(http_status_code=code,
                     http_status_class=obs.http_status_class(code))
            if resp.status_code != 200:
                span.set(status=obs.STATUS_ERROR)
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            if not any(t in ctype for t in _JSON_CONTENT_TYPES):
                span.set(status=obs.STATUS_ERROR)
                return None
            size = len(resp.content)
            span.set(response_bytes=size if isinstance(size, int) else None)
            if size > MAX_RESPONSE_BYTES:
                span.set(status=obs.STATUS_ERROR)
                return None
            data = resp.json()
            span.set(status=obs.STATUS_SUCCESS)
            return data
        except (requests.RequestException, ValueError) as exc:
            span.set(status=obs.STATUS_ERROR, error_type=type(exc).__name__)
            return None

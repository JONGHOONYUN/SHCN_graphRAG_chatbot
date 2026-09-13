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

MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # reject oversized payloads unparsed
USER_AGENT = "SihwaGraphRAG/0.2 (academic research chatbot; https://poetrytalks.org)"
_JSON_CONTENT_TYPES = ("application/json", "application/ld+json", "text/json")


def _fetch(url: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> Optional[dict]:
    """GET + JSON parse. Returns None on ANY failure (timeout, non-200, wrong
    content-type, oversized body, invalid JSON). Never raises, never logs the
    response body."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in _JSON_CONTENT_TYPES):
            return None
        if len(resp.content) > MAX_RESPONSE_BYTES:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None

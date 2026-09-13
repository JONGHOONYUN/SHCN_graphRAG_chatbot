"""Authority fetch cache — TTL + bounded, successes only.

책임: source|node_type|normalized_id 키의 in-process cache. 실패는 절대
cache하지 않는다 (다음 턴 재시도 가능). 정책 값(TTL/크기)은 이동 전과 동일.
허용 의존성: 표준 라이브러리만. 외부 부작용: process-local dict 상태.
기존 facade: tools/external_authority.py (같은 dict 객체를 re-export).

Moved verbatim from tools/external_authority.py (work order Phase 6.3).
"""

from __future__ import annotations

import time
from typing import Any

CACHE_KEY = "external_authority_cache"  # legacy key (kept for compatibility)

CACHE_MAX_SIZE = 256
_authority_cache: "dict[str, tuple[float, Any]]" = {}


def _cache_get(key: str):
    entry = _authority_cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() >= expires_at:
        _authority_cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_sec: int) -> None:
    if len(_authority_cache) >= CACHE_MAX_SIZE:
        for old in sorted(_authority_cache, key=lambda k: _authority_cache[k][0])[:16]:
            _authority_cache.pop(old, None)
    _authority_cache[key] = (time.time() + ttl_sec, value)


def clear_authority_cache() -> None:
    """Test/maintenance helper — empties the in-process authority cache."""
    _authority_cache.clear()

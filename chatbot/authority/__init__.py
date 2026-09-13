"""Authority layer — external authority source registry, parsers, HTTP client.

책임: source registry(단일 source of truth), ID 검증/변환, HTTP fetch + TTL
cache, source별 parser(allowlist 필드만 추출), 구조화된 fetch 결과.
계층 내 의존성 방향: validators → parsers → registry → client/cache → service.
registry/validators/parsers는 순수(표준 라이브러리만); `requests`는 client.py
한 곳에서만 import한다.
외부 부작용: client.py의 HTTP GET, cache.py의 in-process cache.
기존 facade: tools/external_authority.py (모든 심볼 re-export).
"""

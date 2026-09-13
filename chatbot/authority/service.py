"""Structured authority fetch service + legacy string entry point.

책임: registry 기반 단일 record fetch(fetch_authority)와 레거시 ReAct tool용
'source:id' 문자열 엔트리포인트(external_authority_lookup).
허용 의존성: chatbot.authority.{registry, client, cache}; streamlit은
_effective_language의 지연 조회에서만 optional하게 접근한다.
외부 부작용: HTTP fetch(client 경유), cache 갱신 (성공만).
기존 facade: tools/external_authority.py.

Moved verbatim from tools/external_authority.py (work order Phase 6.5).
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from chatbot.authority.cache import _cache_get, _cache_set
from chatbot.authority.client import _fetch
from chatbot.authority.registry import (
    CAPABILITY_FETCHABLE,
    CAPABILITY_LINK_ONLY,
    CAPABILITY_UNSUPPORTED,
    FETCHABLE_SOURCES,
    _url_safe,
    link_only_reference,
    resolve_source,
)


def _effective_language(explicit: Optional[str] = None) -> str:
    """Resolve response language without hard-depending on streamlit."""
    if explicit:
        return explicit
    try:
        import streamlit as st

        lang = st.session_state.get("effective_language")
        if lang:
            return lang
    except Exception:
        pass
    return "ko"


def fetch_authority(
    source: str,
    ext_id: str,
    *,
    node_type: str = "Person",
    language: Optional[str] = None,
    fetcher: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Fetch ONE authority record and return a STRUCTURED, non-fatal result.

    Shape:
        { source, key, id, node_type, capability, fetchable, status, url,
          data?, error?/note? }
    status ∈ {"ok", "unavailable", "error", "link_only", "unsupported"}

    Guarantees:
      * The ID must come from a graph node — never guessed from a name.
      * The ID is validated against the source's node-type-specific pattern
        BEFORE any URL is built, so an invalid/foreign ID makes no HTTP request.
      * Failures never raise; graph/vector evidence survives.
      * Successes cached by source|node_type|normalized_id; failures not cached.

    `fetcher` injects a fake HTTP layer for tests: callable url -> dict|None.
    """
    raw_source = (source or "").strip().lower()
    ext_id = (ext_id or "").strip()
    language = _effective_language(language)
    cfg = resolve_source(raw_source, node_type)

    if cfg is None:
        return {
            "source": raw_source, "id": ext_id, "node_type": node_type,
            "fetchable": False, "status": "error",
            "error": f"unknown authority source '{raw_source}'",
            "supported_sources": list(FETCHABLE_SOURCES),
        }

    base = {
        "source": cfg.id_key, "key": cfg.key, "label": cfg.label,
        "id": ext_id, "node_type": node_type, "capability": cfg.capability,
        "fetchable": cfg.capability == CAPABILITY_FETCHABLE,
    }

    if cfg.capability == CAPABILITY_UNSUPPORTED:
        return {**base, "status": "unsupported",
                "note": cfg.note or "source not supported; no data fetched and no link built"}

    if cfg.node_types and node_type not in cfg.node_types:
        return {**base, "status": "error",
                "error": f"source '{cfg.key}' does not apply to node type '{node_type}'"}

    if not ext_id:
        return {**base, "status": "error", "error": "empty id"}

    # Validate BEFORE constructing any URL — an invalid id must not cause a request.
    if not cfg.validate_id(ext_id):
        return {**base, "status": "error",
                "error": f"invalid id format for source '{cfg.key}'"}

    if cfg.capability == CAPABILITY_LINK_ONLY:
        ref = link_only_reference(cfg.key, ext_id, node_type)
        return {**base, "status": "link_only", "url": ref["url"] if ref else None,
                "note": "link-only source: no data fetched; cite as a reference link only"}

    request_id = cfg.request_id(ext_id)
    if not request_id:
        return {**base, "status": "error",
                "error": f"id transform failed for source '{cfg.key}'"}

    url = cfg.request_url.format(id=_url_safe(request_id))
    # Cache key uses the ORIGINAL authority id (prefix included), so
    # aks_digerati|Person|koreanPerson_7249 and
    # aks_digerati_place|Place|koreanPlace_7249 can never share an entry.
    cache_key = f"{cfg.key}|{node_type}|{ext_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    do_fetch = fetcher if fetcher is not None else (
        lambda u: _fetch(u, timeout=cfg.timeout_sec)
    )
    raw = do_fetch(url)
    if raw is None:
        # Not cached — retryable next turn.
        return {**base, "status": "unavailable", "url": url,
                "hint": "fetch failed/timeout/invalid payload; use graph-only info and note the gap."}

    # Response-side validation: a wrong-schema or wrong-record HTTP 200 is
    # rejected here, BEFORE parsing, and never becomes factual evidence.
    if cfg.response_validator is not None:
        validation_error = cfg.response_validator(raw, request_id)
        if validation_error:
            return {**base, "status": "error", "url": url,
                    "error": validation_error}

    parsed = cfg.parser(raw, request_id, language)
    if isinstance(parsed, dict) and parsed.get("error"):
        return {**base, "status": "unavailable", "url": url,
                "hint": f"authority returned no usable record ({parsed['error']})"}

    result = {
        **base,
        "status": "ok",
        # Prefer the API's own canonical link; else the verified citation URL.
        "url": (parsed.get("canonical_link") or parsed.get("url")
                or (cfg.citation_url.format(id=_url_safe(ext_id)) if cfg.citation_url else url)),
        "data": parsed,
    }
    _cache_set(cache_key, result, cfg.cache_ttl_sec)
    return result


def external_authority_lookup(query: str) -> str:
    """'source:id' → JSON string. Legacy input forms 'wikidata:<Q-id>' and
    'aks_digerati:<koreanPerson_id>' keep working (they resolve to the Person
    configs). Never raises."""
    if not isinstance(query, str) or ":" not in query:
        return json.dumps(
            {"error": "query must be 'source:id' form", "example": "wikidata:Q2913717",
             "supported_sources": list(FETCHABLE_SOURCES)},
            ensure_ascii=False,
        )

    source, ext_id = query.split(":", 1)
    node_type = "Place" if ext_id.strip().startswith("koreanPlace_") else "Person"
    result = fetch_authority(source, ext_id, node_type=node_type)

    if result.get("status") == "ok":
        payload = dict(result.get("data") or {})
        payload.setdefault("source", result.get("source"))
        payload["url"] = result.get("url")
    else:
        payload = {
            "error": result.get("error") or result.get("note")
            or "외부 정보 미조회 (fetch failed or timeout)",
            "source": result.get("source"), "id": result.get("id"),
            "status": result.get("status"),
        }
        if result.get("url"):
            payload["url"] = result["url"]
    return json.dumps(payload, ensure_ascii=False)

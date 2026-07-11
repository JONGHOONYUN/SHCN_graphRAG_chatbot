"""External Authority Lookup — Phase 1

Person 노드에 저장된 idWikidata, idAKSdigerati 값으로 실시간 외부 authority에서
전기(biography)·별칭·생몰년 등을 조회하는 LangChain Tool.

Phase 1 지원 소스:
  - wikidata      : https://www.wikidata.org/wiki/Special:EntityData/{id}.json
  - aks_digerati  : https://digerati.aks.ac.kr:85/api/IdValues/{id}

정책 (사용자 결정 반영):
  - 모드: graphRAG 전용 (agent.py tools에 등록되어 있으므로 자동 그러함)
  - 호출 시점: agent가 자동 판단 (tool description으로 안내)
  - 캐시: streamlit session_state 세션 캐시 (탭 단위, 소멸 시 초기화)
  - Timeout: 5초, 실패 시 재시도 없이 스킵하고 "외부 정보 미조회" 명시
  - 언어: session_state["effective_language"] 참조하여 Wikidata labels/descriptions 우선순위 결정
"""

import json
from typing import Optional

import requests
import streamlit as st


TIMEOUT_SEC = 5
CACHE_KEY = "external_authority_cache"

AUTHORITY_HANDLERS = {
    "wikidata": {
        "url": "https://www.wikidata.org/wiki/Special:EntityData/{id}.json",
        "parser": "parse_wikidata",
    },
    "aks_digerati": {
        "url": "https://digerati.aks.ac.kr:85/api/IdValues/{id}",
        "parser": "parse_aks_digerati",
    },
}

# effective_language별 Wikidata 라벨·설명 선호 순서.
# 사용자 언어를 최우선으로 두되, 없으면 다른 CJK/영어로 폴백.
_WIKIDATA_LANG_PRIORITY = {
    "ko": ["ko", "en", "zh", "ja"],
    "en": ["en", "ko", "zh"],
    "zh": ["zh", "en", "ko", "ja"],
}


def _get_cache() -> dict:
    """세션 캐시 dict을 반환. 없으면 초기화."""
    if CACHE_KEY not in st.session_state:
        st.session_state[CACHE_KEY] = {}
    return st.session_state[CACHE_KEY]


def _fetch(url: str) -> Optional[dict]:
    """HTTP GET 후 JSON 파싱. 실패·타임아웃 시 None.
    User-Agent 명시로 서버측 로그·rate limit 정책에서 정중히 처리되도록 함."""
    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT_SEC,
            headers={"User-Agent": "SihwaGraphRAG/0.1 (academic research chatbot)"},
        )
        if resp.status_code == 200:
            return resp.json()
    except (requests.RequestException, ValueError):
        return None
    return None


# ──────────────────────────────────────────────
# Wikidata parser
# ──────────────────────────────────────────────
def parse_wikidata(data: dict, entity_id: str, user_language: str = "ko") -> dict:
    """Wikidata Special:EntityData JSON에서 인물 요약을 추출.
    labels/descriptions는 사용자 언어 우선순위로 primary_* 필드에 대표값을 지정하고,
    모든 대상 언어 값은 *_by_lang에 함께 노출한다.
    생몰(P569/P570)은 있으면 raw time 문자열 그대로 반환 (예: '+1168-01-01T00:00:00Z')."""
    entity = data.get("entities", {}).get(entity_id, {})
    labels = entity.get("labels", {})
    descs = entity.get("descriptions", {})
    aliases = entity.get("aliases", {})
    claims = entity.get("claims", {})

    priority = _WIKIDATA_LANG_PRIORITY.get(user_language, ["en", "ko", "zh"])

    def _pick(field_dict: dict) -> Optional[str]:
        for lang in priority:
            if lang in field_dict:
                return field_dict[lang].get("value")
        # 마지막 폴백: 임의의 첫 언어
        for lang, blob in field_dict.items():
            if isinstance(blob, dict):
                return blob.get("value")
        return None

    def _time_claim(prop: str) -> Optional[str]:
        try:
            return claims[prop][0]["mainsnak"]["datavalue"]["value"]["time"]
        except (KeyError, IndexError, TypeError):
            return None

    result = {
        "source": "wikidata",
        "url": f"https://www.wikidata.org/wiki/{entity_id}",
        "primary_name": _pick(labels),
        "primary_description": _pick(descs),
        "names_by_lang": {
            lang: labels[lang]["value"]
            for lang in ("ko", "en", "zh", "ja")
            if lang in labels and isinstance(labels[lang], dict)
        },
        "descriptions_by_lang": {
            lang: descs[lang]["value"]
            for lang in ("ko", "en", "zh")
            if lang in descs and isinstance(descs[lang], dict)
        },
        "aliases": [
            a["value"]
            for lang in ("en", "ko")
            for a in aliases.get(lang, [])
            if isinstance(a, dict) and "value" in a
        ][:10],
        "birth_time": _time_claim("P569"),
        "death_time": _time_claim("P570"),
    }
    # None/빈 값 제거로 LLM 컨텍스트 절약
    return {k: v for k, v in result.items() if v not in (None, [], {})}


# ──────────────────────────────────────────────
# AKS Digerati parser
# 실제 응답 스키마 미확인 단계이므로 방어적으로 처리.
# 알려진 한국 인물 authority 필드가 있으면 명시적으로 노출하고,
# 없으면 raw_snippet에 상위 20개 필드를 잘라 담아 LLM이 직접 해석하도록.
# ──────────────────────────────────────────────
_AKS_KNOWN_FIELDS = (
    "nameKor", "nameChi", "nameEng", "nameMR", "nameRR", "namePY",
    "yearBirth", "yearDeath",
    "gender", "clan", "office",
    "biography", "descKor", "descEng", "descChi",
    "era", "dynasty",
)


def parse_aks_digerati(data, entity_id: str) -> dict:
    result = {
        "source": "aks_digerati",
        "url": f"https://digerati.aks.ac.kr:85/api/IdValues/{entity_id}",
    }

    if isinstance(data, dict):
        # 익숙한 필드가 있으면 우선 노출
        for known in _AKS_KNOWN_FIELDS:
            if known in data and data[known] not in (None, "", []):
                result[known] = data[known]
        # 익숙한 필드가 하나도 없었다면 raw_snippet 폴백
        if len(result) <= 2:
            snippet = {}
            for k, v in list(data.items())[:20]:
                if isinstance(v, (dict, list)):
                    snippet[k] = "..."
                else:
                    snippet[k] = str(v)[:200]
            result["raw_snippet"] = snippet
    elif isinstance(data, list):
        result["items_count"] = len(data)
        if data:
            first = data[0]
            result["first_item"] = (
                first if isinstance(first, dict) else str(first)[:500]
            )
    else:
        result["raw"] = str(data)[:500]

    return result


# ──────────────────────────────────────────────
# Public entry point (LangChain Tool.func)
# ──────────────────────────────────────────────
def external_authority_lookup(query: str) -> str:
    """LangChain ReAct agent가 호출하는 진입점.

    query 형식: 'source:id' — 예:
        'wikidata:Q2913717'
        'aks_digerati:koreanPerson_18816'

    반환: JSON 문자열 (한글 포함 시 ensure_ascii=False).
    Observation으로 그대로 agent에 전달되어 LLM이 해석·인용.

    실패 시에도 예외를 raise하지 않고 error dict를 문자열로 반환하여
    agent iteration이 crash 없이 진행되도록 함.
    """
    if not isinstance(query, str) or ":" not in query:
        return json.dumps(
            {
                "error": "query must be 'source:id' form",
                "example": "wikidata:Q2913717",
                "supported_sources": list(AUTHORITY_HANDLERS.keys()),
            },
            ensure_ascii=False,
        )

    source, ext_id = query.split(":", 1)
    source = source.strip().lower()
    ext_id = ext_id.strip()

    if not ext_id:
        return json.dumps(
            {"error": "empty id after ':'", "query": query}, ensure_ascii=False
        )

    if source not in AUTHORITY_HANDLERS:
        return json.dumps(
            {
                "error": f"unsupported source: {source}",
                "supported_sources": list(AUTHORITY_HANDLERS.keys()),
            },
            ensure_ascii=False,
        )

    # 세션 캐시 확인
    cache = _get_cache()
    cache_key = f"{source}:{ext_id}"
    if cache_key in cache:
        return cache[cache_key]

    cfg = AUTHORITY_HANDLERS[source]
    url = cfg["url"].format(id=ext_id)
    raw = _fetch(url)

    if raw is None:
        # 실패 응답도 캐시하지 않음 (다음 호출에서 재시도 여지)
        return json.dumps(
            {
                "error": "외부 정보 미조회 (fetch failed or timeout)",
                "source": source,
                "id": ext_id,
                "url": url,
                "hint": "5초 안에 응답이 없거나 서버 오류. 답변에서 이 인물의 외부 authority 데이터는 건너뛰고 그래프 정보만 사용하세요.",
            },
            ensure_ascii=False,
        )

    # 언어별 파서 분기
    user_language = st.session_state.get("effective_language", "ko")

    if source == "wikidata":
        parsed = parse_wikidata(raw, ext_id, user_language)
    elif source == "aks_digerati":
        parsed = parse_aks_digerati(raw, ext_id)
    else:
        parsed = {"source": source, "raw": str(raw)[:500]}

    result_str = json.dumps(parsed, ensure_ascii=False)
    cache[cache_key] = result_str
    return result_str

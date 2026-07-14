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
import time
from typing import Any, Callable, Optional

import requests


TIMEOUT_SEC = 5
CACHE_KEY = "external_authority_cache"

# ──────────────────────────────────────────────
# Fetchable vs link-only sources
#
# FETCHABLE:  an actual HTTP handler + parser is implemented and tested. Data
#             returned by these may be cited as *fetched* facts.
# LINK_ONLY:  the graph may carry an ID for these authorities, and we can build
#             a public link, but NO data is fetched — the synthesis layer must
#             NOT claim their contents as retrieved facts.
#
# This registry is the single source of truth that prompts/tool descriptions
# must stay aligned with (see CLAUDE_CODE_REFACTOR_TASK.md § 3).
# ──────────────────────────────────────────────
AUTHORITY_HANDLERS = {
    "wikidata": {
        "url": "https://www.wikidata.org/wiki/Special:EntityData/{id}.json",
        "parser": "parse_wikidata",
        "id_transform": None,  # Wikidata Q-id는 그대로 사용
    },
    "aks_digerati": {
        # 실측: 엔드포인트는 GET /api/IdValues/{integer_Id}. 파라미터는 integer 필수.
        # Neo4j에 저장된 값은 'koreanPerson_18816' 형태 문자열이므로 뒤의 숫자만 추출.
        "url": "https://digerati.aks.ac.kr:85/api/IdValues/{id}",
        "parser": "parse_aks_digerati",
        "id_transform": "aks_digerati_id",
    },
}

FETCHABLE_SOURCES = tuple(AUTHORITY_HANDLERS.keys())

# Link-only authorities: we can present a link but must never claim fetched data.
# base_url uses '{id}' as the substitution point for the stored external ID.
LINK_ONLY_SOURCES = {
    "aks_ency": "https://encykorea.aks.ac.kr/Article/{id}",
    "aks_sillok": "https://sillok.history.go.kr/{id}",
    "loc": "https://id.loc.gov/authorities/names/{id}",
    "bnf": "https://catalogue.bnf.fr/ark:/12148/cb{id}",
    "britannica": "https://www.britannica.com/{id}",
    "cbdb": "https://cbdb.fas.harvard.edu/cbdbapi/person.php?id={id}",
    "open_library": "https://openlibrary.org/works/{id}",
}


def link_only_reference(source: str, ext_id: str) -> Optional[dict]:
    """Build a citable, link-only reference for a non-fetchable authority.

    Returns a dict with fetchable=False so the synthesis layer knows the target
    is a link, not a set of retrieved facts. Returns None if the source is
    unknown or the id is empty. Never fabricates a link from a missing id."""
    if not ext_id or source not in LINK_ONLY_SOURCES:
        return None
    return {
        "source": source,
        "fetchable": False,
        "status": "link_only",
        "url": LINK_ONLY_SOURCES[source].format(id=ext_id),
        "id": ext_id,
    }


def _transform_aks_digerati_id(raw_id: str) -> Optional[str]:
    """'koreanPerson_18816' → '18816'.
    이미 순수 정수 문자열이면 그대로 반환.
    변환 실패 시 None."""
    if raw_id.isdigit():
        return raw_id
    if "_" in raw_id:
        tail = raw_id.rsplit("_", 1)[-1]
        if tail.isdigit():
            return tail
    return None


ID_TRANSFORMS = {
    "aks_digerati_id": _transform_aks_digerati_id,
}

# effective_language별 Wikidata 라벨·설명 선호 순서.
# 사용자 언어를 최우선으로 두되, 없으면 다른 CJK/영어로 폴백.
_WIKIDATA_LANG_PRIORITY = {
    "ko": ["ko", "en", "zh", "ja"],
    "en": ["en", "ko", "zh"],
    "zh": ["zh", "en", "ko", "ja"],
}


def _effective_language(explicit: Optional[str] = None) -> str:
    """Resolve the response language without hard-depending on streamlit.

    Order: explicit arg → streamlit session_state['effective_language'] → 'ko'.
    Reading streamlit is wrapped in try/except so this module stays importable
    and testable outside a streamlit script run context."""
    if explicit:
        return explicit
    try:
        import streamlit as st  # local import: keep module usable without a ctx

        lang = st.session_state.get("effective_language")
        if lang:
            return lang
    except Exception:
        pass
    return "ko"


# ──────────────────────────────────────────────
# TTL + bounded in-process cache
#
# Replaces the previous streamlit-session-only cache. Successful authority
# results are cached by 'source:id' with a TTL and a bounded size so repeated
# lookups within a session are cheap and broad result sets cannot grow the
# cache without limit. Failures are NOT cached (so a transient outage can be
# retried on the next turn). Works without a streamlit context.
# ──────────────────────────────────────────────
CACHE_TTL_SEC = 60 * 60  # 1 hour
CACHE_MAX_SIZE = 256

# key -> (expires_at_epoch, value)
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


def _cache_set(key: str, value: Any) -> None:
    # Evict oldest-expiring entries when at capacity (cheap bounded policy).
    if len(_authority_cache) >= CACHE_MAX_SIZE:
        for old_key in sorted(_authority_cache, key=lambda k: _authority_cache[k][0])[:16]:
            _authority_cache.pop(old_key, None)
    _authority_cache[key] = (time.time() + CACHE_TTL_SEC, value)


def clear_authority_cache() -> None:
    """Test/maintenance helper — empties the in-process authority cache."""
    _authority_cache.clear()


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
# AKS Digerati parser (실측 스키마 반영)
#
# 실제 API 스펙 (GET /api/IdValues/{integer_id}):
#   응답은 List[aks_BmainModel] 형태:
#     - AkspId (int)          : AKS 내부 pk
#     - PersonId (str)         : 한국역대인물 종합정보시스템 ID (예: EXM_KS_5COb_1189_020064)
#     - Source (str)           : 데이터 출처 표기 (예: "한국역대인물 종합정보시스템")
#     - ChName / KoName (str)  : 한자·한글 이름
#     - Gender (int)           : 성별 코드
#     - YearBirth / YearDeath  : 생몰년 (integer)
#     - Link (str)             : 사용자용 canonical 뷰 URL (people.aks.ac.kr/...)
#     - aks_PersonAliases[]    : {AliasType, AliasName} — 字·號·諡號 등
#     - aks_Address[]          : {AddrType, AddrName}  — 籍貫 등
#     - aks_Entry[]            : {RuShiDoor, RuShiType, RuShiYear} — 급제/입사 이력
#
# 주의: API에는 관직 이력·가족 관계·상세 전기가 없음.
#       따라서 답변에 이런 내용이 나오면 그것은 API 데이터가 아니라 LLM의 pretrained
#       지식이므로 절대 금지 (agent_prompt에서 별도 강제).
# ──────────────────────────────────────────────
def parse_aks_digerati(data, entity_id: str) -> dict:
    """실제 스키마 기준 파서. data는 list[dict] 예상. 다른 형태면 raw로 폴백.

    LLM 환각 방지를 위해 응답에 다음을 함께 담아 반환:
      - schema_hint : 이 API에 실제로 있는 필드 명시
      - MUST_NOT_ADD: LLM이 넣기 쉬운 카테고리를 명시적으로 금지
      - answer_template : 부족한 정보를 어떻게 안내할지 문구 예시
    """
    base = {
        "source": "aks_digerati",
        "api_url": f"https://digerati.aks.ac.kr:85/api/IdValues/{entity_id}",
        # 실측 필드 목록
        "schema_hint": (
            "AKS Digerati API에 실제로 존재하는 필드만 사용하세요: "
            "KoName, ChName, YearBirth, YearDeath, Gender, Link, "
            "aks_PersonAliases(字/號/諡號 등), aks_Address(籍貫 등), "
            "aks_Entry(급제/입사 이력)."
        ),
        # 명시적 금지 목록 — Gemini가 pretrained 지식으로 자동 채우기 쉬운 것들
        "MUST_NOT_ADD": [
            "관직 이력 (문하시랑평장사·좌사간·한림학사 등 어떤 관직명도 이 API는 반환하지 않습니다)",
            "가족 관계 (아버지·어머니·아들·형제 등 이 API는 반환하지 않습니다)",
            "관련 인물 (스승·동료·후원자·최충헌/최우 등 이 API는 반환하지 않습니다)",
            "저작 목록 (『동국이상국집』 등 어떤 저작명도 이 API는 반환하지 않습니다)",
            "문학적 특징·평가 (문체·주제·영향력 서술 이 API는 반환하지 않습니다)",
            "출생지 지명 확대 (aks_Address에 있는 그대로만 사용. 예: '驪州'를 '황해도 해주'로 확대·재해석 금지. 驪州는 경기도 여주의 옛 이름입니다.)",
            "본관 (aks_Address에 '籍貫'이 있으면 그 값 그대로. 예: '驪州' 그대로 표기, '전주 이씨' 등 다른 본관 절대 지어내지 말 것)",
        ],
        # LLM이 그대로 사용할 수 있는 안내 문구 템플릿 (사용자 언어에 맞게 번역해서)
        "answer_template_when_missing": (
            "AKS Digerati 데이터베이스에는 이 인물의 [X] 정보가 포함되어 있지 않습니다."
        ),
    }

    if not isinstance(data, list) or not data:
        base["error"] = "empty or unexpected response shape"
        base["raw_head"] = str(data)[:500]
        return base

    entry = data[0]  # 첫 항목 채택. 여러 매칭이 있으면 다중 처리로 확장 가능.
    if not isinstance(entry, dict):
        base["error"] = "first item not a dict"
        base["raw_head"] = str(entry)[:500]
        return base

    # 사용자 표시용 canonical link (실제 사이트, LLM이 답변에 인용)
    base["canonical_link"] = entry.get("Link")

    # 이름·생몰
    base["name_kor"] = entry.get("KoName")
    base["name_chi"] = entry.get("ChName")
    year_birth = entry.get("YearBirth")
    year_death = entry.get("YearDeath")
    if year_birth:
        base["year_birth"] = year_birth
    if year_death:
        base["year_death"] = year_death

    # 성별 코드 (원시값 그대로 노출 — 매핑은 확인 후 별도)
    if entry.get("Gender") is not None:
        base["gender_code"] = entry["Gender"]

    # 출처 및 people.aks.ac.kr 식별자
    base["source_label"] = entry.get("Source")
    base["aks_person_id"] = entry.get("PersonId")

    # 별호 (字/號/諡號)
    aliases = entry.get("aks_PersonAliases") or []
    if aliases:
        base["aliases"] = [
            {"type": a.get("AliasType"), "name": a.get("AliasName")}
            for a in aliases
            if isinstance(a, dict) and a.get("AliasName")
        ]

    # 주소 (본관 등)
    addresses = entry.get("aks_Address") or []
    if addresses:
        base["addresses"] = [
            {"type": a.get("AddrType"), "name": a.get("AddrName")}
            for a in addresses
            if isinstance(a, dict) and a.get("AddrName")
        ]

    # 급제/입사 이력
    entries = entry.get("aks_Entry") or []
    if entries:
        base["examination_entries"] = [
            {k: v for k, v in e.items() if v}
            for e in entries
            if isinstance(e, dict)
        ]

    # 여러 매칭이 있었다면 그 개수도 알림
    if len(data) > 1:
        base["additional_matches_count"] = len(data) - 1

    return base


# ──────────────────────────────────────────────
# Structured programmatic entry point (used by the orchestrator)
# ──────────────────────────────────────────────
def fetch_authority(
    source: str,
    ext_id: str,
    *,
    language: Optional[str] = None,
    fetcher: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Fetch one fetchable authority record and return a STRUCTURED result.

    Always returns a dict of the shape:
        {
          "source": <source>,
          "id": <original id>,
          "fetchable": True,
          "status": "ok" | "unavailable" | "error",
          "url": <request/link url>,
          "data": {<parsed fields>},          # present only when status == "ok"
          ...status-specific keys...
        }

    Key guarantees for the anti-hallucination policy:
      * Never invents an authority ID — callers pass an ID that came from a
        graph node.
      * On any fetch failure returns status="unavailable" with an empty `data`;
        the synthesis layer must then say the authority data was unavailable and
        must NOT backfill from model knowledge.
      * Successful results are cached (TTL + bounded). Failures are not cached.

    `fetcher` lets tests inject a fake HTTP layer: a callable url -> dict|None.
    """
    source = (source or "").strip().lower()
    ext_id = (ext_id or "").strip()
    language = _effective_language(language)

    if source not in AUTHORITY_HANDLERS:
        return {
            "source": source,
            "id": ext_id,
            "fetchable": source in LINK_ONLY_SOURCES,
            "status": "error",
            "error": f"source '{source}' is not fetchable",
            "supported_sources": list(FETCHABLE_SOURCES),
        }
    if not ext_id:
        return {
            "source": source,
            "id": ext_id,
            "fetchable": True,
            "status": "error",
            "error": "empty id",
        }

    cache_key = f"{source}:{ext_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    cfg = AUTHORITY_HANDLERS[source]

    # ID transform is for the API REQUEST only (e.g. 'koreanPerson_18816' → '18816').
    id_for_url = ext_id
    transform_key = cfg.get("id_transform")
    if transform_key:
        transform_fn = ID_TRANSFORMS.get(transform_key)
        if transform_fn is not None:
            transformed = transform_fn(ext_id)
            if transformed is None:
                return {
                    "source": source,
                    "id": ext_id,
                    "fetchable": True,
                    "status": "error",
                    "error": f"ID transform failed for '{source}' with id '{ext_id}'",
                    "hint": "aks_digerati id should be 'koreanPerson_<integer>' or a pure integer.",
                }
            id_for_url = transformed

    url = cfg["url"].format(id=id_for_url)
    do_fetch = fetcher if fetcher is not None else _fetch
    raw = do_fetch(url)

    if raw is None:
        # Not cached — allow a retry on a later turn.
        return {
            "source": source,
            "id": ext_id,
            "fetchable": True,
            "status": "unavailable",
            "url": url,
            "hint": "fetch failed or timed out; use graph-only info and note the gap.",
        }

    if source == "wikidata":
        parsed = parse_wikidata(raw, ext_id, language)
    elif source == "aks_digerati":
        parsed = parse_aks_digerati(raw, id_for_url)
    else:  # pragma: no cover - guarded by the membership check above
        parsed = {"source": source}

    result = {
        "source": source,
        "id": ext_id,
        "fetchable": True,
        "status": "ok",
        # For aks_digerati the citable public link is the API's canonical_link,
        # NOT a URL built from the raw koreanPerson_<n> id.
        "url": parsed.get("url") or parsed.get("canonical_link") or url,
        "data": parsed,
    }
    _cache_set(cache_key, result)
    return result


# ──────────────────────────────────────────────
# String entry point (LangChain Tool.func — backward compatible)
# ──────────────────────────────────────────────
def external_authority_lookup(query: str) -> str:
    """LangChain ReAct agent 진입점. 'source:id' 문자열을 받아 JSON 문자열 반환.

    내부적으로 structured fetch_authority()에 위임하고 결과를 JSON 직렬화한다.
    실패 시에도 예외를 raise하지 않고 error/unavailable dict를 문자열로 반환하여
    agent iteration이 crash 없이 진행되도록 한다.
    """
    if not isinstance(query, str) or ":" not in query:
        return json.dumps(
            {
                "error": "query must be 'source:id' form",
                "example": "wikidata:Q2913717",
                "supported_sources": list(FETCHABLE_SOURCES),
            },
            ensure_ascii=False,
        )

    source, ext_id = query.split(":", 1)
    result = fetch_authority(source, ext_id)

    # 하위 호환: 기존 tool은 'error'가 있으면 그것을 신호로 사용했으므로
    # unavailable/ error 상태를 'error' 키로도 노출한다.
    if result.get("status") == "ok":
        payload = dict(result.get("data") or {})
        payload.setdefault("source", result["source"])
        payload["url"] = result.get("url")
    else:
        payload = {
            "error": result.get("error") or "외부 정보 미조회 (fetch failed or timeout)",
            "source": result.get("source"),
            "id": result.get("id"),
            "status": result.get("status"),
        }
        if result.get("url"):
            payload["url"] = result["url"]

    return json.dumps(payload, ensure_ascii=False)

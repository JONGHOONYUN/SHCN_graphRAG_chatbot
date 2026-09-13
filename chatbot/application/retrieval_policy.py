"""Retrieval/authority intent policy and bounded-cap configuration.

책임: authority 필요성 cue 판별(Person/Place/compare), ranking 전용 authority
gate, ranking winner 추출, cap 기본값과 env/secrets override.
허용 의존성: 표준 라이브러리 + chatbot.domain (+ override 조회 시 streamlit
지연 import — 실패해도 기본값 사용).
외부 부작용: process-local `_config_cache` (1회 해석 후 고정).
기존 facade: tools/orchestrator.py.

Moved verbatim from tools/orchestrator.py (modularization work order Phase
7.1) — 현재 heuristic을 재설계하지 않고 그대로 이동했다.
"""

from __future__ import annotations

import os

from chatbot.domain.evidence_models import Evidence

# ──────────────────────────────────────────────
# Authority-enrichment caps (bounded, configurable — work order §4)
#
# Defaults can be overridden via environment variables or Streamlit secrets
# (AUTHORITY_PERSON_CAP / AUTHORITY_PLACE_CAP / AUTHORITY_SOURCES_PER_ENTITY).
# Caps are never removed: an explicit cross-source comparison request raises the
# per-entity source limit only to the documented EXHAUSTIVE bound below.
# ──────────────────────────────────────────────
DEFAULT_PERSON_AUTHORITY_CAP = 10
DEFAULT_PLACE_AUTHORITY_CAP = 5
DEFAULT_FETCHABLE_SOURCES_PER_ENTITY = 2
EXHAUSTIVE_SOURCES_PER_ENTITY = 4   # documented bounded ceiling for compare asks

# Legacy aliases (previous defaults were 3/2; names kept for compatibility).
DEFAULT_PERSON_CAP = DEFAULT_PERSON_AUTHORITY_CAP
DEFAULT_PLACE_CAP = DEFAULT_PLACE_AUTHORITY_CAP
DEFAULT_SOURCES_PER_ENTITY = DEFAULT_FETCHABLE_SOURCES_PER_ENTITY


_config_cache: dict = {}


def _config_int(name: str, default: int) -> int:
    """Read a bounded-cap override from env or Streamlit secrets; fall back to
    the default on any error. Never returns a negative value. Resolved once per
    process (cached) — restart to change."""
    if name in _config_cache:
        return _config_cache[name]
    raw = os.environ.get(name)
    if raw is None:
        try:
            import streamlit as st

            raw = st.secrets.get(name)  # type: ignore[attr-defined]
        except Exception:
            raw = None
    try:
        value = max(0, int(raw)) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    _config_cache[name] = value
    return value


# ──────────────────────────────────────────────
# Intent routing
#
# Authority lookups run ONLY when the question asks for something external
# sources can actually answer. A poem list or pure corpus-relation query must
# trigger no external call. Person and Place intents are detected separately so
# a place question can reliably enrich Place entities.
# ──────────────────────────────────────────────
_PERSON_CUES = [
    # Korean
    "생몰", "생년", "몰년", "태어", "출생", "사망", "죽은", "자세히", "상세",
    "생애", "전기", "누구", "어떤 인물", "인물 정보", "별호", "별칭", "이명",
    "아호", "본관", "시호", "국제", "위키", "알려져",
    # English
    "biograph", "who is", "who was", "life of", "born", "birth", "death",
    "date", "alias", "aliases", "courtesy name", "pen name", "posthumous",
    "in detail", "tell me about", "identity", "international", "wikidata",
    # Chinese
    "生卒", "生平", "生年", "卒年", "出生", "逝世", "別號", "别号", "字號",
    "字号", "別名", "别名", "是谁", "介绍", "傳記", "传记", "生日",
]
_PLACE_CUES = [
    # Korean
    "어디", "위치", "지명", "장소", "지도", "지리", "위치한", "소재", "고을",
    "지역", "행정구역", "옛 지명", "어느 곳", "곳인가",
    # English
    "where is", "where was", "location", "located", "place name", "geography",
    "map", "region", "toponym", "coordinates",
    # Chinese
    "在哪", "位置", "地名", "地理", "地圖", "地图", "何處", "何处", "地区",
]
# Explicit request to compare across authorities → lift the per-entity source cap.
_COMPARE_CUES = [
    "비교", "교차", "여러 출처", "출처별", "대조",
    "compare", "cross-reference", "cross reference", "both sources", "each source",
    "对比", "比較", "比较", "交叉",
]


def _matches(question: str, cues: list) -> bool:
    if not question:
        return False
    q = question.lower()
    return any(cue.lower() in q for cue in cues)


def needs_authority(question: str, language: str = "ko") -> bool:
    """True if the question calls for ANY external authority enrichment.
    Conservative: False by default, so structural/poem-list questions never fan
    out to external APIs."""
    return _matches(question, _PERSON_CUES) or _matches(question, _PLACE_CUES)


def authority_intent(question: str, language: str = "ko") -> dict:
    """Entity-type-aware routing decision.

    Returns {"Person": bool, "Place": bool, "compare": bool}. A person-style cue
    ('생몰', 'biography') enables Person enrichment; a place-style cue ('어디',
    'location') enables Place enrichment. Kept as a lightweight cue gate (the
    fallback the work order allows) but split per entity type so place-oriented
    questions reliably reach Place authorities."""
    person = _matches(question, _PERSON_CUES)
    place = _matches(question, _PLACE_CUES)
    compare = _matches(question, _COMPARE_CUES)
    # An explicit cross-source comparison request IS an authority request even
    # without a separate biographical cue ("여러 출처로 비교해줘").
    if compare and not (person or place):
        person = True
    return {"Person": person, "Place": place, "compare": compare}


# ──────────────────────────────────────────────
# Ranking/aggregation authority gate (graph-ranking-reliability work order
# §3 P0-D)
#
# `_PERSON_CUES` includes generic biography triggers like "who is" / "tell me
# about" — necessary for real biography questions, but the reference bug
# question ("Who is the most mentioned king in Sihwa ch'ongnim?") ALSO
# contains "who is" while asking a pure corpus-ranking question. Routing that
# through `authority_intent()` fanned out to external lookups for every
# candidate Person the vector retriever happened to surface (10 of 14, in the
# observed incident) — none of them relevant to the ranking answer itself.
#
# For a detected ranking/aggregation question, `_PERSON_CUES` is NOT
# consulted at all. Only an unambiguous request for outside information (the
# cues below, or an explicit comparison request) opts in, and even then
# enrichment is scoped to the ranking winner(s) only — never the full
# candidate cohort — via `_extract_ranking_winner_ids`.
# ──────────────────────────────────────────────
_RANKING_EXTERNAL_CUES = [
    # Korean
    "위키데이터", "위키", "백과사전", "외부 출처", "외부 자료",
    # English
    "wikidata", "wikipedia", "encyclopedia", "external source", "authority record",
    "authority database",
    # Chinese
    "维基", "維基", "百科",
]


def _ranking_authority_intent(question: str) -> dict:
    """Authority-enrichment gate used ONLY for a detected ranking/aggregation
    question. Deliberately ignores `_PERSON_CUES`/`_PLACE_CUES` — see module
    note above. Ranking is currently Person-only (`tools.graph_intent`'s role
    registry has no Place roles yet), so Place enrichment always stays off
    here regardless of cues."""
    compare = _matches(question, _COMPARE_CUES)
    person = compare or _matches(question, _RANKING_EXTERNAL_CUES)
    return {"Person": person, "Place": False, "compare": compare}


def _extract_ranking_winner_ids(graph_ev: Evidence) -> set:
    """Pull the winner Person id(s) out of a `retrieve_role_ranking_evidence`
    result's `ranking` claim. Empty set when there is no ranking claim (e.g.
    the ranking query returned no_results / was unavailable) — callers must
    treat that as "no eligible authority candidates", never as "enrich
    everyone"."""
    for claim in graph_ev.claims or []:
        if isinstance(claim, dict) and claim.get("type") == "ranking":
            return set(claim.get("winner_person_ids") or [])
    return set()

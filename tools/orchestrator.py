"""GraphRAG evidence orchestrator.

Sequences graph retrieval, vector retrieval, and DETERMINISTIC authority
enrichment into one structured evidence bundle for the final synthesis step.
The retrievers never write user-facing prose.

Selection is REGISTRY-DRIVEN: which authorities apply to an entity is decided by
tools/external_authority.AUTHORITY_REGISTRY (node type + capability), not by a
hard-coded list. Person and Place are routed independently, and every request
carries its node type so `koreanPerson_*` and `koreanPlace_*` can never cross.

Design for testability: the graph retriever, vector retriever, and authority
fetcher are injectable. When not provided they are lazily imported, so unit
tests drive the whole sequence with mocks and never touch Neo4j, the LLM, the
network, or streamlit-bound modules.
"""

from __future__ import annotations

from typing import Callable, Optional

from tools.evidence import Entity, Evidence, Provenance, collect_entities
from tools.external_authority import (
    CAPABILITY_FETCHABLE,
    CAPABILITY_LINK_ONLY,
    sources_for_node_type,
)

# Caps: a broad result set must not fan out into unbounded external calls.
DEFAULT_PERSON_CAP = 3
DEFAULT_PLACE_CAP = 2
DEFAULT_SOURCES_PER_ENTITY = 2   # raised when the user asks to compare sources


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
    return {"Person": person, "Place": place, "compare": _matches(question, _COMPARE_CUES)}


# ──────────────────────────────────────────────
# Lazy default dependencies (kept out of the import path for tests)
# ──────────────────────────────────────────────
def _default_graph_retriever(question: str, language: str) -> Evidence:
    from tools.cypher import retrieve_graph_evidence

    return retrieve_graph_evidence(question)


def _default_vector_retriever(question: str, language: str) -> Evidence:
    from tools.vector import retrieve_sihwa_evidence

    return retrieve_sihwa_evidence(question, language)


def _default_authority_fetcher(source: str, ext_id: str, language: str,
                               node_type: str = "Person") -> dict:
    from tools.external_authority import fetch_authority

    return fetch_authority(source, ext_id, node_type=node_type, language=language)


def _call_fetcher(fetcher: Callable, source: str, ext_id: str, language: str,
                  node_type: str) -> dict:
    """Call the injected fetcher, tolerating 3-arg fetchers from older tests."""
    try:
        return fetcher(source, ext_id, language, node_type)
    except TypeError:
        return fetcher(source, ext_id, language)


def gather_graphrag_evidence(
    question: str,
    language: str = "ko",
    *,
    graph_retriever: Optional[Callable[[str, str], Evidence]] = None,
    vector_retriever: Optional[Callable[[str, str], Evidence]] = None,
    authority_fetcher: Optional[Callable] = None,
    person_cap: int = DEFAULT_PERSON_CAP,
    place_cap: int = DEFAULT_PLACE_CAP,
    sources_per_entity: int = DEFAULT_SOURCES_PER_ENTITY,
    want_authority: Optional[bool] = None,
) -> dict:
    """Collect graph + vector + (optional) external authority evidence.

    Returns:
        {
          "question", "language",
          "graph":    Evidence(kind='graph'),
          "vector":   Evidence(kind='vector'),
          "external": Evidence(kind='external'),
          "entities": list[Entity],   # de-duplicated Person + Place
          "persons":  list[Entity],   # compatibility view
          "places":   list[Entity],
          "authority_attempted": bool,
        }

    Sequence: graph → vector → collect entities → de-duplicate → registry-driven
    selection → capped, de-duplicated fetches → link-only references recorded
    separately. Enrichment is OPTIONAL: failures record an unavailable/error
    status and never abort the response.
    """
    graph_retriever = graph_retriever or _default_graph_retriever
    vector_retriever = vector_retriever or _default_vector_retriever
    authority_fetcher = authority_fetcher or _default_authority_fetcher

    graph_ev = graph_retriever(question, language) or Evidence(kind="graph")
    vector_ev = vector_retriever(question, language) or Evidence(kind="vector")

    entities = collect_entities(graph_ev, vector_ev)
    persons = [e for e in entities if (e.node_type or "Person") == "Person"]
    places = [e for e in entities if e.node_type == "Place"]

    intent = authority_intent(question, language)
    if want_authority is True:
        intent = {"Person": True, "Place": True, "compare": intent["compare"]}
    elif want_authority is False:
        intent = {"Person": False, "Place": False, "compare": False}

    max_sources = 99 if intent.get("compare") else sources_per_entity

    external_ev = Evidence(kind="external")
    seen: set = set()          # (key|node_type|id) already requested
    attempted = False

    for group, cap in ((persons, person_cap), (places, place_cap)):
        node_type = "Person" if group is persons else "Place"
        if not intent.get(node_type):
            continue
        enriched = 0
        for entity in group:
            if enriched >= cap:
                break
            if not entity.has_authority_id():
                continue        # never look up from a name alone
            hit = _enrich_entity(
                entity, node_type, external_ev, seen, authority_fetcher,
                language, max_sources,
            )
            if hit:
                enriched += 1
                attempted = True

    return {
        "question": question,
        "language": language,
        "graph": graph_ev,
        "vector": vector_ev,
        "external": external_ev,
        "entities": entities,
        "persons": persons,
        "places": places,
        "authority_attempted": attempted,
    }


def _enrich_entity(
    entity: Entity,
    node_type: str,
    external_ev: Evidence,
    seen: set,
    fetcher: Callable,
    language: str,
    max_sources: int,
) -> bool:
    """Fetch this entity's registry-eligible authorities. Returns True if at
    least one fetchable source was requested (link-only refs don't count toward
    the entity cap)."""
    fetched = 0
    hit = False

    for cfg in sources_for_node_type(node_type, capability=CAPABILITY_FETCHABLE):
        if fetched >= max_sources:
            break
        ext_id = entity.authority_ids.get(cfg.id_key)
        if not ext_id or not cfg.validate_id(ext_id):
            continue            # invalid/foreign id → no request at all
        key = f"{cfg.key}|{node_type}|{ext_id}"
        if key in seen:
            continue
        seen.add(key)
        result = _call_fetcher(fetcher, cfg.key, ext_id, language, node_type)
        _record_authority_result(external_ev, entity, result)
        fetched += 1
        hit = True

    # Link-only references: cited as links, never as fetched facts.
    for cfg in sources_for_node_type(node_type, capability=CAPABILITY_LINK_ONLY):
        ext_id = entity.authority_ids.get(cfg.id_key)
        if not ext_id or not cfg.validate_id(ext_id) or not cfg.citation_url:
            continue
        key = f"{cfg.key}|{node_type}|{ext_id}"
        if key in seen:
            continue
        seen.add(key)
        from tools.external_authority import link_only_reference

        ref = link_only_reference(cfg.key, ext_id, node_type)
        if ref:
            _record_link_only(external_ev, entity, ref)
    return hit


def _record_authority_result(external_ev: Evidence, entity: Entity, result: dict) -> None:
    """Fold one fetch result into the external Evidence bundle.

    Records provenance for every attempt (including failures, so synthesis can
    state the data was unavailable) and parsed data only on success."""
    source = result.get("source")
    status = result.get("status")
    url = result.get("url")

    external_ev.provenance.append(
        Provenance(
            source_type=source if isinstance(source, str) else "external",
            label=f"{result.get('label') or source} lookup for {entity.display_name()} — {status}",
            source_url=url,
            entity_id=entity.node_id,
        )
    )
    claim = {
        "entity": entity.display_name(),
        "entity_node_id": entity.node_id,
        "node_type": entity.node_type or "Person",
        "source": source,
        "source_label": result.get("label"),
        "status": status,
        "url": url,
    }
    if status == "ok":
        claim["data"] = result.get("data") or {}
    else:
        claim["note"] = result.get("error") or result.get("note") or result.get("hint") \
            or "authority data unavailable"
    external_ev.claims.append(claim)


def _record_link_only(external_ev: Evidence, entity: Entity, ref: dict) -> None:
    """Record a link-only reference. No factual content — link only."""
    external_ev.provenance.append(
        Provenance(
            source_type=ref.get("source") or "external",
            label=f"{ref.get('label')} reference link for {entity.display_name()}",
            source_url=ref.get("url"),
            entity_id=entity.node_id,
        )
    )
    external_ev.claims.append({
        "entity": entity.display_name(),
        "entity_node_id": entity.node_id,
        "node_type": entity.node_type or "Person",
        "source": ref.get("source"),
        "source_label": ref.get("label"),
        "status": "link_only",
        "url": ref.get("url"),
        "note": "link-only reference: no data was fetched; do not assert its contents",
    })

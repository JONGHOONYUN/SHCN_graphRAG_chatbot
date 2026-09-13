"""GraphRAG evidence orchestrator — graph → vector → optional authority.

책임: 검색 순서 유지, Entity 수집/분류, intent별 authority enrichment 선별,
최종 evidence bundle 조립. 반환 dict의 key와 값 의미는 이동 전과 동일하다.
허용 의존성: chatbot.domain/application/authority + tools.graph_intent.
기본 retriever/fetcher는 지연 import(테스트가 mock 주입 시 전혀 로드 안 됨).
외부 부작용: 주입/기본 retriever·fetcher 호출.
기존 facade: tools/orchestrator.py.

Moved verbatim from tools/orchestrator.py (modularization work order Phase 7.4).
"""

from __future__ import annotations

from typing import Callable, Optional

from chatbot.application.authority_enrichment import (
    _enrich_entity,
    _has_valid_fetchable_id,
)
from chatbot.application.retrieval_policy import (
    DEFAULT_FETCHABLE_SOURCES_PER_ENTITY,
    DEFAULT_PERSON_AUTHORITY_CAP,
    DEFAULT_PLACE_AUTHORITY_CAP,
    EXHAUSTIVE_SOURCES_PER_ENTITY,
    _config_int,
    _extract_ranking_winner_ids,
    _ranking_authority_intent,
    authority_intent,
)
from chatbot.application.retriever_invocation import _safe_retrieve
from chatbot.domain.evidence_merge import collect_entities
from chatbot.domain.evidence_models import Evidence
from tools.graph_intent import detect_role_cue, is_graph_aggregation_intent


# ──────────────────────────────────────────────
# Lazy default dependencies (kept out of the import path for tests)
# ──────────────────────────────────────────────
def _default_graph_retriever(question: str, language: str,
                             history_text: Optional[str] = None) -> Evidence:
    from tools.cypher import retrieve_graph_evidence

    return retrieve_graph_evidence(question, history_text=history_text)


def _default_vector_retriever(question: str, language: str,
                              history_text: Optional[str] = None) -> Evidence:
    # Vector search embeds the CURRENT question only: mixing serialized history
    # into the embedding would degrade similarity matching. History-based
    # reference resolution happens in graph retrieval and final synthesis.
    from tools.vector import retrieve_sihwa_evidence

    return retrieve_sihwa_evidence(question, language)


def _default_role_ranking_retriever(role: str) -> Evidence:
    from tools.cypher import retrieve_role_ranking_evidence

    return retrieve_role_ranking_evidence(role)


def _default_authority_fetcher(source: str, ext_id: str, language: str,
                               node_type: str = "Person") -> dict:
    from chatbot.authority.service import fetch_authority

    return fetch_authority(source, ext_id, node_type=node_type, language=language)


def gather_graphrag_evidence(
    question: str,
    language: str = "ko",
    *,
    graph_retriever: Optional[Callable] = None,
    vector_retriever: Optional[Callable] = None,
    authority_fetcher: Optional[Callable] = None,
    history_text: Optional[str] = None,
    person_cap: Optional[int] = None,
    place_cap: Optional[int] = None,
    sources_per_entity: Optional[int] = None,
    want_authority: Optional[bool] = None,
    role_ranking_retriever: Optional[Callable] = None,
    response_language: Optional[str] = None,
) -> dict:
    """Collect graph + vector + (optional) external authority evidence.

    Returns:
        {
          "question", "language",
          "question_language", "response_language",
          "graph":    Evidence(kind='graph'),
          "vector":   Evidence(kind='vector'),
          "external": Evidence(kind='external'),
          "entities": list[Entity],   # de-duplicated Person + Place
          "persons":  list[Entity],   # compatibility view
          "places":   list[Entity],
          "statuses": {"graph": {source, outcome}, "vector": {...}},  # user-safe
          "coverage": {"Person": {eligible_entity_count, enriched_entity_count,
                                  skipped_due_to_cap_count}, "Place": {...}},
          "authority_attempted": bool,
          "ranking_role": Optional[str],  # e.g. "king" when routed through
                                          # the deterministic ranking template
        }

    Sequence: graph → vector → collect entities → de-duplicate → registry-driven
    selection → capped, de-duplicated fetches → link-only references recorded
    separately. `history_text` (bounded prior conversation) is forwarded to
    graph retrieval for reference resolution only. Retrieval failures become
    user-safe statuses; enrichment failures never abort the response. External
    fetches stay sequential (bounded, provider-friendly — no unbounded
    concurrency).

    Language split (work order
    CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §3/Phase 3):
    the positional `language` argument is the QUESTION language — it is
    forwarded to `vector_retriever` for vector-index selection, exactly as
    before. `response_language` (new, keyword-only, optional) is the FINAL
    output language: it is forwarded to the external authority fetcher (its
    `language` argument, e.g. for a localized authority label) and is what
    coverage-note consumers should use when rendering
    `format_evidence_for_prompt()`/`build_citations()` afterward. When
    `response_language` is omitted, it defaults to `language` — this is a
    silent-behavior-PRESERVING default: every existing single-language
    caller (`gather_graphrag_evidence(question, "ko", ...)`) keeps working
    exactly as before, since question and response language are then the
    same value throughout, identical to pre-split behavior. The graph
    retriever never receives a language-driven translation of `question` —
    only the raw question text, unaffected by this split.

    Ranking/aggregation routing (graph-ranking-reliability work order §3
    P0-B/P0-D): when `question` is a detected corpus-wide rank/count
    question naming a registered role (currently just "king" — see
    `tools.graph_intent.ROLE_TYPE_CUES`), `graph_retriever` is bypassed
    entirely in favor of `role_ranking_retriever` (default:
    `retrieve_role_ranking_evidence`), a parameterized template — never
    free-form LLM Cypher generation. The generic `_PERSON_CUES`/`_PLACE_CUES`
    authority gate is also bypassed for this path (see
    `_ranking_authority_intent`): a bare "who is"/"tell me about" no longer
    triggers external lookups, and when an explicit external-source request
    IS present, enrichment is scoped to the ranking winner(s) only — never
    the full candidate cohort."""
    question_language = language
    resp_language = response_language if response_language is not None else question_language

    graph_retriever = graph_retriever or _default_graph_retriever
    vector_retriever = vector_retriever or _default_vector_retriever
    authority_fetcher = authority_fetcher or _default_authority_fetcher
    role_ranking_retriever = role_ranking_retriever or _default_role_ranking_retriever
    if person_cap is None:
        person_cap = _config_int("AUTHORITY_PERSON_CAP", DEFAULT_PERSON_AUTHORITY_CAP)
    if place_cap is None:
        place_cap = _config_int("AUTHORITY_PLACE_CAP", DEFAULT_PLACE_AUTHORITY_CAP)
    if sources_per_entity is None:
        sources_per_entity = _config_int(
            "AUTHORITY_SOURCES_PER_ENTITY", DEFAULT_FETCHABLE_SOURCES_PER_ENTITY)

    ranking_role = detect_role_cue(question) if is_graph_aggregation_intent(question) else None

    if ranking_role:
        def _ranking_graph_retriever(q, lang, hist=None, _role=ranking_role):
            return role_ranking_retriever(_role)

        graph_ev, graph_status = _safe_retrieve(
            _ranking_graph_retriever, question, question_language, history_text, "graph")
    else:
        graph_ev, graph_status = _safe_retrieve(
            graph_retriever, question, question_language, history_text, "graph")
    vector_ev, vector_status = _safe_retrieve(
        vector_retriever, question, question_language, history_text, "vector")

    entities = collect_entities(graph_ev, vector_ev)
    persons = [e for e in entities if (e.node_type or "Person") == "Person"]
    places = [e for e in entities if e.node_type == "Place"]

    if ranking_role:
        intent = _ranking_authority_intent(question)
        # Candidate pool for enrichment is the graph ranking winner(s) ONLY
        # (P0-D item 5) — never the full `persons` cohort, and never a
        # person the vector retriever happened to surface. An empty winner
        # set (ranking failed / no_results) means zero eligible candidates,
        # which forces zero fetches below regardless of `intent`.
        winner_ids = _extract_ranking_winner_ids(graph_ev)
        persons_for_authority = [e for e in persons if e.node_id in winner_ids]
        places_for_authority: list = []
    else:
        intent = authority_intent(question, resp_language)
        persons_for_authority = persons
        places_for_authority = places

    if want_authority is True:
        intent = {"Person": True, "Place": True, "compare": intent["compare"]}
    elif want_authority is False:
        intent = {"Person": False, "Place": False, "compare": False}

    # An explicit cross-source comparison raises the per-entity source limit
    # only to the documented bounded ceiling — caps are never removed.
    max_sources = (max(EXHAUSTIVE_SOURCES_PER_ENTITY, sources_per_entity)
                   if intent.get("compare") else sources_per_entity)

    external_ev = Evidence(kind="external")
    seen: set = set()          # source|node_type|original_id already requested
    attempted = False
    coverage: dict = {}

    for node_type, group, cap in (
        ("Person", persons_for_authority, person_cap),
        ("Place", places_for_authority, place_cap),
    ):
        if not intent.get(node_type):
            continue
        eligible = enriched = skipped = 0
        for entity in group:
            if not entity.has_authority_id():
                continue        # never look up from a name alone
            fetch_eligible = _has_valid_fetchable_id(entity, node_type)
            if fetch_eligible:
                eligible += 1
                if enriched >= cap:
                    skipped += 1
                    continue    # cap reached — counted, reported, not fetched
            hit = _enrich_entity(
                entity, node_type, external_ev, seen, authority_fetcher,
                resp_language, max_sources,
            )
            if hit:
                enriched += 1
                attempted = True
        if eligible or enriched:
            coverage[node_type] = {
                "eligible_entity_count": eligible,
                "enriched_entity_count": enriched,
                "skipped_due_to_cap_count": skipped,
            }
            if skipped > 0:
                # Structured, user-safe coverage note (rendered by synthesis).
                external_ev.claims.append({
                    "type": "coverage", "node_type": node_type,
                    "eligible_entity_count": eligible,
                    "enriched_entity_count": enriched,
                    "skipped_due_to_cap_count": skipped,
                })

    return {
        "question": question,
        "language": language,               # unchanged: whatever the caller
                                             # passed positionally (= question_language)
        "question_language": question_language,
        "response_language": resp_language,
        "graph": graph_ev,
        "vector": vector_ev,
        "external": external_ev,
        "entities": entities,
        "persons": persons,
        "places": places,
        "statuses": {"graph": graph_status, "vector": vector_status},
        "coverage": coverage,
        "authority_attempted": attempted,
        "ranking_role": ranking_role,
    }

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

import inspect
import logging
import os
import uuid
from typing import Callable, Optional

from tools.evidence import Entity, Evidence, Provenance, collect_entities
from tools.external_authority import (
    CAPABILITY_FETCHABLE,
    CAPABILITY_LINK_ONLY,
    sources_for_node_type,
)
from tools.graph_intent import detect_role_cue, is_graph_aggregation_intent

logger = logging.getLogger(__name__)

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


# ──────────────────────────────────────────────
# User-safe retrieval status (work order §3)
#
# Retriever failures never surface raw exception text to the synthesis prompt:
# diagnostics go to server logs with a correlation code, evidence keeps only a
# stable outcome token, and tools/synthesis.py renders the localized wording.
# ──────────────────────────────────────────────
# ── Signature-based arity dispatch (Phase 4) ────────────────────────────────
# The legacy pattern `try: fn(3-args) except TypeError: fn(2-args)` conflates
# two very different failures:
#   (a) the callable does not accept 3 positional args (compatibility);
#   (b) the callable's body raised a TypeError (a real defect).
# Case (b) was being silently swallowed and the callable retried with 2 args
# — masking bugs AND double-calling network-touching mocks.
#
# The new dispatch decides arity BEFORE the call using `inspect.signature`.
# A TypeError from the callable's BODY is now surfaced as a retrieval failure
# with a correlation id, never as an arity-retry signal.


def _fn_accepts_arity(fn: Callable, target_arity: int) -> bool:
    """True iff `fn` can be called with exactly `target_arity` positional args.

    A callable that declares `*args` (e.g. MagicMock, most decorators) is
    reported as compatible with any arity. If signature introspection fails
    (some C-level callables), we return True — a genuine mismatch will
    surface as TypeError from the body, which is treated as retrieval
    failure (not retried)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    required = sum(1 for p in positional
                   if p.default is inspect.Parameter.empty)
    return required <= target_arity <= len(positional)


def _safe_retrieve(fn: Callable, question: str, language: str,
                   history_text: Optional[str], kind: str) -> tuple:
    """Run one retriever with the arity its signature actually declares.

    Signature dispatch order (Phase 4):
      1. `fn(question, language, history_text)` — canonical 3-arg
      2. `fn(question, language)` — legacy 2-arg (kept for existing tests)

    A TypeError raised from inside the callable is NOT interpreted as an
    arity mismatch — it becomes a retrieval failure with a correlation id.
    """
    if _fn_accepts_arity(fn, 3):
        args: tuple = (question, language, history_text)
    elif _fn_accepts_arity(fn, 2):
        args = (question, language)
    else:
        code = uuid.uuid4().hex[:8]
        logger.warning(
            "%s retriever signature incompatible [%s]", kind, code)
        return Evidence(kind=kind), {"source": kind,
                                     "outcome": "temporarily_unavailable"}
    try:
        ev = fn(*args)
    except Exception as e:
        code = uuid.uuid4().hex[:8]
        logger.warning("%s retrieval failed [%s]: %s: %s",
                       kind, code, type(e).__name__, e)
        return Evidence(kind=kind), {"source": kind,
                                     "outcome": "temporarily_unavailable"}
    ev = ev or Evidence(kind=kind)
    return _normalize_evidence_status(ev, kind)


def _normalize_evidence_status(ev: Evidence, kind: str) -> tuple:
    """Strip technical error claims out of Evidence (logging them instead) and
    derive the user-safe outcome. 'no_results' is NOT an error state."""
    outcome = None
    kept = []
    for claim in ev.claims or []:
        ctype = claim.get("type") if isinstance(claim, dict) else None
        if ctype == "error":
            # Legacy shape carrying raw exception text — log-only, never kept.
            code = uuid.uuid4().hex[:8]
            logger.warning("%s retrieval error claim [%s]: %s",
                           kind, code, claim.get("message"))
            outcome = outcome or "temporarily_unavailable"
            continue
        if ctype == "status":
            if claim.get("outcome") in ("temporarily_unavailable",
                                        "invalid_query", "no_results"):
                outcome = claim.get("outcome")
            continue                        # status claims are never rendered
        kept.append(claim)
    ev.claims = kept
    if outcome is None:
        outcome = "ok" if ev.documents else "no_results"
    return ev, {"source": kind, "outcome": outcome}


def _default_authority_fetcher(source: str, ext_id: str, language: str,
                               node_type: str = "Person") -> dict:
    from tools.external_authority import fetch_authority

    return fetch_authority(source, ext_id, node_type=node_type, language=language)


def _call_fetcher(fetcher: Callable, source: str, ext_id: str, language: str,
                  node_type: str) -> dict:
    """Call the injected fetcher with signature-appropriate arity.

    Introspection-based (Phase 4): a TypeError raised inside the fetcher is
    NOT interpreted as an arity mismatch — it propagates so the retriever
    layer can classify it as a real fetch failure. This prevents side-
    effectful fetchers (network calls, counters) from being invoked twice
    when their body raised for an unrelated reason."""
    if _fn_accepts_arity(fetcher, 4):
        return fetcher(source, ext_id, language, node_type)
    if _fn_accepts_arity(fetcher, 3):
        return fetcher(source, ext_id, language)
    raise TypeError(
        "authority fetcher signature incompatible — expected 3 or 4 "
        "positional arguments")


def _has_valid_fetchable_id(entity: Entity, node_type: str) -> bool:
    """True when the entity carries at least one registry-valid fetchable ID for
    its node type (link-only IDs alone do not make it cap-eligible)."""
    for cfg in sources_for_node_type(node_type, capability=CAPABILITY_FETCHABLE):
        ext_id = entity.authority_ids.get(cfg.id_key)
        if ext_id and cfg.validate_id(ext_id):
            return True
    return False


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

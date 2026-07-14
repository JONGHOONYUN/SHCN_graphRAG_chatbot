"""GraphRAG evidence orchestrator.

Sequences graph retrieval, vector retrieval, and DETERMINISTIC authority
enrichment into a single structured evidence bundle that the final synthesis
step consumes. The retrievers never write user-facing prose here — only
structured Evidence flows through.

Design for testability: the heavy dependencies (graph retriever, vector
retriever, authority fetcher) are injectable. When not provided they are lazily
imported from tools.cypher / tools.vector / tools.external_authority, so unit
tests can drive the whole sequence with mocks and never touch Neo4j, the LLM,
or the network — and never import streamlit-bound modules.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from tools.evidence import (
    Entity,
    Evidence,
    collect_person_entities,
)

# Default: enrich at most this many distinct people per request, so a broad
# result set cannot fan out into an unbounded number of external calls.
DEFAULT_AUTHORITY_CAP = 3

# The two fetchable sources, in the order we attempt them per person.
_FETCHABLE_ID_ATTRS = (
    ("wikidata", "wikidata_id"),
    ("aks_digerati", "aks_digerati_id"),
)


# ──────────────────────────────────────────────
# Authority-need gating
#
# Authority lookups happen ONLY when the user asks for biographical / naming /
# alias / date / authority-context information. A poem-list or pure structural
# question must NOT trigger any external call (acceptance criterion 4). Absence
# of a biographical cue ⇒ no enrichment.
# ──────────────────────────────────────────────
_AUTHORITY_CUES = [
    # Korean
    "생몰", "생몰년", "생년", "몰년", "태어", "출생", "사망", "죽은",
    "자세히", "상세", "생애", "전기", "누구", "어떤 인물", "인물 정보",
    "별호", "별칭", "이명", "아호", "본관", "시호", "국제", "위키", "알려져",
    # English
    "biograph", "who is", "who was", "life of", "born", "birth", "death",
    "date", "alias", "aliases", "courtesy name", "pen name", "posthumous",
    "in detail", "tell me about", "identity", "international", "wikidata",
    # Chinese
    "生卒", "生平", "生年", "卒年", "出生", "逝世", "別號", "别号", "字號",
    "字号", "別名", "别名", "是谁", "介绍", "傳記", "传记", "生日",
]


def needs_authority(question: str, language: str = "ko") -> bool:
    """True if the question calls for external authority enrichment.

    Keyword-based and conservative: returns False by default so structural /
    poem-list questions never fan out to external APIs."""
    if not question:
        return False
    q = question.lower()
    return any(cue.lower() in q for cue in _AUTHORITY_CUES)


# ──────────────────────────────────────────────
# Lazy default dependencies (kept out of import path for tests)
# ──────────────────────────────────────────────
def _default_graph_retriever(question: str, language: str) -> Evidence:
    from tools.cypher import retrieve_graph_evidence

    return retrieve_graph_evidence(question)


def _default_vector_retriever(question: str, language: str) -> Evidence:
    from tools.vector import retrieve_sihwa_evidence

    return retrieve_sihwa_evidence(question, language)


def _default_authority_fetcher(source: str, ext_id: str, language: str) -> dict:
    from tools.external_authority import fetch_authority

    return fetch_authority(source, ext_id, language=language)


def gather_graphrag_evidence(
    question: str,
    language: str = "ko",
    *,
    graph_retriever: Optional[Callable[[str, str], Evidence]] = None,
    vector_retriever: Optional[Callable[[str, str], Evidence]] = None,
    authority_fetcher: Optional[Callable[[str, str, str], dict]] = None,
    authority_cap: int = DEFAULT_AUTHORITY_CAP,
    want_authority: Optional[bool] = None,
) -> dict:
    """Collect graph + vector + (optional) external authority evidence.

    Returns a dict:
        {
          "question": str,
          "language": str,
          "graph":   Evidence(kind='graph'),
          "vector":  Evidence(kind='vector'),
          "external": Evidence(kind='external'),
          "persons": list[Entity],           # de-duplicated across sources
          "authority_attempted": bool,
        }

    Sequence (see CLAUDE_CODE_REFACTOR_TASK.md § 2):
      1. graph retrieval  2. vector retrieval  3. collect Person entities
      4. de-duplicate  5/6. lookup only when needed & only from real IDs
      7. cap the number of people enriched  8. cache handled by the fetcher.

    External enrichment is OPTIONAL: a failed lookup records an explicit
    unavailable/error status and never aborts the response.
    """
    graph_retriever = graph_retriever or _default_graph_retriever
    vector_retriever = vector_retriever or _default_vector_retriever
    authority_fetcher = authority_fetcher or _default_authority_fetcher

    # 1 + 2: structured retrieval (each degrades to empty Evidence on failure)
    graph_ev = graph_retriever(question, language) or Evidence(kind="graph")
    vector_ev = vector_retriever(question, language) or Evidence(kind="vector")

    # 3 + 4: collect and de-duplicate Person entities across both result sets
    persons = collect_person_entities(graph_ev, vector_ev)

    # 5: decide whether to enrich at all
    enrich = needs_authority(question, language) if want_authority is None else want_authority

    external_ev = Evidence(kind="external")
    attempted = False
    if enrich:
        seen: set[str] = set()          # (source:id) pairs already fetched
        enriched_people = 0
        for entity in persons:
            if enriched_people >= authority_cap:
                break
            if not entity.has_authority_id():
                continue                 # 6: never look up from a name alone
            person_hit = False
            for source, attr in _FETCHABLE_ID_ATTRS:
                ext_id = getattr(entity, attr)
                if not ext_id:
                    continue
                key = f"{source}:{ext_id}"
                if key in seen:
                    continue
                seen.add(key)
                attempted = True
                person_hit = True
                result = authority_fetcher(source, ext_id, language)
                _record_authority_result(external_ev, entity, result)
            if person_hit:
                enriched_people += 1

    return {
        "question": question,
        "language": language,
        "graph": graph_ev,
        "vector": vector_ev,
        "external": external_ev,
        "persons": persons,
        "authority_attempted": attempted,
    }


def _record_authority_result(external_ev: Evidence, entity: Entity, result: dict) -> None:
    """Fold one authority fetch result into the external Evidence bundle.

    Records a provenance entry for every attempt (including failures, so the
    synthesis layer can state the data was unavailable) and a claim carrying the
    parsed data only on success. Never invents fields."""
    from tools.evidence import Provenance  # local import avoids a cycle at top

    source = result.get("source")
    status = result.get("status")
    url = result.get("url")

    external_ev.provenance.append(
        Provenance(
            source_type=source if source in ("wikidata", "aks_digerati") else "wikidata",
            label=f"{source} lookup for {entity.display_name()} — {status}",
            source_url=url,
            entity_id=entity.node_id,
        )
    )

    claim = {
        "person": entity.display_name(),
        "person_node_id": entity.node_id,
        "source": source,
        "status": status,
        "url": url,
    }
    if status == "ok":
        claim["data"] = result.get("data") or {}
    else:
        claim["note"] = result.get("error") or "authority data unavailable"
    external_ev.claims.append(claim)

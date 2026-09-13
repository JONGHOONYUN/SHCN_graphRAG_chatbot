"""Vector-retrieval document → Evidence normalization (pure).

책임: retrieval metadata dict/LangChain Document를 Entity/NodeReference/
Provenance/Evidence로 변환. langchain을 import하지 않고 duck-typing으로 처리.
허용 의존성: 표준 라이브러리 + chatbot.domain.
외부 부작용: 없음 (metadata 계약 위반 경고 로그만 — logger 이름은 기존
"tools.evidence"를 유지).
기존 facade: tools/evidence.py (모든 심볼 re-export).

Moved verbatim from tools/evidence.py (modularization work order Phase 2.4).
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from chatbot.domain.evidence_models import (
    Entity,
    Evidence,
    Provenance,
    make_node_reference,
    merge_node_references,
)
from chatbot.domain.node_identity import (
    _linked_id,
    is_valid_node_id,
    logger,
    normalize_entry_position,
    poetrytalks_url,
)


def _clean_ids(raw: Any) -> dict:
    """Normalize an authority-id map from retrieval metadata: keep only non-empty
    string values. Neo4j returns nulls for absent properties."""
    if not isinstance(raw, dict):
        return {}
    return {
        k: v.strip()
        for k, v in raw.items()
        if isinstance(v, str) and v.strip()
    }


def _person_from_flat(p: dict) -> Optional[Entity]:
    """Build a Person Entity from a flat metadata dict (mentioned_persons,
    audiences). Any key that is not a name/id field is treated as an authority
    id, so new registry keys flow through without code changes.

    `nameRR` is deliberately read into NOTHING — it is dropped here (present
    in `reserved` only so it is excluded from `authority_ids`, never so it
    reaches a name field). The project's sole Latin-script field is `nameMR`
    (work order §2 rule 7)."""
    if not isinstance(p, dict):
        return None
    reserved = {"id", "nameKor", "nameChi", "nameEng", "nameMR", "namePY", "nameRR"}
    authority = _clean_ids({k: v for k, v in p.items() if k not in reserved})
    return Entity(
        node_id=p.get("id"), node_type="Person",
        name_kor=p.get("nameKor"), name_chi=p.get("nameChi"), name_eng=p.get("nameEng"),
        name_mr=p.get("nameMR"),
        authority_ids=authority,
    )


def _normalize_place_authority(authority: dict) -> dict:
    """A Place's idAKSdigerati (koreanPlace_<n>) belongs to the Place authority
    namespace: key it as 'aks_digerati_place' so it can never be routed to the
    AKS Person endpoint. Person-namespace values are dropped, not remapped."""
    out = dict(authority)
    raw = out.pop("aks_digerati", None)
    if isinstance(raw, str) and raw.strip():
        raw = raw.strip()
        if not raw.startswith("koreanPerson_"):
            out["aks_digerati_place"] = raw
    return out


def _place_from_flat(p: dict) -> Optional[Entity]:
    """Build a Place Entity from a flat metadata dict. `gis`/`image` are display
    data, not authority ids, so they are excluded from authority_ids.
    `nameRR` is excluded from authority_ids but never read into a name field —
    `nameMR` is the sole Latin-script field (work order §2 rule 7)."""
    if not isinstance(p, dict):
        return None
    reserved = {"id", "nameKor", "nameChi", "nameEng", "nameMR", "nameRR",
               "gis", "image"}
    authority = _normalize_place_authority(
        _clean_ids({k: v for k, v in p.items() if k not in reserved})
    )
    return Entity(
        node_id=p.get("id"), node_type="Place",
        name_kor=p.get("nameKor"), name_chi=p.get("nameChi"), name_eng=p.get("nameEng"),
        name_mr=p.get("nameMR"),
        authority_ids=authority,
    )


def entities_from_vector_meta(meta: dict) -> list:
    """Extract Person AND Place entities from one vector-retrieval metadata dict.

    Persons: the Entry creator, mentioned_persons (HAS_SUBJECT_PERSON), and
    audiences (HAS_AUDIENCE). Places: places (HAS_SUBJECT_PLACE).
    No ID is dropped in the Document.metadata → Evidence conversion."""
    entities: list = []

    creator_ids = _clean_ids(meta.get("creator_external_ids") or {})
    if meta.get("creator") or meta.get("creator_eng") or meta.get("creator_id"):
        entities.append(
            Entity(
                node_id=meta.get("creator_id"), node_type="Person",
                name_kor=meta.get("creator"), name_chi=meta.get("creator_chi"),
                name_eng=meta.get("creator_eng"), name_mr=meta.get("creator_mr"),
                authority_ids=creator_ids,
            )
        )

    for key in ("mentioned_persons", "audiences"):
        for p in meta.get(key) or []:
            e = _person_from_flat(p)
            if e is not None:
                entities.append(e)

    for pl in meta.get("places") or []:
        e = _place_from_flat(pl)
        if e is not None:
            entities.append(e)

    return entities


def person_entities_from_vector_meta(meta: dict) -> list:
    """Compatibility wrapper — Person entities only."""
    return [e for e in entities_from_vector_meta(meta) if e.node_type == "Person"]


def _doc_meta_and_page(doc: Any) -> tuple:
    """Shared (metadata, page_content) extraction for both `document_to_parts`
    and `node_references_from_vector_meta` — `doc` may be a LangChain Document
    or a plain dict, kept dependency-free of langchain."""
    if hasattr(doc, "metadata"):
        return dict(doc.metadata or {}), getattr(doc, "page_content", None)
    if isinstance(doc, dict):
        return dict(doc.get("metadata") or {}), (doc.get("page_content") or doc.get("text"))
    return {}, str(doc)


def node_references_from_vector_meta(meta: dict) -> list:
    """Extract NodeReferences for EVERY node class surfaced in one
    vector-retrieval metadata dict (work order Phase 3.1): the Entry itself,
    its Work, the creator Person, mentioned_persons/audiences (Person),
    places (Place), topics/forms_types/critical_terms (Topic/CriticalTerm),
    era (Era), and contained_poems/contained_critiques (Poem/Critique).

    Each candidate id is validated by `make_node_reference` — an absent or
    malformed `id` field (e.g. a projection that hasn't been updated yet)
    silently yields no reference rather than a fabricated one."""
    if not isinstance(meta, dict):
        return []

    work_id = meta.get("source_work_id")
    entry_id = meta.get("entry_id")
    refs = [
        make_node_reference(
            entry_id, source_type="neo4j_vector",
            name_kor=meta.get("entry_name_kor"), name_chi=meta.get("entry_name_chi"),
            name_eng=meta.get("entry_name_eng"),
        ),
        make_node_reference(
            work_id, source_type="neo4j_vector",
            name_kor=meta.get("source_work_kor"), name_chi=meta.get("source_work_chi"),
            name_eng=meta.get("source_work_eng"), name_mr=meta.get("source_work_mr"),
        ),
        make_node_reference(
            meta.get("creator_id"), source_type="neo4j_vector",
            name_kor=meta.get("creator"), name_chi=meta.get("creator_chi"),
            name_eng=meta.get("creator_eng"), name_mr=meta.get("creator_mr"),
        ),
    ]

    for key in ("mentioned_persons", "audiences", "places", "topics",
               "forms_types", "critical_terms"):
        for item in meta.get(key) or []:
            if isinstance(item, dict):
                refs.append(make_node_reference(
                    item.get("id"), source_type="neo4j_vector",
                    name_kor=item.get("nameKor"), name_chi=item.get("nameChi"),
                    name_eng=item.get("nameEng"), name_mr=item.get("nameMR"),
                ))

    era = meta.get("era")
    if isinstance(era, dict):
        refs.append(make_node_reference(
            era.get("id"), source_type="neo4j_vector",
            name_kor=era.get("nameKor"), name_eng=era.get("nameEng"),
            name_mr=era.get("nameMR"),
        ))

    for key in ("contained_poems", "contained_critiques"):
        for item in meta.get(key) or []:
            if isinstance(item, dict):
                refs.append(make_node_reference(
                    item.get("id"), source_type="neo4j_vector",
                    name_kor=item.get("nameKor"), name_chi=item.get("nameChi"),
                    name_eng=item.get("nameEng"),
                    work_id=work_id, entry_id=entry_id,
                ))

    return merge_node_references(refs)


def document_to_parts(doc: Any) -> tuple:
    """Pure helper: (langchain Document | dict) → (document_dict, entities, provenance).

    Keeps source text fields verbatim (textChi/textKor/textEng/descEng) and
    preserves work/entry provenance. `doc` may be a LangChain Document (with
    .page_content/.metadata) or a plain dict — keeps this testable without
    importing langchain."""
    meta, page = _doc_meta_and_page(doc)

    document = {
        "entry_id": meta.get("entry_id"),
        "entry_position": meta.get("entry_position"),
        "work_id": meta.get("source_work_id"),
        "work_name_kor": meta.get("source_work_kor"),
        "work_name_eng": meta.get("source_work_eng"),
        "work_name_chi": meta.get("source_work_chi"),
        # Verbatim source text fields — never altered.
        "textChi": meta.get("original_chinese"),
        "textKor": meta.get("korean_translation"),
        "textEng": meta.get("english_translation"),
        "descEng": meta.get("source_work_desc") or meta.get("creator_desc"),
        "matched_text": page,
        "score": meta.get("score"),
        "poetrytalks_link": meta.get("poetrytalks_link"),
        "contained_poems": meta.get("contained_poems"),
        "contained_critiques": meta.get("contained_critiques"),
    }
    document = {k: v for k, v in document.items() if v not in (None, [], {})}

    entities = entities_from_vector_meta(meta)

    # ── Provenance validity policy (work order Phase 2) ──────────────────
    # Only shape-valid internal node IDs may anchor a user-facing breadcrumb:
    #   1. valid entry_id            → Entry citation (position only if > 0)
    #   2. valid work_id only        → work-only citation (no faked position)
    #   3. neither valid internal ID → NO user-facing provenance; diagnostic
    #      log only (metadata contract violation — the retrieval query
    #      projects node.ID / position / work ID, so absence is a bug).
    # `(?)`, `(None)`, `Entry 0`, `Entry None` must never be produced.
    raw_entry_id = meta.get("entry_id")
    raw_work_id = meta.get("source_work_id")
    entry_id = raw_entry_id if is_valid_node_id(raw_entry_id) else None
    work_id = raw_work_id if is_valid_node_id(raw_work_id) else None
    entry_position = normalize_entry_position(meta.get("entry_position"))

    # Retrieval-time default label — Korean-first for backward compatibility.
    # Synthesis code rebuilds a language-appropriate label from the raw
    # `work_name_*` components on the Provenance record.
    work_name = (
        meta.get("source_work_kor") or meta.get("source_work_eng") or "Work"
    )
    work_ref = _linked_id(work_id)
    work_label = f"{work_name} {work_ref}" if work_ref else work_name

    provenance: list = []
    if entry_id:
        entry_part = (
            f"Entry {entry_position} {_linked_id(entry_id)}"
            if entry_position else f"Entry {_linked_id(entry_id)}"
        )
        label = f"{work_label} > {entry_part}"
    elif work_id:
        label = work_label            # work-only; no position faking
    else:
        code = uuid.uuid4().hex[:8]
        logger.warning(
            "vector provenance skipped [%s]: no valid internal work/entry id "
            "(entry_id=%r, work_id=%r) — metadata contract violation",
            code, raw_entry_id, raw_work_id,
        )
        label = None

    if label:
        provenance.append(
            Provenance(
                source_type="neo4j_vector",
                label=label,
                source_url=poetrytalks_url(entry_id) or poetrytalks_url(work_id),
                work_id=work_id,
                entry_id=entry_id,
                work_name_kor=meta.get("source_work_kor"),
                work_name_eng=meta.get("source_work_eng"),
                work_name_chi=meta.get("source_work_chi"),
                work_name_mr=meta.get("source_work_mr"),
                entry_position=entry_position,
            )
        )
    return document, entities, provenance


def docs_to_evidence(docs: Any) -> Evidence:
    """Normalize retrieved documents into a vector Evidence bundle."""
    ev = Evidence(kind="vector")
    for doc in docs or []:
        document, entities, provenance = document_to_parts(doc)
        ev.documents.append(document)
        ev.entities.extend(entities)
        ev.provenance.extend(provenance)
        meta, _page = _doc_meta_and_page(doc)
        ev.node_references.extend(node_references_from_vector_meta(meta))
    ev.node_references = merge_node_references(ev.node_references)
    return ev

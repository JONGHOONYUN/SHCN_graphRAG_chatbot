"""Structured evidence data contract for the graphRAG pipeline.

This module defines the typed objects that the retrieval + orchestration layer
produces BEFORE the single final synthesis step. Keeping these as plain
dataclasses (no streamlit / neo4j / llm imports) means the contract and all of
its helper logic can be unit-tested without a live database or API keys.

Object model (see CLAUDE_CODE_REFACTOR_TASK.md § Required Data Contract):

    Evidence   { kind, claims, entities, documents, provenance }
    Entity     { node_id, node_type, name_kor/chi/eng, wikidata_id, aks_digerati_id }
    Provenance { source_type, source_url, entity_id, work_id, entry_id,
                 poem_or_critique_id, label }

`documents` and `claims` are kept as plain dicts (per the contract) but are
always produced by the retrievers below so their shape is predictable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# Allowed vocab — kept as tuples for cheap validation/documentation. We do NOT
# hard-fail on unknown values (retrievers may legitimately surface new types);
# these are advisory and used by tests.
ENTITY_TYPES = (
    "Person", "Work", "Entry", "Poem", "Critique", "Place", "Topic", "Era",
)
SOURCE_TYPES = (
    "neo4j_graph", "neo4j_vector", "wikidata", "aks_digerati",
)
EVIDENCE_KINDS = ("graph", "vector", "external")


@dataclass
class Entity:
    """A resolved entity (usually a Person) carrying authority identifiers.

    Authority IDs must only ever be populated from a matching graph node — never
    guessed from a name. `wikidata_id` / `aks_digerati_id` are the two IDs the
    system can actually fetch (see tools/external_authority.py).
    """

    node_id: Optional[str] = None
    node_type: Optional[str] = None
    name_kor: Optional[str] = None
    name_chi: Optional[str] = None
    name_eng: Optional[str] = None
    wikidata_id: Optional[str] = None
    aks_digerati_id: Optional[str] = None

    def display_name(self) -> str:
        return (
            self.name_kor
            or self.name_chi
            or self.name_eng
            or self.node_id
            or "(unknown)"
        )

    def dedup_key(self) -> str:
        """Stable key for de-duplication: prefer the internal node ID, then an
        authority ID, then a name. Two entities that share ANY of these should
        be treated as the same person (see merge_entities)."""
        if self.node_id:
            return f"node:{self.node_id}"
        if self.wikidata_id:
            return f"wikidata:{self.wikidata_id}"
        if self.aks_digerati_id:
            return f"aks:{self.aks_digerati_id}"
        return f"name:{self.name_kor or self.name_chi or self.name_eng or ''}"

    def has_authority_id(self) -> bool:
        return bool(self.wikidata_id or self.aks_digerati_id)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Provenance:
    """Where a claim/document came from, in citable form."""

    source_type: str
    label: str
    source_url: Optional[str] = None
    entity_id: Optional[str] = None
    work_id: Optional[str] = None
    entry_id: Optional[str] = None
    poem_or_critique_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Evidence:
    """A bundle of evidence from a single retrieval source."""

    kind: str
    claims: list[dict] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "claims": list(self.claims),
            "entities": [e.to_dict() for e in self.entities],
            "documents": list(self.documents),
            "provenance": [p.to_dict() for p in self.provenance],
        }


def _entities_match(a: Entity, b: Entity) -> bool:
    """True if two Entity records almost certainly denote the same person.

    Matches on any shared non-null strong identifier (node_id, wikidata_id,
    aks_digerati_id). Names alone are intentionally NOT enough to merge, to
    avoid collapsing distinct people who share a common name."""
    if a.node_id and b.node_id and a.node_id == b.node_id:
        return True
    if a.wikidata_id and b.wikidata_id and a.wikidata_id == b.wikidata_id:
        return True
    if a.aks_digerati_id and b.aks_digerati_id and a.aks_digerati_id == b.aks_digerati_id:
        return True
    return False


def _merge_into(target: Entity, other: Entity) -> None:
    """Fill any empty field on `target` from `other` (non-destructive)."""
    for f in (
        "node_id", "node_type", "name_kor", "name_chi", "name_eng",
        "wikidata_id", "aks_digerati_id",
    ):
        if not getattr(target, f) and getattr(other, f):
            setattr(target, f, getattr(other, f))


def merge_entities(entities: list[Entity]) -> list[Entity]:
    """De-duplicate a list of entities, merging records that share a strong
    identifier. Preserves first-seen order and merges TRANSITIVELY: if a later
    record bridges two earlier ones (e.g. one had only a node_id, another only a
    wikidata_id, and a third carries both), all three collapse into one. Later
    records enrich earlier ones without ever mutating the caller's objects."""
    items = [Entity(**asdict(e)) for e in entities if e is not None]
    changed = True
    while changed:
        changed = False
        out: list[Entity] = []
        for e in items:
            found = None
            for m in out:
                if _entities_match(m, e):
                    found = m
                    break
            if found is not None:
                _merge_into(found, e)
                changed = True  # a merge may let `found` bridge later records
            else:
                out.append(e)
        items = out
    return items


def person_entities_from_vector_meta(meta: dict) -> list[Entity]:
    """Extract Person entities from one vector-retrieval metadata dict.

    Covers the Entry creator, mentioned_persons (HAS_SUBJECT_PERSON), and
    audiences (HAS_AUDIENCE). Authority IDs are taken verbatim from the graph —
    never guessed. Persons without an authority ID are still returned (they may
    be merged/enriched later) but will not trigger a lookup on their own."""
    entities: list[Entity] = []

    creator_ids = meta.get("creator_external_ids") or {}
    if meta.get("creator") or meta.get("creator_eng") or meta.get("creator_id"):
        entities.append(
            Entity(
                node_id=meta.get("creator_id"),
                node_type="Person",
                name_kor=meta.get("creator"),
                name_chi=meta.get("creator_chi"),
                name_eng=meta.get("creator_eng"),
                wikidata_id=creator_ids.get("wikidata"),
                aks_digerati_id=creator_ids.get("aks_digerati"),
            )
        )

    for key in ("mentioned_persons", "audiences"):
        for p in meta.get(key) or []:
            if not isinstance(p, dict):
                continue
            entities.append(
                Entity(
                    node_id=p.get("id"),
                    node_type="Person",
                    name_kor=p.get("nameKor"),
                    name_chi=p.get("nameChi"),
                    name_eng=p.get("nameEng"),
                    wikidata_id=p.get("wikidata"),
                    aks_digerati_id=p.get("aks_digerati"),
                )
            )
    return entities


def document_to_parts(doc: Any) -> tuple[dict, list[Entity], list[Provenance]]:
    """Pure helper: (langchain Document | dict) → (document_dict, entities, provenance).

    Keeps source text fields verbatim (textChi/textKor/textEng/descEng) and
    preserves work/entry provenance. `doc` may be a LangChain Document (with
    .page_content and .metadata) or a plain dict with those keys — this keeps
    the function testable without importing langchain."""
    if hasattr(doc, "metadata"):
        meta = dict(doc.metadata or {})
        page = getattr(doc, "page_content", None)
    elif isinstance(doc, dict):
        meta = dict(doc.get("metadata") or {})
        page = doc.get("page_content") or doc.get("text")
    else:
        meta, page = {}, str(doc)

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

    entities = person_entities_from_vector_meta(meta)

    provenance = [
        Provenance(
            source_type="neo4j_vector",
            label=(
                f"{meta.get('source_work_kor') or meta.get('source_work_eng') or 'Work'}"
                f" > Entry {meta.get('entry_position')} ({meta.get('entry_id')})"
            ),
            source_url=meta.get("poetrytalks_link"),
            work_id=meta.get("source_work_id"),
            entry_id=meta.get("entry_id"),
        )
    ]
    return document, entities, provenance


def docs_to_evidence(docs: Any) -> Evidence:
    """Normalize a list of retrieved documents into a vector Evidence bundle."""
    ev = Evidence(kind="vector")
    for doc in docs or []:
        document, entities, provenance = document_to_parts(doc)
        ev.documents.append(document)
        ev.entities.extend(entities)
        ev.provenance.extend(provenance)
    return ev


_ID_PREFIX_TO_KIND = {
    "B": "work", "E": "entry", "M": "poem", "C": "critique",
    "P": "person", "L": "place", "T": "topic", "H": "era",
}


def _looks_like_node_id(value: Any) -> Optional[str]:
    """Return the id-kind for a value like 'B016'/'E003'/'P027', else None."""
    if not isinstance(value, str) or len(value) < 2:
        return None
    prefix, rest = value[0], value[1:]
    if prefix in _ID_PREFIX_TO_KIND and rest.isdigit():
        return _ID_PREFIX_TO_KIND[prefix]
    return None


def entities_from_graph_row(row: dict) -> list[Entity]:
    """Extract Person entities from one graph result row.

    Relies on the standardized aliases requested by the Cypher-generation prompt
    (person_id, person_name_kor/chi/eng, wikidata_id, aks_digerati_id). Also
    honours a common prefixed convention '<role>_wikidata_id' etc. so multi-hop
    rows (critic_*, subject_*) still surface their IDs. Authority IDs are taken
    verbatim — never guessed from a name."""
    if not isinstance(row, dict):
        return []
    entities: list[Entity] = []

    def _add(prefix: str) -> None:
        wid = row.get(f"{prefix}wikidata_id")
        aid = row.get(f"{prefix}aks_digerati_id")
        pid = row.get(f"{prefix}person_id")
        if not (wid or aid or pid):
            return
        entities.append(
            Entity(
                node_id=pid,
                node_type="Person",
                name_kor=row.get(f"{prefix}person_name_kor"),
                name_chi=row.get(f"{prefix}person_name_chi"),
                name_eng=row.get(f"{prefix}person_name_eng"),
                wikidata_id=wid,
                aks_digerati_id=aid,
            )
        )

    _add("")  # standardized, unprefixed person
    # discover role prefixes from any '<prefix>_wikidata_id' / '<prefix>_person_id' keys
    prefixes = set()
    for key in row:
        for suffix in ("_wikidata_id", "_aks_digerati_id", "_person_id"):
            if key.endswith(suffix) and key != suffix.lstrip("_"):
                prefixes.add(key[: -len(suffix) + 1])  # keep trailing '_'
    for prefix in sorted(prefixes):
        _add(prefix)
    return entities


def provenance_from_graph_row(row: dict) -> list[Provenance]:
    """Best-effort provenance from id-looking values in a graph row."""
    if not isinstance(row, dict):
        return []
    ids: dict[str, str] = {}
    for value in row.values():
        kind = _looks_like_node_id(value)
        if kind and kind not in ids:
            ids[kind] = value
    if not ids:
        return []
    label_bits = [f"{k}={v}" for k, v in ids.items()]
    return [
        Provenance(
            source_type="neo4j_graph",
            label="Graph: " + ", ".join(label_bits),
            work_id=ids.get("work"),
            entry_id=ids.get("entry"),
            poem_or_critique_id=ids.get("poem") or ids.get("critique"),
            entity_id=ids.get("person"),
        )
    ]


def graph_rows_to_evidence(rows: Any, cypher: Optional[str] = None) -> Evidence:
    """Normalize graph query result rows into a graph Evidence bundle.

    Rows are kept as `documents` (the structured graph facts); Person entities
    and provenance are extracted where possible. The generated Cypher, if given,
    is recorded as a claim for transparency."""
    ev = Evidence(kind="graph")
    if cypher:
        ev.claims.append({"type": "cypher", "query": cypher})
    for row in rows or []:
        if isinstance(row, dict):
            ev.documents.append(row)
            ev.entities.extend(entities_from_graph_row(row))
            ev.provenance.extend(provenance_from_graph_row(row))
    return ev


def collect_person_entities(*evidences: Evidence) -> list[Entity]:
    """Gather and de-duplicate all Person entities across the given Evidence
    bundles. Non-Person entities are ignored for authority enrichment."""
    persons: list[Entity] = []
    for ev in evidences:
        if ev is None:
            continue
        for e in ev.entities:
            if e is None:
                continue
            if e.node_type in (None, "Person"):
                persons.append(e)
    return merge_entities(persons)

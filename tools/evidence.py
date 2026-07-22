"""Structured evidence data contract for the graphRAG pipeline.

Defines the typed objects the retrieval + orchestration layer produces BEFORE the
single final synthesis step. Plain dataclasses (no streamlit / neo4j / llm
imports) so the contract and all helper logic are unit-testable without a live
database, API keys, or a network.

Object model:

    Evidence   { kind, claims, entities, documents, provenance }
    Entity     { node_id, node_type, name_kor/chi/eng, authority_ids }
    Provenance { source_type, source_url, entity_id, work_id, entry_id,
                 poem_or_critique_id, label }

`Entity.authority_ids` is a generic {registry_key: id} map — the single source of
truth for external identifiers. It supports every authority declared in
tools/external_authority.py (Person and Place), not just Wikidata/AKS.
Authority IDs are only ever populated from a matching graph node, never guessed
from a name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


ENTITY_TYPES = (
    "Person", "Work", "Entry", "Poem", "Critique", "Place", "Topic", "Era",
)
SOURCE_TYPES = (
    "neo4j_graph", "neo4j_vector", "wikidata", "aks_digerati", "aks_digerati_place",
    "loc", "open_library", "cbdb", "yale_lux", "aks_ency", "britannica", "bnf",
    "world_history",
)
EVIDENCE_KINDS = ("graph", "vector", "external")

# Node types eligible for external authority enrichment.
ENRICHABLE_TYPES = ("Person", "Place")

# ──────────────────────────────────────────────
# Poetry Talks wiki URL scheme  ("poetrytalks wikidata")
#
# EVERY graph node — regardless of class (Person, Entry, Poem, Critique,
# Work, Place, Topic, Era, CriticalTerm, Series, ...) — has an `id` property
# on the Neo4j node whose value resolves deterministically to
# `https://poetrytalks.org/<id>`. The final Sources section refers to these
# URLs as **"poetrytalks wikidata"** links (per user's naming convention);
# they are the canonical, unconditional reference URL for any node cited in
# an answer.
#
# Detection strategy: any single uppercase letter followed by 1–4 digits.
# The digit ceiling rejects external-authority IDs that share a first letter
# with a node prefix — most notably `idAKSency` values like `E0063034`
# (7 digits) that look like Entry IDs but are Encyclopedia of Korean Culture
# references. Column-name filtering (`_is_external_id_key`) provides a
# second guard against those.
# ──────────────────────────────────────────────
POETRYTALKS_BASE = "https://poetrytalks.org/"

# Canonical citation category name for these URLs.
POETRYTALKS_WIKIDATA_LABEL = "poetrytalks wikidata"


def is_valid_node_id(value: Any) -> bool:
    """True if `value` fits the node-id shape: one uppercase A–Z followed by
    1–4 decimal digits. Works for every node class in the schema — including
    ones whose prefix letter is not enumerated in `_ID_PREFIX_TO_KIND`
    (e.g. CriticalTerm)."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not (2 <= len(value) <= 5):
        return False
    if not (value[0].isascii() and value[0].isupper() and value[0].isalpha()):
        return False
    return value[1:].isdigit()


def poetrytalks_url(node_id: Any) -> Optional[str]:
    """Return the Poetry Talks wiki URL ("poetrytalks wikidata" link) for any
    node ID matching the canonical shape (single uppercase letter + 1–4
    digits). Returns None for anything else — callers must never fabricate a
    URL from an unrelated string (e.g. `idAKSency` values, Wikidata Q-ids)."""
    if not isinstance(node_id, str):
        return None
    node_id = node_id.strip()
    if not node_id:
        return None
    if is_valid_node_id(node_id):
        return POETRYTALKS_BASE + node_id
    return None


def _linked_id(node_id: Optional[str]) -> str:
    """Format a node ID as a markdown link if it fits the node-id shape,
    else return it verbatim.

    CONTRACT (work order Phase 2): empty/None returns '' — NEVER a user-facing
    '?' placeholder. Callers must omit the ID portion entirely when this
    returns an empty string; `(?)`, `(None)`, `[?](...)` must not exist in any
    user-visible provenance or citation."""
    if not node_id:
        return ""
    url = poetrytalks_url(node_id)
    return f"[{node_id}]({url})" if url else str(node_id)


def normalize_entry_position(value: Any) -> Optional[int]:
    """Normalize a retrieved Entry position to a positive int, else None.

    None / 0 / negative / bool / non-numeric values are all treated as
    'position unknown' — a position of 0 is not a valid ordinal in this
    corpus, and `Entry 0` must never render as normal provenance."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            iv = int(stripped)
            return iv if iv > 0 else None
    return None


@dataclass
class Entity:
    """A resolved entity (Person or Place) carrying authority identifiers.

    `authority_ids` maps a normalized registry key ('wikidata', 'aks_digerati',
    'loc', 'aks_map', …) to the ID stored on the Neo4j node."""

    node_id: Optional[str] = None
    node_type: Optional[str] = None
    name_kor: Optional[str] = None
    name_chi: Optional[str] = None
    name_eng: Optional[str] = None
    authority_ids: dict = field(default_factory=dict)

    # ── Convenience accessors (read-only; authority_ids stays the source of truth)
    @property
    def wikidata_id(self) -> Optional[str]:
        return self.authority_ids.get("wikidata")

    @property
    def aks_digerati_id(self) -> Optional[str]:
        return self.authority_ids.get("aks_digerati")

    def display_name(self) -> str:
        return (
            self.name_kor or self.name_chi or self.name_eng
            or self.node_id or "(unknown)"
        )

    def dedup_key(self) -> str:
        """Stable key: internal node ID first, then any authority ID, then name."""
        if self.node_id:
            return f"node:{self.node_id}"
        for key in sorted(self.authority_ids):
            if self.authority_ids[key]:
                return f"{key}:{self.authority_ids[key]}"
        return f"name:{self.name_kor or self.name_chi or self.name_eng or ''}"

    def has_authority_id(self) -> bool:
        return any(v for v in self.authority_ids.values())

    def to_dict(self) -> dict:
        d = asdict(self)
        # Surface the legacy flat fields for consumers/tests that still read them.
        d["wikidata_id"] = self.wikidata_id
        d["aks_digerati_id"] = self.aks_digerati_id
        return d


@dataclass
class Provenance:
    """Where a claim/document came from, in citable form.

    `label` is the retrieval-time default (Korean-first) string. Synthesis
    consumers should PREFER rebuilding a language-appropriate label from the
    raw components (`work_name_kor/eng/chi`, `entry_position`) so a locked
    response language of `en` or `zh` doesn't leak Korean into the Sources
    section. When those fields are absent, `label` remains the fallback."""

    source_type: str
    label: str
    source_url: Optional[str] = None
    entity_id: Optional[str] = None
    work_id: Optional[str] = None
    entry_id: Optional[str] = None
    poem_or_critique_id: Optional[str] = None
    # Raw components for language-aware rendering at synthesis time.
    work_name_kor: Optional[str] = None
    work_name_eng: Optional[str] = None
    work_name_chi: Optional[str] = None
    entry_position: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Evidence:
    """A bundle of evidence from a single retrieval source."""

    kind: str
    claims: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    provenance: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "claims": list(self.claims),
            "entities": [e.to_dict() for e in self.entities],
            "documents": list(self.documents),
            "provenance": [p.to_dict() for p in self.provenance],
        }


# ──────────────────────────────────────────────
# Entity de-duplication
# ──────────────────────────────────────────────
def _entities_match(a: Entity, b: Entity) -> bool:
    """True if two Entity records denote the same node.

    Order (per the work order): Neo4j node_id first, then matching source+ID
    pairs. A Person and a Place never merge. Names alone NEVER merge — distinct
    people often share a name."""
    if a.node_type and b.node_type and a.node_type != b.node_type:
        return False
    if a.node_id and b.node_id:
        return a.node_id == b.node_id
    for key, value in a.authority_ids.items():
        if value and b.authority_ids.get(key) == value:
            return True
    return False


def _merge_into(target: Entity, other: Entity) -> None:
    """Fill empty fields on `target` from `other`, merging all non-conflicting
    authority IDs. An existing value always wins over a conflicting one."""
    for f in ("node_id", "node_type", "name_kor", "name_chi", "name_eng"):
        if not getattr(target, f) and getattr(other, f):
            setattr(target, f, getattr(other, f))
    for key, value in other.authority_ids.items():
        if value and not target.authority_ids.get(key):
            target.authority_ids[key] = value


def _copy_entity(e: Entity) -> Entity:
    return Entity(
        node_id=e.node_id, node_type=e.node_type, name_kor=e.name_kor,
        name_chi=e.name_chi, name_eng=e.name_eng,
        authority_ids=dict(e.authority_ids or {}),
    )


def merge_entities(entities: list) -> list:
    """De-duplicate entities, merging records that share a strong identifier.

    Merges TRANSITIVELY: if a later record bridges two earlier ones, all collapse
    into one. Preserves first-seen order; never mutates the caller's objects."""
    items = [_copy_entity(e) for e in entities if e is not None]
    changed = True
    while changed:
        changed = False
        out: list = []
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


def collect_entities(*evidences: Evidence, node_types: tuple = ENRICHABLE_TYPES) -> list:
    """Gather and de-duplicate entities of the given node types across bundles.

    Entities with an unset node_type are treated as Person for backward
    compatibility with earlier graph rows."""
    found: list = []
    for ev in evidences:
        if ev is None:
            continue
        for e in ev.entities or []:
            if e is None:
                continue
            ntype = e.node_type or "Person"
            if ntype in node_types:
                found.append(e)
    return merge_entities(found)


def collect_person_entities(*evidences: Evidence) -> list:
    """Compatibility wrapper — Person entities only."""
    return collect_entities(*evidences, node_types=("Person",))


# ──────────────────────────────────────────────
# Vector-retrieval normalization
# ──────────────────────────────────────────────
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
    id, so new registry keys flow through without code changes."""
    if not isinstance(p, dict):
        return None
    reserved = {"id", "nameKor", "nameChi", "nameEng", "nameMR", "namePY", "nameRR"}
    authority = _clean_ids({k: v for k, v in p.items() if k not in reserved})
    return Entity(
        node_id=p.get("id"), node_type="Person",
        name_kor=p.get("nameKor"), name_chi=p.get("nameChi"), name_eng=p.get("nameEng"),
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
    data, not authority ids, so they are excluded from authority_ids."""
    if not isinstance(p, dict):
        return None
    reserved = {"id", "nameKor", "nameChi", "nameEng", "gis", "image"}
    authority = _normalize_place_authority(
        _clean_ids({k: v for k, v in p.items() if k not in reserved})
    )
    return Entity(
        node_id=p.get("id"), node_type="Place",
        name_kor=p.get("nameKor"), name_chi=p.get("nameChi"), name_eng=p.get("nameEng"),
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
                name_eng=meta.get("creator_eng"), authority_ids=creator_ids,
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


def document_to_parts(doc: Any) -> tuple:
    """Pure helper: (langchain Document | dict) → (document_dict, entities, provenance).

    Keeps source text fields verbatim (textChi/textKor/textEng/descEng) and
    preserves work/entry provenance. `doc` may be a LangChain Document (with
    .page_content/.metadata) or a plain dict — keeps this testable without
    importing langchain."""
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

    entities = entities_from_vector_meta(meta)

    # ── Provenance validity policy (work order Phase 2) ──────────────────
    # Only shape-valid internal node IDs may anchor a user-facing breadcrumb:
    #   1. valid entry_id            → Entry citation (position only if > 0)
    #   2. valid work_id only        → work-only citation (no faked position)
    #   3. neither valid internal ID → NO user-facing provenance; diagnostic
    #      log only (metadata contract violation — the retrieval query
    #      projects node.id / position / work id, so absence is a bug).
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
    return ev


# ──────────────────────────────────────────────
# Graph-row normalization
# ──────────────────────────────────────────────
_ID_PREFIX_TO_KIND = {
    "B": "work", "E": "entry", "M": "poem", "C": "critique",
    "P": "person", "L": "place", "T": "topic", "H": "era",
}

# Standardized graph-row alias suffixes → normalized authority registry keys.
# The Cypher-generation prompt asks for these aliases (optionally role-prefixed,
# e.g. creator_wikidata_id, place_aks_map_id).
_ROW_ID_SUFFIXES = {
    "wikidata_id": "wikidata",
    "aks_digerati_id": "aks_digerati",
    "aks_ency_id": "aks_ency",
    "aks_map_id": "aks_map",
    "loc_id": "loc",
    "open_library_id": "open_library",
    "cbdb_id": "cbdb",
    "yale_lux_id": "yale_lux",
    "bnf_id": "bnf",
    "britannica_id": "britannica",
    "world_history_id": "world_history",
    "nlk_id": "nlk",
    "ency_china_id": "ency_china",
    "academia_sinica_id": "academia_sinica",
    "british_museum_id": "british_museum",
    "aks_kdp_id": "aks_kdp",
    "aks_sillok_id": "aks_sillok",
}


def _looks_like_node_id(value: Any) -> Optional[str]:
    """Return the id-kind for a value like 'B016'/'E003'/'P027', or the
    generic 'node' string for any correctly-shaped id whose prefix is not
    enumerated (e.g. CriticalTerm). Returns None if the value is not a valid
    node id shape.

    Every uppercase-letter + 1-4-digit value is treated as a node id — the
    schema has 9 node classes and this project should never DROP a node
    citation just because we don't know the exact prefix letter. External
    authority IDs (idAKSency like 'E0063034', Wikidata like 'Q464558') are
    rejected by the >4-digit ceiling and by column-name filtering elsewhere."""
    if not is_valid_node_id(value):
        return None
    prefix = value[0]
    return _ID_PREFIX_TO_KIND.get(prefix, "node")


def _is_external_id_key(key: str) -> bool:
    """True if `key` names an external-authority ID column.

    In this schema, external-reference columns are `id` immediately followed
    by an uppercase letter (idAKSency, idAKSdigerati, idWikidata, idLOC,
    idOpenLibrary, idCBDB, idYaleLux, idBritannica, idBnF, ...). Node-own ID
    columns are either exactly `id` or `<role>_id` (person_id, entry_id,
    work_id, creator_person_id, ...) — never rejected here.

    Even with the digit-length guard in `_looks_like_node_id`, keeping this
    key-level guard makes the intent explicit: idAKS* / idWiki* / idLOC etc.
    are references OUT of the graph, not identifiers of the current row's
    node. Renders as `entry=E0063034` never make sense."""
    return len(key) >= 3 and key.startswith("id") and key[2].isupper()


def _allowed_authority_keys(node_type: str) -> Optional[set]:
    """Registry keys valid for a node type, so a Person-only authority (e.g.
    wikidata) never lands on a Place entity and vice-versa. Falls back to no
    filtering if the registry is unavailable."""
    try:
        from tools.external_authority import sources_for_node_type

        return {c.id_key for c in sources_for_node_type(node_type)}
    except Exception:
        return None


def _row_entity(row: dict, prefix: str, node_type: str) -> Optional[Entity]:
    """Build one Entity from a (possibly role-prefixed) group of row aliases.

    Anchoring rules keep Person and Place rows from contaminating each other:
      * a Place is built ONLY when a place_id / place_name_* alias is present;
      * a Person is built from a person anchor, or — for legacy rows carrying
        bare authority aliases — only when no place anchor exists in the group.
    Authority IDs are filtered to those the registry allows for the node type."""
    lower = "person" if node_type == "Person" else "place"
    other = "place" if node_type == "Person" else "person"

    node_id = row.get(f"{prefix}{lower}_id")
    names = {
        "name_kor": row.get(f"{prefix}{lower}_name_kor"),
        "name_chi": row.get(f"{prefix}{lower}_name_chi"),
        "name_eng": row.get(f"{prefix}{lower}_name_eng"),
    }
    has_anchor = bool(node_id) or any(names.values())
    has_other_anchor = bool(row.get(f"{prefix}{other}_id")) or any(
        row.get(f"{prefix}{other}_name_{s}") for s in ("kor", "chi", "eng")
    )

    authority = {}
    for suffix, key in _ROW_ID_SUFFIXES.items():
        val = row.get(f"{prefix}{suffix}")
        if isinstance(val, str) and val.strip():
            authority[key] = val.strip()
    if node_type == "Place":
        # Re-key idAKSdigerati into the Place namespace BEFORE the registry
        # filter, so a koreanPlace_<n> id survives as 'aks_digerati_place'.
        authority = _normalize_place_authority(authority)
    allowed = _allowed_authority_keys(node_type)
    if allowed is not None:
        authority = {k: v for k, v in authority.items() if k in allowed}

    if not has_anchor:
        # Unanchored group: a Place is never inferred, and a Person is inferred
        # only when the row has no place anchor to attribute the ids to.
        if node_type != "Person" or has_other_anchor:
            return None
    if not node_id and not authority:
        # A bare name with no id cannot be enriched and risks name-based
        # guessing downstream — drop it.
        return None
    return Entity(node_id=node_id, node_type=node_type, authority_ids=authority, **names)


def entities_from_graph_row(row: dict) -> list:
    """Extract Person and Place entities from one graph result row.

    Uses the standardized aliases the Cypher prompt requests (person_id,
    person_name_kor, wikidata_id, aks_digerati_id, place_id, aks_map_id, …) and
    supports role prefixes for multi-hop rows (creator_*, subject_*, place_*).
    Authority IDs are taken verbatim — never guessed from a name."""
    if not isinstance(row, dict):
        return []

    entities: list = []
    # Discover role prefixes from any '<prefix>_<known suffix>' key.
    prefixes = {""}
    known_suffixes = list(_ROW_ID_SUFFIXES) + [
        "person_id", "place_id", "person_name_kor", "place_name_kor",
    ]
    for key in row:
        for suffix in known_suffixes:
            if key.endswith("_" + suffix):
                prefixes.add(key[: -len(suffix)])  # keeps the trailing '_'

    for prefix in sorted(prefixes):
        for node_type in ("Person", "Place"):
            e = _row_entity(row, prefix, node_type)
            if e is not None:
                entities.append(e)
    return entities


def provenance_from_graph_row(row: dict) -> list:
    """Best-effort provenance from id-looking values in a graph row.

    Every referenced node — Person, Entry, Poem, Critique, Work, Place,
    Topic, Era, CriticalTerm, or any other class the graph exposes — has
    its `id` value rendered as a markdown link to
    `https://poetrytalks.org/<id>`. This link is the canonical
    **"poetrytalks wikidata"** reference and MUST appear in the answer's
    Sources section for every node cited in the answer body.

    Unknown-prefix IDs (e.g. CriticalTerm if it uses a letter outside the
    named-kind map) are preserved individually under `_extra_ids` so the
    citation renderer can list them all — no node is silently dropped."""
    if not isinstance(row, dict):
        return []

    kinds: dict = {}          # kind → value  (for named kinds: entry, person, …)
    extras: list = []         # (raw_value,)  (for unknown-prefix nodes)
    seen_values: set = set()  # de-dup across the same row

    for key, value in row.items():
        # External-authority columns (idAKSency, idWikidata, ...) sometimes
        # hold values whose first letter matches a node-type prefix.
        # Skipping those columns keeps only a row's OWN id fields as URL
        # sources.
        if isinstance(key, str) and _is_external_id_key(key):
            continue
        kind = _looks_like_node_id(value)
        if not kind:
            continue
        if value in seen_values:
            continue
        seen_values.add(value)
        if kind == "node":
            extras.append(value)   # unknown-prefix — keep in insertion order
        elif kind not in kinds:
            kinds[kind] = value

    if not kinds and not extras:
        return []

    label_bits = [f"{k}={_linked_id(v)}" for k, v in kinds.items()]
    for value in extras:
        label_bits.append(f"node={_linked_id(value)}")

    primary_id = (
        kinds.get("entry") or kinds.get("poem") or kinds.get("critique")
        or kinds.get("work") or kinds.get("person") or kinds.get("place")
        or kinds.get("topic") or kinds.get("era")
        or (extras[0] if extras else None)
    )
    return [
        Provenance(
            source_type="neo4j_graph",
            label="Graph: " + ", ".join(label_bits),
            source_url=poetrytalks_url(primary_id),
            work_id=kinds.get("work"),
            entry_id=kinds.get("entry"),
            poem_or_critique_id=kinds.get("poem") or kinds.get("critique"),
            entity_id=kinds.get("person") or kinds.get("place"),
        )
    ]


def graph_rows_to_evidence(rows: Any, cypher: Optional[str] = None) -> Evidence:
    """Normalize graph query result rows into a graph Evidence bundle.

    Rows are kept as `documents` (structured graph facts); entities and
    provenance are extracted where possible. The generated Cypher, if given, is
    recorded as a claim for transparency."""
    ev = Evidence(kind="graph")
    if cypher:
        ev.claims.append({"type": "cypher", "query": cypher})
    for row in rows or []:
        if isinstance(row, dict):
            ev.documents.append(row)
            ev.entities.extend(entities_from_graph_row(row))
            ev.provenance.extend(provenance_from_graph_row(row))
    return ev

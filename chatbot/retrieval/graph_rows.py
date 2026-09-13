"""Graph result row → Evidence normalization (pure).

책임: Cypher 결과 row의 alias 해석, Person/Place Entity 추출, provenance
breadcrumb, bounded 재귀 NodeReference 추출, graph_rows_to_evidence.
허용 의존성: 표준 라이브러리 + chatbot.domain; authority 허용키 필터는
chatbot.authority.registry(순수)를 지연 import한다 (실패 시 필터 생략 —
기존 fail-open 동작 유지).
외부 부작용: 없음 (logger 이름은 기존 "tools.evidence" 유지).
기존 facade: tools/evidence.py (모든 심볼 re-export).

Moved verbatim from tools/evidence.py (modularization work order Phase 2.5).
"""

from __future__ import annotations

from typing import Any, Optional

from chatbot.domain.evidence_models import (
    Entity,
    Evidence,
    Provenance,
    make_node_reference,
    merge_node_references,
)
from chatbot.domain.node_identity import (
    NODE_ID_PREFIXES,
    _linked_id,
    poetrytalks_url,
    split_node_id,
)
from chatbot.retrieval.vector_documents import _normalize_place_authority

# Lowercase "kind" labels used by provenance/Provenance-field plumbing,
# derived from the single NODE_ID_PREFIXES registry (no second source of
# truth). "CT" -> "critical_term" is distinct from "C" -> "critique" —
# longest-prefix matching in split_node_id() keeps them from colliding.
_ID_PREFIX_TO_KIND = {
    prefix: {
        "Work": "work", "Entry": "entry", "Poem": "poem", "Critique": "critique",
        "Person": "person", "Place": "place", "Topic": "topic", "Era": "era",
        "CriticalTerm": "critical_term",
    }[node_type]
    for prefix, node_type in NODE_ID_PREFIXES.items()
}

# Legacy name-alias variants observed in REAL generated Cypher that predate
# the standardized `<stem>_name_kor|chi|eng|mr` convention (work order:
# CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §2.2). Keyed by the
# EXACT id_key each variant belongs to — deliberately explicit, never a
# generic stem-stripping transform, so a role can never inherit another
# role's name (e.g. `subject_person_id` must never read `critic_name_eng`).
# Consulted by both `_row_entity` (Person/Place extraction) and
# `_sibling_names_for_id_key` (generic NodeReference walker, e.g. for
# `critical_term_id`, which is not a Person/Place id at all).
_LEGACY_SIBLING_NAME_KEYS = {
    "subject_person_id": {
        "name_kor": "subject_name_kor", "name_chi": "subject_name_chi",
        "name_eng": "subject_name_eng", "name_mr": "subject_name_mr",
    },
    "critic_person_id": {
        "name_kor": "critic_name_kor", "name_chi": "critic_name_chi",
        "name_eng": "critic_name_eng", "name_mr": "critic_name_mr",
    },
    "critical_term_id": {
        "name_kor": "critical_term_kor", "name_chi": "critical_term_chi",
        "name_eng": "critical_term_eng", "name_mr": "critical_term_mr",
    },
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
    """Return the id-kind for a value like 'B016'/'E003'/'P027'/'CT017', or
    the generic 'node' string for any correctly-shaped id whose prefix is not
    in the registry (defensive fallback — every currently known prefix IS
    registered). Returns None if the value is not a valid node id shape.

    Uses `split_node_id()` (longest-prefix match) so a two-letter CriticalTerm
    id ('CT017') is never mistaken for a Critique id ('C017') sharing the
    letter 'C'. External authority IDs (idAKSency like 'E0063034', Wikidata
    like 'Q464558') are rejected by the digit ceiling / prefix allowlist and
    by column-name filtering elsewhere."""
    parts = split_node_id(value)
    if not parts:
        return None
    return _ID_PREFIX_TO_KIND.get(parts[0], "node")


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
    node. Renders like `entry=E0063034` never make sense."""
    return len(key) >= 3 and key.startswith("id") and key[2].isupper()


def _allowed_authority_keys(node_type: str) -> Optional[set]:
    """Registry keys valid for a node type, so a Person-only authority (e.g.
    wikidata) never lands on a Place entity and vice-versa. Falls back to no
    filtering if the registry is unavailable."""
    try:
        from chatbot.authority.registry import sources_for_node_type

        return {c.id_key for c in sources_for_node_type(node_type)}
    except Exception:
        return None


def _row_entity(row: dict, prefix: str, node_type: str) -> Optional[Entity]:
    """Build one Entity from a (possibly role-prefixed) group of row aliases.

    Anchoring rules keep Person and Place rows from contaminating each other:
      * a Place is built ONLY when a place_id / place_name_* alias is present;
      * a Person is built from a person anchor, or — for legacy rows carrying
        bare authority aliases — only when no place anchor exists in the group.
    Authority IDs are filtered to those the registry allows for the node type.

    Name lookup ALSO accepts the exact legacy alias variants observed in real
    generated Cypher (`subject_name_kor` for `subject_person_id`,
    `critic_name_kor` for `critic_person_id` — work order:
    CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §2.2/§4 Phase 2)
    via `_LEGACY_SIBLING_NAME_KEYS`, keyed by the EXACT id_key so a role can
    never inherit another role's name (no broad/heuristic fallback)."""
    lower = "person" if node_type == "Person" else "place"
    other = "place" if node_type == "Person" else "person"

    id_key = f"{prefix}{lower}_id"
    node_id = row.get(id_key)
    legacy = _LEGACY_SIBLING_NAME_KEYS.get(id_key, {})
    names = {}
    for canon in ("name_kor", "name_chi", "name_eng", "name_mr"):
        val = row.get(f"{prefix}{lower}_{canon}")
        if val is None and canon in legacy:
            val = row.get(legacy[canon])
        names[canon] = val
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


# ──────────────────────────────────────────────
# Recursive, bounded NodeReference extraction from graph rows (Phase 3.3)
#
# Cypher aggregations frequently nest ids inside collect()/map results, e.g.
#   RETURN collect({id: poem.id, nameKor: poem.nameKor}) AS poems
# and a single row can carry MULTIPLE ids of the SAME kind (several Poem ids,
# several CriticalTerm ids, ...). The scalar-only scan in
# `provenance_from_graph_row` intentionally keeps its "one breadcrumb per row"
# semantics for backward compatibility; THIS walker is the complete-coverage
# path and preserves every distinct id it finds.
#
# Safety invariants:
#   * an id is collected ONLY when it sits under an id-shaped KEY ("id",
#     "<role>_id", ..., excluding external-authority keys/aliases) — a value
#     is never treated as an id just because a string happens to fit the
#     shape (so source text can never be mistaken for a node id);
#   * `_is_external_id_key` / row-authority-suffix keys are excluded at EVERY
#     depth, so idWikidata/idAKSency/wikidata_id/aks_map_id/... nested inside
#     a collect() never leak in as Poetry Talks node ids;
#   * depth and per-structure item counts are bounded so a pathological
#     payload cannot cause unbounded recursion or scanning.
# ──────────────────────────────────────────────
_MAX_WALK_DEPTH = 6
_MAX_WALK_ITEMS = 200


def _is_id_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    if _is_external_id_key(key):
        return False
    if key == "id" or key.endswith("_id"):
        # Exclude the known external-authority row-alias suffixes
        # (wikidata_id, aks_digerati_id, loc_id, ...) regardless of any role
        # prefix — these hold external values, never a node's own id.
        return not any(key == s or key.endswith("_" + s) for s in _ROW_ID_SUFFIXES)
    return False


def _sibling_names_for_id_key(container: dict, id_key: str) -> dict:
    """Best-effort name fields living alongside an id key in the same dict,
    supporting the project's standardized snake_case convention
    (`<stem>_name_kor`), the exact legacy alias variants observed in real
    generated Cypher for `subject_person_id`/`critic_person_id`/
    `critical_term_id` (`_LEGACY_SIBLING_NAME_KEYS`), and bare Neo4j-style
    camelCase (`nameKor`) for generic collect() maps.

    `name_mr` (McCune–Reischauer) is included on equal footing — it is the
    sole Latin-script display field for Korean-related entities (work order
    §2 rule 7); `nameRR` is intentionally never read here."""
    stem = id_key[:-3] if id_key.endswith("_id") else ""
    legacy = _LEGACY_SIBLING_NAME_KEYS.get(id_key, {})
    out: dict = {}
    for canon, tail in (("name_kor", "name_kor"), ("name_chi", "name_chi"),
                       ("name_eng", "name_eng"), ("name_mr", "name_mr")):
        candidates = [f"{stem}_{tail}"] if stem else [tail]
        if canon in legacy:
            candidates.append(legacy[canon])
        candidates.append({"name_kor": "nameKor", "name_chi": "nameChi",
                           "name_eng": "nameEng", "name_mr": "nameMR"}[canon])
        for cand in candidates:
            val = container.get(cand)
            if isinstance(val, str) and val.strip():
                out[canon] = val.strip()
                break
    return out


def _walk_for_node_refs(value: Any, refs: dict, depth: int = 0,
                        budget: Optional[list] = None) -> None:
    """Bounded recursive walk collecting NodeReference-worthy ids into `refs`
    (a node_id -> NodeReference dict, so repeats within one row de-dup for
    free). `budget` is a shared single-element counter bounding total
    dict/list nodes visited across the whole walk."""
    if budget is None:
        budget = [0]
    if depth > _MAX_WALK_DEPTH or budget[0] >= _MAX_WALK_ITEMS:
        return
    if isinstance(value, dict):
        budget[0] += 1
        for key, val in value.items():
            if _is_id_key(key) and isinstance(val, str):
                ref = make_node_reference(val, source_type="neo4j_graph",
                                          **_sibling_names_for_id_key(value, key))
                if ref is not None:
                    refs.setdefault(ref.node_id, ref)
        for key, val in value.items():
            if isinstance(key, str) and (_is_external_id_key(key)
                                         or any(key == s or key.endswith("_" + s)
                                               for s in _ROW_ID_SUFFIXES)):
                continue  # never descend into an external-authority subtree
            if isinstance(val, (dict, list)):
                _walk_for_node_refs(val, refs, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            if budget[0] >= _MAX_WALK_ITEMS:
                break
            budget[0] += 1
            if isinstance(item, (dict, list)):
                _walk_for_node_refs(item, refs, depth + 1, budget)
            # scalar list items are never treated as ids — no key proves them.


def node_references_from_graph_row(row: dict) -> list:
    """Extract every distinct, validly-shaped node id in one graph result row
    — top-level scalars AND nested collect()/map structures — as
    NodeReferences. Complements `provenance_from_graph_row` (which builds one
    human-readable breadcrumb per row) by preserving EVERY id, including
    multiple ids of the same node class in a single row."""
    if not isinstance(row, dict):
        return []
    refs: dict = {}
    _walk_for_node_refs(row, refs)
    return list(refs.values())


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
            ev.node_references.extend(node_references_from_graph_row(row))
    ev.node_references = merge_node_references(ev.node_references)
    return ev

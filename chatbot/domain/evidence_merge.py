"""Entity de-duplication and cross-bundle collection.

책임: Entity 병합(강한 식별자 기준·이름만으로 병합 금지)과 evidence bundle
전반의 Entity/NodeReference 수집.
허용 의존성: 표준 라이브러리 + chatbot.domain.{node_identity, evidence_models}.
외부 부작용: 없음.
기존 facade: tools/evidence.py (모든 심볼 re-export).

Moved verbatim from tools/evidence.py (modularization work order Phase 2.3).
"""

from __future__ import annotations

from chatbot.domain.evidence_models import Entity, Evidence, merge_node_references
from chatbot.domain.node_identity import ENRICHABLE_TYPES


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
    for f in ("node_id", "node_type", "name_kor", "name_chi", "name_eng", "name_mr"):
        if not getattr(target, f) and getattr(other, f):
            setattr(target, f, getattr(other, f))
    for key, value in other.authority_ids.items():
        if value and not target.authority_ids.get(key):
            target.authority_ids[key] = value


def _copy_entity(e: Entity) -> Entity:
    return Entity(
        node_id=e.node_id, node_type=e.node_type, name_kor=e.name_kor,
        name_chi=e.name_chi, name_eng=e.name_eng, name_mr=e.name_mr,
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


def collect_node_references(*evidences: Evidence) -> list:
    """Gather and de-duplicate NodeReferences (ALL node classes) across
    evidence bundles — the all-node-class counterpart to `collect_entities`,
    used by the citation/body-linking layer rather than authority enrichment."""
    found: list = []
    for ev in evidences:
        if ev is None:
            continue
        found.extend(ev.node_references or [])
    return merge_node_references(found)

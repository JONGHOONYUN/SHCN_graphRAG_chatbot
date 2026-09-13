"""Evidence dataclasses — the retrieval → synthesis data contract.

책임: Entity / Provenance / NodeReference / Evidence 모델과 NodeReference
생성·병합. dataclass field, 기본값, to_dict() 결과는 이동 전과 동일.
허용 의존성: 표준 라이브러리 + chatbot.domain.node_identity.
외부 부작용: 없음 (type-mismatch 경고 로그만).
기존 facade: tools/evidence.py (모든 심볼 re-export).

Moved verbatim from tools/evidence.py (modularization work order Phase 2.1).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from chatbot.domain.node_identity import (
    is_valid_node_id,
    logger,
    node_type_for_id,
    poetrytalks_url,
)


@dataclass
class Entity:
    """A resolved entity (Person or Place) carrying authority identifiers.

    `authority_ids` maps a normalized registry key ('wikidata', 'aks_digerati',
    'loc', 'aks_map', …) to the ID stored on the Neo4j node.

    `name_mr` is the stored McCune–Reischauer romanization — the ONLY
    authoritative Latin-script form for Korean-related entities in this
    project (graph-ranking-reliability work order §2 rule 7). It is never
    derived, guessed, or backfilled from `name_eng`/`name_rr`; it is simply
    carried through verbatim from the `nameMR` graph property, or left `None`
    when the node has none."""

    node_id: Optional[str] = None
    node_type: Optional[str] = None
    name_kor: Optional[str] = None
    name_chi: Optional[str] = None
    name_eng: Optional[str] = None
    name_mr: Optional[str] = None
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
    work_name_mr: Optional[str] = None
    entry_position: Any = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NodeReference:
    """A citable reference to ANY Neo4j node — not scoped to Person/Place.

    `Entity` exists for external-authority enrichment (Person/Place only,
    carries `authority_ids`). `NodeReference` is the parallel, more general
    concept that guarantees every node class in the schema (Work, Entry, Poem,
    Critique, Person, Place, Topic, Era, CriticalTerm) can be linked and cited
    — Person/Place nodes legitimately produce BOTH an Entity and a
    NodeReference; they serve different downstream purposes.

    `node_id` is validated at construction time (`make_node_reference`) —
    never fabricate one from a name or an external identifier."""

    node_id: str
    node_type: str
    name_kor: Optional[str] = None
    name_chi: Optional[str] = None
    name_eng: Optional[str] = None
    name_mr: Optional[str] = None
    source_type: Optional[str] = None
    work_id: Optional[str] = None
    entry_id: Optional[str] = None

    def url(self) -> Optional[str]:
        return poetrytalks_url(self.node_id)

    def display_name(self) -> str:
        return self.name_kor or self.name_chi or self.name_eng or self.node_id

    def to_dict(self) -> dict:
        return asdict(self)


def make_node_reference(
    node_id: Any, *, node_type: Optional[str] = None,
    source_type: Optional[str] = None,
    work_id: Optional[str] = None, entry_id: Optional[str] = None,
    name_kor: Optional[str] = None, name_chi: Optional[str] = None,
    name_eng: Optional[str] = None, name_mr: Optional[str] = None,
) -> Optional["NodeReference"]:
    """Construct a NodeReference, or None if `node_id` is not a valid,
    registered-prefix node id — this is the ONLY validation gate; callers
    never need to pre-check `is_valid_node_id` themselves.

    The node TYPE is always taken from the id itself (`node_type_for_id`); a
    caller-supplied `node_type` that disagrees is logged and overridden, never
    trusted blindly (guards against a mislabeled role in a Cypher alias)."""
    inferred = node_type_for_id(node_id)
    if inferred is None:
        return None
    if node_type and node_type != inferred:
        logger.warning(
            "node reference type mismatch for %r: claimed=%r inferred=%r — using inferred",
            node_id, node_type, inferred,
        )
    return NodeReference(
        node_id=node_id.strip(), node_type=inferred,
        name_kor=name_kor, name_chi=name_chi, name_eng=name_eng,
        name_mr=name_mr, source_type=source_type,
        work_id=work_id if is_valid_node_id(work_id) else None,
        entry_id=entry_id if is_valid_node_id(entry_id) else None,
    )


def merge_node_references(refs: Any) -> list:
    """De-duplicate NodeReferences strictly by `node_id` — the only identity a
    NodeReference has. Distinct ids are NEVER merged just because a name
    matches; homonym ambiguity is resolved (by refusing to auto-link) at
    body-linking time, not here. Fills in missing name/source fields from
    later duplicates; preserves first-seen order."""
    order: list = []
    by_id: dict = {}
    for r in refs or []:
        if r is None:
            continue
        if r.node_id not in by_id:
            by_id[r.node_id] = NodeReference(**asdict(r))
            order.append(r.node_id)
        else:
            existing = by_id[r.node_id]
            for f in ("node_type", "name_kor", "name_chi", "name_eng",
                     "name_mr", "source_type", "work_id", "entry_id"):
                if not getattr(existing, f) and getattr(r, f):
                    setattr(existing, f, getattr(r, f))
    return [by_id[i] for i in order]


@dataclass
class Evidence:
    """A bundle of evidence from a single retrieval source."""

    kind: str
    claims: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    provenance: list = field(default_factory=list)
    # Every node class (Work/Entry/Poem/Critique/Person/Place/Topic/Era/
    # CriticalTerm) referenced in this evidence bundle, for citation coverage.
    node_references: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "claims": list(self.claims),
            "entities": [e.to_dict() for e in self.entities],
            "documents": list(self.documents),
            "provenance": [p.to_dict() for p in self.provenance],
            "node_references": [r.to_dict() for r in self.node_references],
        }

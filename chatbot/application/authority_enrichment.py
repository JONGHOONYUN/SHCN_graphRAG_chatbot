"""Authority enrichment service — capped, registry-driven, link-only aware.

책임: 한 Entity의 registry-eligible authority fetch(중복·cap 준수), fetch/
link-only 결과의 external Evidence 기록.
허용 의존성: chatbot.domain + chatbot.authority.registry +
chatbot.application.retriever_invocation.
외부 부작용: 주입된 fetcher 호출 (HTTP는 fetcher 소유).
기존 facade: tools/orchestrator.py.

Moved verbatim from tools/orchestrator.py (modularization work order Phase 7.3).
"""

from __future__ import annotations

from typing import Callable

from chatbot.application.retriever_invocation import _call_fetcher
from chatbot.authority.registry import (
    CAPABILITY_FETCHABLE,
    CAPABILITY_LINK_ONLY,
    sources_for_node_type,
)
from chatbot.domain.evidence_models import Entity, Evidence, Provenance


def _has_valid_fetchable_id(entity: Entity, node_type: str) -> bool:
    """True when the entity carries at least one registry-valid fetchable ID for
    its node type (link-only IDs alone do not make it cap-eligible)."""
    for cfg in sources_for_node_type(node_type, capability=CAPABILITY_FETCHABLE):
        ext_id = entity.authority_ids.get(cfg.id_key)
        if ext_id and cfg.validate_id(ext_id):
            return True
    return False


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
        from chatbot.authority.registry import link_only_reference

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

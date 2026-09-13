"""Compatibility facade — structured evidence data contract.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/domain/node_identity.py     node ID prefix registry · URL 규칙
    chatbot/domain/evidence_models.py   Entity/Provenance/NodeReference/Evidence
    chatbot/domain/evidence_merge.py    entity 병합 · cross-bundle 수집
    chatbot/retrieval/vector_documents.py  vector metadata/document → Evidence
    chatbot/retrieval/graph_rows.py        graph result row → Evidence

이 모듈은 기존 import 경로(`from tools.evidence import ...`)를 보존하는
re-export만 수행한다 — 로직·상태·부작용 없음. 밑줄 헬퍼도 기존 테스트/내부
호출자가 import하므로 단일 구현을 그대로 re-export한다 (복제 금지, §11.4).
경고 로그의 logger 이름("tools.evidence")은 이동한 구현 쪽에서 유지된다.
"""

from chatbot.domain.node_identity import (  # noqa: F401
    ENRICHABLE_TYPES,
    ENTITY_TYPES,
    EVIDENCE_KINDS,
    NODE_ID_PREFIXES,
    POETRYTALKS_BASE,
    POETRYTALKS_BASE_URL,
    POETRYTALKS_WIKIDATA_LABEL,
    SOURCE_TYPES,
    _linked_id,
    _NODE_ID_RE,
    is_valid_node_id,
    logger,
    node_type_for_id,
    normalize_entry_position,
    poetrytalks_url,
    split_node_id,
)
from chatbot.domain.evidence_models import (  # noqa: F401
    Entity,
    Evidence,
    NodeReference,
    Provenance,
    make_node_reference,
    merge_node_references,
)
from chatbot.domain.evidence_merge import (  # noqa: F401
    _copy_entity,
    _entities_match,
    _merge_into,
    collect_entities,
    collect_node_references,
    collect_person_entities,
    merge_entities,
)
from chatbot.retrieval.vector_documents import (  # noqa: F401
    _clean_ids,
    _doc_meta_and_page,
    _normalize_place_authority,
    _person_from_flat,
    _place_from_flat,
    docs_to_evidence,
    document_to_parts,
    entities_from_vector_meta,
    node_references_from_vector_meta,
    person_entities_from_vector_meta,
)
from chatbot.retrieval.graph_rows import (  # noqa: F401
    _ID_PREFIX_TO_KIND,
    _LEGACY_SIBLING_NAME_KEYS,
    _MAX_WALK_DEPTH,
    _MAX_WALK_ITEMS,
    _ROW_ID_SUFFIXES,
    _allowed_authority_keys,
    _is_external_id_key,
    _is_id_key,
    _looks_like_node_id,
    _row_entity,
    _sibling_names_for_id_key,
    _walk_for_node_refs,
    entities_from_graph_row,
    graph_rows_to_evidence,
    node_references_from_graph_row,
    provenance_from_graph_row,
)

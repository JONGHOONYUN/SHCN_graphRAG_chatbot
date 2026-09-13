"""Compatibility facade — graphRAG evidence orchestrator.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/application/retrieval_policy.py       intent cue · cap 설정
    chatbot/application/retriever_invocation.py   arity dispatch · user-safe status
    chatbot/application/authority_enrichment.py   entity별 authority fetch/기록
    chatbot/application/evidence_orchestrator.py  graph → vector → authority 조립

이 모듈은 기존 import 경로(`from tools.orchestrator import ...`)를 보존하는
re-export만 수행한다 — 로직·상태·부작용 없음. `logger` 이름은 구현 쪽에서
계속 "tools.orchestrator"를 사용하므로 기존 assertLogs/운영 로그가 그대로
동작한다.
"""

from chatbot.application.retrieval_policy import (  # noqa: F401
    DEFAULT_FETCHABLE_SOURCES_PER_ENTITY,
    DEFAULT_PERSON_AUTHORITY_CAP,
    DEFAULT_PERSON_CAP,
    DEFAULT_PLACE_AUTHORITY_CAP,
    DEFAULT_PLACE_CAP,
    DEFAULT_SOURCES_PER_ENTITY,
    EXHAUSTIVE_SOURCES_PER_ENTITY,
    _COMPARE_CUES,
    _PERSON_CUES,
    _PLACE_CUES,
    _RANKING_EXTERNAL_CUES,
    _config_cache,
    _config_int,
    _extract_ranking_winner_ids,
    _matches,
    _ranking_authority_intent,
    authority_intent,
    needs_authority,
)
from chatbot.application.retriever_invocation import (  # noqa: F401
    _call_fetcher,
    _fn_accepts_arity,
    _normalize_evidence_status,
    _safe_retrieve,
    logger,
)
from chatbot.application.authority_enrichment import (  # noqa: F401
    _enrich_entity,
    _has_valid_fetchable_id,
    _record_authority_result,
    _record_link_only,
)
from chatbot.application.evidence_orchestrator import (  # noqa: F401
    _default_authority_fetcher,
    _default_graph_retriever,
    _default_role_ranking_retriever,
    _default_vector_retriever,
    gather_graphrag_evidence,
)

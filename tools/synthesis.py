"""Compatibility facade — final-synthesis helpers for the graphRAG pipeline.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/synthesis/language_fields.py  언어별 이름 선택·source-text 순서
    chatbot/synthesis/source_policy.py    SYNTHESIS_SYSTEM_RULES (source/conflict)
    chatbot/synthesis/history_format.py   대화 이력 직렬화 + HISTORY_RULES
    chatbot/synthesis/evidence_format.py  evidence block 포맷 + 예산 + status
    chatbot/synthesis/citations.py        결정론적 citation 생성 + CITATION_LABELS

이 모듈은 기존 import 경로(`from tools.synthesis import ...`)를 보존하는
re-export만 수행한다 — 로직·상태·부작용 없음.

Single-source notes:
  * `_MD_LINK_ID_RE`는 chatbot/synthesis/citations.py에서 단 한 번,
    re.escape(POETRYTALKS_BASE_URL) 기반으로 구성된다 — 여기에는 사본이 없다.
  * base URL 상수는 여전히 tools.evidence(→ chatbot.domain.node_identity)가
    단일 소유자다.
"""

from tools.evidence import POETRYTALKS_BASE_URL as _PTW_BASE  # noqa: F401  (single source)

from chatbot.synthesis.language_fields import (  # noqa: F401
    _BARE_TEXT_FIELD_LANG,
    _LANG_TO_BARE_TEXT_FIELD,
    _PLACEHOLDER_RE,
    _ROLE_TEXT_FIELD_RE,
    _SOURCE_TEXT_PRIORITY,
    _family_member_key,
    _label_is_clean,
    _pick_by_language,
    _text_field_family,
    _work_name_bilingual,
    reorder_source_text_fields,
    source_text_priority,
)
from chatbot.synthesis.source_policy import SYNTHESIS_SYSTEM_RULES  # noqa: F401
from chatbot.synthesis.history_format import (  # noqa: F401
    HISTORY_MAX_MESSAGE_CHARS,
    HISTORY_MAX_MESSAGES,
    HISTORY_MAX_TOTAL_CHARS,
    HISTORY_RULES,
    _HISTORY_EXCLUDE_MARKERS,
    _history_content,
    _history_role,
    serialize_chat_history,
)
from chatbot.synthesis.evidence_format import (  # noqa: F401
    BOTH_RETRIEVALS_FAILED_MESSAGES,
    EVIDENCE_BLOCK_FORMATTERS,
    EVIDENCE_BLOCK_ORDER,
    RETRIEVAL_OUTCOMES,
    RETRIEVAL_STATUS_MESSAGES,
    _COVERAGE_TEMPLATES,
    _MAX_BLOCK_CHARS,
    _MAX_DOCS,
    _MAX_EXTERNAL_CLAIMS,
    _MAX_FIELD_CHARS,
    _MAX_TOTAL_CHARS,
    _allowlist_for,
    _entity_line,
    _format_coverage_block,
    _format_external_block,
    _format_graph_block,
    _format_status_block,
    _format_vector_block,
    _to_dict,
    _truncate,
    both_retrievals_failed,
    format_evidence_for_prompt,
    retrieval_failure_message,
)
from chatbot.synthesis.citations import (  # noqa: F401
    CITATION_LABELS,
    _MD_LINK_ID_RE,
    _collect_all_node_ids,
    _collect_node_names,
    _format_citation_name,
    _prov_dedup_key,
    _rebuild_vector_prov_label,
    build_citations,
)

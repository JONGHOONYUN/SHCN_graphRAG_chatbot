"""Evidence block formatting + prompt budgeting for the final synthesis prompt.

책임: source별(graph/vector/external) evidence block, retrieval status,
authority coverage 렌더링과 per-block/total 문자 예산 집행.
허용 의존성: 표준 라이브러리 + chatbot.domain + chatbot.synthesis.language_fields
(+ allowlist 조회를 위한 chatbot.authority.registry 지연 import).
외부 부작용: 없음.
기존 facade: tools/synthesis.py.

Moved verbatim from tools/synthesis.py (modularization work order Phase 3.3);
block 순서·per-block cap·total cap 불변. 확장 경계(§3.5): source별 block
formatter는 `EVIDENCE_BLOCK_FORMATTERS` dispatch에 등록된다 — 현재 소스
(graph/vector/external)만 등록하며 미래 소스 이름은 추가하지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

from chatbot.domain.node_identity import (
    is_valid_node_id,
    normalize_entry_position,
    poetrytalks_url,
)
from chatbot.synthesis.language_fields import (
    _work_name_bilingual,
    reorder_source_text_fields,
    source_text_priority,
)

_MAX_BLOCK_CHARS = 4000
_MAX_TOTAL_CHARS = 14000
_MAX_DOCS = 8
_MAX_EXTERNAL_CLAIMS = 10
_MAX_FIELD_CHARS = 1200


# ── User-safe retrieval status (work order §3) ────────────────────────────────
# Outcomes are stable tokens; localized wording lives here, technical
# diagnostics live only in server logs (see the orchestration layer).
RETRIEVAL_OUTCOMES = ("ok", "no_results", "temporarily_unavailable", "invalid_query")

RETRIEVAL_STATUS_MESSAGES = {
    ("graph", "temporarily_unavailable"): {
        "ko": "그래프 구조 검색은 현재 일시적으로 사용할 수 없습니다. 다른 검색 결과는 계속 참고했습니다.",
        "en": "Graph (structural) search is temporarily unavailable. Other retrieval results were still used.",
        "zh": "图结构检索暂时不可用。其他检索结果仍被参考。",
    },
    ("graph", "no_results"): {
        "ko": "그래프 검색에서 일치하는 결과를 찾지 못했습니다.",
        "en": "Graph search found no matching results.",
        "zh": "图检索未找到匹配结果。",
    },
    ("graph", "invalid_query"): {
        "ko": "질문을 그래프 검색으로 해석하지 못했습니다. 인물명·서명 등을 조금 더 구체적으로 적어 주세요.",
        "en": "The question could not be interpreted as a graph query. Please be more specific (names, titles).",
        "zh": "无法将问题解释为图查询。请提供更具体的信息（人名、书名等）。",
    },
    ("vector", "temporarily_unavailable"): {
        "ko": "텍스트(벡터) 검색은 현재 일시적으로 사용할 수 없습니다. 그래프 검색 결과는 계속 참고했습니다.",
        "en": "Text (vector) search is temporarily unavailable. Graph results were still used.",
        "zh": "文本（向量）检索暂时不可用。图检索结果仍被参考。",
    },
    ("vector", "no_results"): {
        "ko": "텍스트 검색에서 일치하는 결과를 찾지 못했습니다.",
        "en": "Text search found no matching results.",
        "zh": "文本检索未找到匹配结果。",
    },
}


# Returned directly (no LLM call) when BOTH retrieval sources failed and there is
# no external evidence to answer from. Never backfilled from pretraining.
BOTH_RETRIEVALS_FAILED_MESSAGES = {
    "ko": "죄송합니다. 지금은 그래프 검색과 텍스트 검색이 모두 일시적으로 사용할 수 없습니다. "
          "잠시 후 다시 시도하거나, 질문을 조금 바꿔서 다시 물어봐 주세요.",
    "en": "Sorry — both graph search and text search are temporarily unavailable. "
          "Please try again shortly or rephrase your question.",
    "zh": "抱歉，图检索与文本检索目前均暂时不可用。请稍后重试或换个问法。",
}


def both_retrievals_failed(statuses: dict) -> bool:
    """True when graph AND vector retrieval are unavailable (not mere
    no_results — an empty result set is an answerable state)."""
    if not isinstance(statuses, dict):
        return False
    g = (statuses.get("graph") or {}).get("outcome")
    v = (statuses.get("vector") or {}).get("outcome")
    return g == "temporarily_unavailable" and v == "temporarily_unavailable"


def retrieval_failure_message(language: str = "ko") -> str:
    return BOTH_RETRIEVALS_FAILED_MESSAGES.get(
        language, BOTH_RETRIEVALS_FAILED_MESSAGES["ko"])


def _allowlist_for(source: str) -> tuple:
    """Per-source parsed-field allowlist, from the authority registry."""
    try:
        from chatbot.authority.registry import AUTHORITY_REGISTRY, resolve_source

        cfg = AUTHORITY_REGISTRY.get(source) or resolve_source(source)
        if cfg is not None and cfg.allowed_fields:
            return cfg.allowed_fields
    except Exception:
        pass
    return ()


def _truncate(text: str, limit: int = _MAX_BLOCK_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def _entity_line(e: dict) -> str:
    """Render one resolved-entity line. The node_id is embedded as a markdown
    link to its Poetry Talks wiki page (deterministic — same URL scheme every
    referenced graph node uses) so the LLM can weave clickable references into
    the answer without reconstructing URLs itself."""
    name = (
        e.get("name_kor") or e.get("name_eng") or e.get("name_chi")
        or e.get("node_id") or "?"
    )
    bits = [f"{name} [{e.get('node_type') or 'Person'}]"]
    node_id = e.get("node_id")
    if node_id:
        url = poetrytalks_url(node_id)
        bits.append(f"id=[{node_id}]({url})" if url else f"id={node_id}")
    if e.get("name_mr"):
        bits.append(f"MR={e['name_mr']}")
    for key, value in sorted((e.get("authority_ids") or {}).items()):
        if value:
            bits.append(f"{key}={value}")
    return "- " + ", ".join(bits)


def _format_graph_block(graph: dict, outcome: str = "ok",
                        language: str = "ko") -> str:
    """Graph rows only. Error/status claims are NEVER rendered here — raw
    exception text must not reach the final prompt (work order §3); the
    localized status line is emitted by _format_status_block instead.

    Each row is reordered (never mutated or altered in value) via
    `reorder_source_text_fields` so parallel source-text fields — top-level
    or nested inside collect()/map results — present in the locked response
    language's priority order regardless of the Cypher RETURN key order or
    Python dict insertion order (work order §3.1)."""
    lines = ["## Graph Evidence (Neo4j — authoritative for the sihwa corpus)"]
    docs = graph.get("documents", [])
    if not docs:
        if outcome == "temporarily_unavailable":
            lines.append("(graph retrieval unavailable this turn — see Retrieval Status)")
        else:
            lines.append("(no graph rows)")
    for row in docs[:_MAX_DOCS]:
        ordered_row = reorder_source_text_fields(row, language)
        lines.append("- " + _truncate(json.dumps(ordered_row, ensure_ascii=False), 800))
    return _truncate("\n".join(lines))


def _format_vector_block(vector: dict, outcome: str = "ok",
                          language: str = "ko") -> str:
    lines = ["## Vector Evidence (Neo4j text retrieval — authoritative source texts)"]
    docs = vector.get("documents", [])
    if not docs:
        if outcome == "temporarily_unavailable":
            lines.append("(vector retrieval unavailable this turn — see Retrieval Status)")
        else:
            lines.append("(no vector documents)")
    for d in docs[:_MAX_DOCS]:
        # Pick the work name matching the locked response language and, when
        # the answer isn't Korean, append the Korean original in parens so
        # non-Korean readers can still cross-reference the original title.
        work = _work_name_bilingual(d, language) or "Work"
        # NOTE: "source:" is a language-neutral evidence-block metadata label —
        # kept in English so the LLM does not mirror a Korean prefix into the
        # user-facing Sources section. Final citation labels are enforced by
        # CITATION_LABELS + rule 9 in SYNTHESIS_SYSTEM_RULES.
        # Invalid/absent entry ids or positions are OMITTED (never `Entry None`
        # / `Entry 0` — the LLM must not see placeholder shapes it could copy).
        head = f"- source: {work}"
        eid = d.get("entry_id") if is_valid_node_id(d.get("entry_id")) else None
        pos = normalize_entry_position(d.get("entry_position"))
        if eid and pos:
            head += f" > Entry {pos} ({eid})"
        elif eid:
            head += f" > Entry ({eid})"
        elif pos:
            head += f" > Entry {pos}"
        lines.append(head)
        for f in source_text_priority(language) + ("descEng",):
            if d.get(f):
                lines.append(f"    {f}: {d[f]}")
        if d.get("poetrytalks_link"):
            lines.append(f"    link: {d['poetrytalks_link']}")
    return _truncate("\n".join(lines))


def _format_external_block(external: dict) -> str:
    lines = [
        "## External Authority Evidence",
        "(status=ok → fetched, supplementary. status=link_only → NOT fetched, "
        "reference link only. Other statuses → unavailable; do not fill gaps.)",
    ]
    claims = [c for c in external.get("claims", [])
              if c.get("type") != "coverage"]      # rendered in its own block
    if not claims:
        lines.append("(no external authority data)")

    for c in claims[:_MAX_EXTERNAL_CLAIMS]:
        src = c.get("source")
        label = c.get("source_label") or src
        status = c.get("status")
        who = c.get("entity") or c.get("person")
        ntype = c.get("node_type") or "Person"

        if status == "link_only":
            lines.append(
                f"- {label} for {who} [{ntype}]: LINK-ONLY (not fetched) — "
                f"present as a reference link only, do NOT assert its "
                f"contents. url: {c.get('url')}"
            )
            continue
        if status != "ok":
            lines.append(
                f"- {label} for {who} [{ntype}]: UNAVAILABLE ({status}) — "
                "do not use pretraining to fill this gap."
            )
            continue

        data = c.get("data") or {}
        allow = _allowlist_for(src)
        filtered = (
            {k: data[k] for k in allow if data.get(k) not in (None, [], {})}
            if allow else {}
        )
        lines.append(f"- {label} for {who} [{ntype}] (status=ok, FETCHED):")
        lines.append("    " + _truncate(json.dumps(filtered, ensure_ascii=False), _MAX_FIELD_CHARS))
        # Carry the source's own anti-hallucination guardrails through.
        if data.get("MUST_NOT_ADD"):
            lines.append(
                "    MUST_NOT_ADD: "
                + _truncate(json.dumps(data["MUST_NOT_ADD"], ensure_ascii=False), 500)
            )
        if c.get("url"):
            lines.append(f"    url: {c['url']}")
    return _truncate("\n".join(lines))


def _format_status_block(statuses: dict, language: str) -> str:
    """Localized, user-safe retrieval status lines. Only non-ok outcomes are
    shown; 'no_results' and 'temporarily_unavailable' render differently."""
    if not isinstance(statuses, dict):
        return ""
    lines = []
    for source in ("graph", "vector"):
        outcome = (statuses.get(source) or {}).get("outcome")
        if not outcome or outcome == "ok":
            continue
        msgs = RETRIEVAL_STATUS_MESSAGES.get((source, outcome))
        if msgs:
            lines.append(f"- {msgs.get(language, msgs['ko'])}")
    if not lines:
        return ""
    return "\n".join(
        ["## Retrieval Status (사용자에게 관련 내용을 전달할 것)"] + lines)


_COVERAGE_TEMPLATES = {
    "Person": {
        "ko": "외부 authority 보강은 관련 인물 {eligible}명 중 {enriched}명에 적용했습니다. "
              "나머지 인물은 시화총림 그래프 정보만으로 제시했습니다.",
        "en": "External authority enrichment was applied to {enriched} of {eligible} "
              "relevant persons; the rest are presented from graph data only.",
        "zh": "外部权威数据补充应用于{eligible}位相关人物中的{enriched}位；其余人物仅基于图数据呈现。",
    },
    "Place": {
        "ko": "외부 authority 보강은 관련 장소 {eligible}곳 중 {enriched}곳에 적용했습니다. "
              "나머지 장소는 시화총림 그래프 정보만으로 제시했습니다.",
        "en": "External authority enrichment was applied to {enriched} of {eligible} "
              "relevant places; the rest are presented from graph data only.",
        "zh": "外部权威数据补充应用于{eligible}处相关地点中的{enriched}处；其余地点仅基于图数据呈现。",
    },
}


def _format_coverage_block(coverage: dict, language: str) -> str:
    """Cap/truncation transparency (work order §4): shown ONLY when at least one
    eligible entity was skipped due to a cap. The synthesis rules require this
    statement to appear in the final answer."""
    if not isinstance(coverage, dict):
        return ""
    lines = []
    for node_type in ("Person", "Place"):
        c = coverage.get(node_type) or {}
        if not c.get("skipped_due_to_cap_count"):
            continue
        tmpl = _COVERAGE_TEMPLATES[node_type]
        lines.append("- " + tmpl.get(language, tmpl["ko"]).format(
            eligible=c.get("eligible_entity_count", 0),
            enriched=c.get("enriched_entity_count", 0)))
    if not lines:
        return ""
    return "\n".join(
        ["## Authority Coverage (답변에 반드시 포함할 것 — 완전한 목록이라고 주장 금지)"]
        + lines)


def _to_dict(ev: Any) -> dict:
    """Accept either an Evidence object or an already-serialized dict."""
    if ev is None:
        return {}
    if hasattr(ev, "to_dict"):
        return ev.to_dict()
    if isinstance(ev, dict):
        return ev
    return {}


# ── Extension boundary (work order §3.5) ─────────────────────────────────────
# Per-source block formatters, keyed by evidence source. A future source is
# added by REGISTERING here (and in EVIDENCE_BLOCK_ORDER) instead of editing
# `format_evidence_for_prompt`'s body. Only the CURRENT sources are
# registered — no speculative future names. Each formatter takes the
# already-serialized bundle dict, the statuses dict, and the response
# language, and returns one bounded text block.
EVIDENCE_BLOCK_FORMATTERS = {
    "graph": lambda bundle, statuses, language: _format_graph_block(
        bundle, (statuses.get("graph") or {}).get("outcome", "ok"), language),
    "vector": lambda bundle, statuses, language: _format_vector_block(
        bundle, (statuses.get("vector") or {}).get("outcome", "ok"), language),
    "external": lambda bundle, statuses, language: _format_external_block(bundle),
}

# Presentation order of the per-source blocks — unchanged from the pre-split
# hard-coded sequence.
EVIDENCE_BLOCK_ORDER = ("graph", "vector", "external")


def format_evidence_for_prompt(evidence: dict, language: str = "ko") -> str:
    """Render the evidence bundle into labelled, bounded blocks for the final
    synthesis prompt. Guarantees source labels are present, that only allowlisted
    parsed fields appear, and that per-block and total size caps hold."""
    bundles = {key: _to_dict(evidence.get(key)) for key in EVIDENCE_BLOCK_ORDER}
    graph = bundles["graph"]
    vector = bundles["vector"]
    statuses = evidence.get("statuses") or {}
    coverage = evidence.get("coverage") or {}

    entities = graph.get("entities", []) + vector.get("entities", [])
    ent_lines = ["## Resolved Entities (Person / Place)"]
    if entities:
        seen = set()
        for e in entities:
            key = e.get("node_id") or json.dumps(e.get("authority_ids") or {}, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            ent_lines.append(_entity_line(e))
    else:
        ent_lines.append("(none resolved)")

    parts = ["\n".join(ent_lines)]
    for key in EVIDENCE_BLOCK_ORDER:
        parts.append(EVIDENCE_BLOCK_FORMATTERS[key](bundles[key], statuses, language))
    status_block = _format_status_block(statuses, language)
    if status_block:
        parts.append(status_block)
    coverage_block = _format_coverage_block(coverage, language)
    if coverage_block:
        parts.append(coverage_block)
    return _truncate("\n\n".join(parts), _MAX_TOTAL_CHARS)

"""Deterministic citation building — source-separated, verified URLs only.

책임: "poetrytalks wikidata" 그룹, graph/vector provenance breadcrumb,
external fetched/link-only citation과 구조화된 de-duplication. 본문 링크
삽입·최종 조립의 단일 소유권은 tools/answer_renderer.py에 있다 (여기서는
citation 목록만 만든다).
허용 의존성: 표준 라이브러리 + chatbot.domain + chatbot.synthesis.language_fields.
외부 부작용: 없음.
기존 facade: tools/synthesis.py.

Moved verbatim from tools/synthesis.py (modularization work order Phase 3.4).
"""

from __future__ import annotations

import re as _re
from typing import Any, Optional

from chatbot.domain.node_identity import (
    POETRYTALKS_BASE_URL as _PTW_BASE,
    _linked_id,
    is_valid_node_id,
    normalize_entry_position,
    poetrytalks_url,
)
from chatbot.synthesis.evidence_format import _to_dict
from chatbot.synthesis.language_fields import _label_is_clean, _work_name_bilingual

# ── Localized citation labels (matches locked response language) ─────────────
# The final answer's "Sources" section must be written in the user's language,
# not always Korean. These labels are used by (a) build_citations() when
# pre-composing suggested citation lines and (b) the system-rule text in
# chatbot/synthesis/source_policy.py, which shows the LLM the exact
# per-language label set to use.
#
# `poetrytalks_wikidata_prefix` is the canonical group label for URLs of the
# form `https://poetrytalks.org/<node_id>` — the deterministic reference URL
# for EVERY graph node (Person, Entry, Poem, Critique, Work, Place, Topic,
# Era, CriticalTerm, ...). By explicit user policy this group MUST appear in
# every response that cites any graph node, using exactly the proper name
# "poetrytalks wikidata" across all languages (no translation of the name).
CITATION_LABELS = {
    "ko": {
        "sources_header":            "출처",
        "poetrytalks_wikidata_prefix": "poetrytalks wikidata",
        "graph_prefix":              "시화총림 그래프",
        "link_only_prefix":          "참고 링크 (내용 미조회)",
    },
    "en": {
        "sources_header":            "Sources",
        "poetrytalks_wikidata_prefix": "poetrytalks wikidata",
        "graph_prefix":              "Sihwa Ch'ongnim Graph",
        "link_only_prefix":          "Reference Link (not fetched)",
    },
    "zh": {
        "sources_header":            "来源",
        "poetrytalks_wikidata_prefix": "poetrytalks wikidata",
        "graph_prefix":              "诗话丛林图谱",
        "link_only_prefix":          "参考链接（内容未获取）",
    },
}


def _rebuild_vector_prov_label(prov: dict, language: str) -> str:
    """Render one vector-provenance line in the locked response language.

    Validity policy (work order Phase 2/3):
      * a Poetry Talks link is rendered only for a shape-valid internal id;
      * position renders only when it normalizes to a positive int;
      * valid entry → `Work [B023](url) > Entry 31 [E031](url)` (slash-free
        square links, no `)(` double parens);
      * valid work only → work-only citation;
      * neither valid id → fall back to the retrieval-time `label` ONLY when
        it is placeholder-free; otherwise return '' so the caller skips it.
    """
    work_id = prov.get("work_id")
    entry_id = prov.get("entry_id")
    work_ok = is_valid_node_id(work_id)
    entry_ok = is_valid_node_id(entry_id)
    pos = normalize_entry_position(prov.get("entry_position"))
    work_name = _work_name_bilingual(prov, language)

    if not (work_ok or entry_ok):
        label = prov.get("label") or ""
        return label if _label_is_clean(label) else ""

    parts = []
    if work_name and work_ok:
        parts.append(f"{work_name} {_linked_id(work_id)}")
    elif work_name:
        parts.append(work_name)
    elif work_ok:
        parts.append(_linked_id(work_id))
    if entry_ok:
        parts.append(
            f"Entry {pos} {_linked_id(entry_id)}" if pos
            else f"Entry {_linked_id(entry_id)}"
        )
    return " > ".join(p for p in parts if p)


# ── Named "poetrytalks wikidata" citations (work order:
# CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §3.2/Phase 3) ──────
def _collect_node_names(graph: dict, vector: dict) -> dict:
    """node_id -> {"name_kor", "name_chi", "name_eng"}, merged across BOTH
    evidence sources for citation-name display.

    Priority (work order Phase 3 item 2): `node_references` (the
    all-node-class inventory) is the primary source; `entities` (Person/
    Place only) fills in a field ONLY when node_references left it empty.
    Never overwrites an already-populated field with a different value —
    first-seen non-empty wins, mirroring `merge_node_references()`'s policy
    exactly, just operating on the already-serialized dict shape `graph`/
    `vector` carry by the time `build_citations` runs."""
    names: dict = {}

    def _fill(node_id, name_kor, name_chi, name_eng):
        if not node_id:
            return
        slot = names.setdefault(
            node_id, {"name_kor": None, "name_chi": None, "name_eng": None})
        for field_name, val in (("name_kor", name_kor), ("name_chi", name_chi),
                               ("name_eng", name_eng)):
            if not slot[field_name] and val:
                slot[field_name] = val

    # Primary: node_references (covers every node class).
    for ref in graph.get("node_references", []) + vector.get("node_references", []):
        if isinstance(ref, dict):
            _fill(ref.get("node_id"), ref.get("name_kor"),
                 ref.get("name_chi"), ref.get("name_eng"))
    # Secondary: entities (Person/Place) — fills gaps only.
    for ent in graph.get("entities", []) + vector.get("entities", []):
        if isinstance(ent, dict):
            _fill(ent.get("node_id"), ent.get("name_kor"),
                 ent.get("name_chi"), ent.get("name_eng"))
    return names


def _format_citation_name(name_kor: Optional[str], name_eng: Optional[str],
                          name_chi: Optional[str], language: str) -> str:
    """Render the ' — <primary> (<secondary>)' suffix for one "poetrytalks
    wikidata" bullet (work order §3.2). Returns '' when no usable name
    exists — the caller then keeps the plain ID-only bullet (rule 6).

    Rules (work order §3.2 name-fallback list):
      1. en: nameEng primary; a DIFFERENT nameKor appends in parens.
      2. ko: nameKor primary; a DIFFERENT nameEng appends in parens.
      3. zh: nameChi primary (existing zh convention); nameKor/nameEng are
         still preserved as the secondary rather than dropped outright.
      4. Only one of the pair present → show that one alone.
      5. Trimmed-equal pair → show once, no parenthetical repeat.
      6. Neither present → '' (ID-only fallback; never fabricated).
      7. nameMR/nameChi never substitutes for a missing nameEng in the
         en/ko cases — only the zh case is allowed to prioritize nameChi."""
    kor = (name_kor or "").strip() or None
    eng = (name_eng or "").strip() or None
    chi = (name_chi or "").strip() or None

    if language == "zh":
        primary = chi or kor or eng
        if not primary:
            return ""
        secondary = kor if kor and kor != primary else (
            eng if eng and eng != primary else None)
        return f" — {primary} ({secondary})" if secondary else f" — {primary}"

    if language == "en":
        primary, secondary = eng, kor
    else:  # ko, or an unsupported/unknown language — matches the project's
        primary, secondary = kor, eng   # existing ko-default fallback policy.

    if not primary:
        primary, secondary = secondary, None
    if not primary:
        return ""
    if secondary and secondary == primary:
        secondary = None
    return f" — {primary} ({secondary})" if secondary else f" — {primary}"


def build_citations(evidence: dict, language: str = "ko",
                    referenced_node_ids: Optional[list] = None) -> list:
    """Build source-separated citation lines from provenance/claims.

    Every referenced graph node — regardless of class — carries a canonical
    `https://poetrytalks.org/<id>` URL. Those URLs are collected into a
    single **"poetrytalks wikidata"** group that MUST appear whenever any
    graph node is cited. The group name is a proper noun and is NOT
    translated between languages.

    `referenced_node_ids` (work order Phase 4, optional for backward
    compatibility): when given, restricts the "poetrytalks wikidata" group to
    ONLY these ids — the ones the finished answer actually mentions or relies
    on — instead of every id merely retrieved into evidence. When `None`
    (legacy default, and every pre-existing caller), the group includes every
    node id found anywhere in the evidence, exactly as before.

    Work/Entry/Poem/Critique PROVENANCE breadcrumbs are intentionally NOT
    filtered by `referenced_node_ids`: every such breadcrumb corresponds to a
    document that was actually retrieved and placed in the evidence blocks
    the LLM was given, so its source citation must remain available even if
    the model's prose didn't literally repeat the id — dropping it would
    regress the "answers must be citable" guarantee for a much smaller (and
    much riskier) gain than filtering the entity-mention group.

    Never fabricates a link — only emits URLs derivable from ids present in
    the evidence. Graph and link-only group labels come from
    CITATION_LABELS[language]. External authority proper names (Wikidata,
    AKS Digerati, ...) stay in their original form."""
    labels = CITATION_LABELS.get(language) or CITATION_LABELS["ko"]
    ptw_prefix = labels["poetrytalks_wikidata_prefix"]
    graph_prefix = labels["graph_prefix"]
    link_only_prefix = labels["link_only_prefix"]

    citations: list = []
    seen: set = set()

    def _add(line: str) -> None:
        if line and line not in seen:
            seen.add(line)
            citations.append(line)

    graph = _to_dict(evidence.get("graph"))
    vector = _to_dict(evidence.get("vector"))
    external = _to_dict(evidence.get("external"))

    # (a) Collect every node id referenced anywhere in the evidence bundle,
    # in insertion order (which mirrors the order the LLM will see them).
    # These become the mandatory "poetrytalks wikidata" citations — narrowed
    # to `referenced_node_ids` when the caller supplies it.
    ptw_ids = _collect_all_node_ids(graph, vector)
    if referenced_node_ids is not None:
        allowed = set(referenced_node_ids)
        ptw_ids = [nid for nid in ptw_ids if nid in allowed]
    node_names = _collect_node_names(graph, vector)
    for node_id in ptw_ids:
        url = poetrytalks_url(node_id)
        if not url:
            continue
        nm = node_names.get(node_id) or {}
        name_suffix = _format_citation_name(
            nm.get("name_kor"), nm.get("name_eng"), nm.get("name_chi"), language)
        _add(f"- {ptw_prefix}: [{node_id}]({url}){name_suffix}")

    # (b) Per-provenance breadcrumb rendered in the LOCKED response language.
    # Graph-provenance labels are ID-only (already language-neutral) so we
    # keep their static label. Vector-provenance labels are REBUILT from raw
    # `work_name_kor/eng/chi` + `entry_position` so an English or Chinese
    # answer doesn't leak the Korean work name into the Sources section.
    #
    # De-duplication uses STRUCTURED keys (work order Phase 3) — stable
    # internal ids first, so the same Entry retrieved twice yields one
    # breadcrumb while distinct node ids (e.g. P553 vs P1227 sharing an
    # external Wikidata id) always stay separate.
    seen_prov_keys: set = set()

    def _add_prov(prov: dict, rendered: str) -> None:
        if not rendered or not _label_is_clean(rendered):
            return
        key = _prov_dedup_key(prov, rendered)
        if key in seen_prov_keys:
            return
        seen_prov_keys.add(key)
        _add(f"- {graph_prefix}: {rendered}")

    for prov in graph.get("provenance", []):
        _add_prov(prov, prov.get("label") or "")
    for prov in vector.get("provenance", []):
        _add_prov(prov, _rebuild_vector_prov_label(prov, language))

    for c in external.get("claims", []):
        url = c.get("url")
        if not url:
            continue                     # no verified URL → no citation
        label = c.get("source_label") or c.get("source")
        who = c.get("entity") or c.get("person") or label
        status = c.get("status")
        if status == "ok":
            _add(f"- {label}: [{who}]({url})")
        elif status == "link_only":
            _add(f"- {link_only_prefix} — {label}: [{who}]({url})")
    return citations


def _prov_dedup_key(prov: dict, rendered: str) -> tuple:
    """Structured de-duplication key for one provenance record.

    Priority (work order Phase 3):
        source_type + entry_id
        source_type + poem_or_critique_id
        source_type + entity_id
        source_type + work_id            (work-only citation)
        verified source_url
        (fallback) rendered line

    Distinct internal node ids always produce distinct keys — shared external
    ids play no role here, so P553 / P1227 can never collapse."""
    st = prov.get("source_type") or ""
    for field_name in ("entry_id", "poem_or_critique_id", "entity_id"):
        value = prov.get(field_name)
        if is_valid_node_id(value):
            return (st, field_name, value)
    value = prov.get("work_id")
    if is_valid_node_id(value):
        return (st, "work_only", value)
    url = prov.get("source_url")
    if url:
        return (st, "url", url)
    return (st, "line", rendered)


def _collect_all_node_ids(graph: dict, vector: dict) -> list:
    """Return every distinct node id referenced in graph + vector evidence,
    preserving first-seen order. Sources (highest-coverage first):

      * `Evidence.node_references` — the complete, all-node-class inventory
        (Work/Entry/Poem/Critique/Person/Place/Topic/Era/CriticalTerm),
        including ids nested inside collect()/map results and multiple ids
        of the same class in one row;
      * `Provenance.work_id / entry_id / poem_or_critique_id / entity_id`
        for named kinds (backward compatible with older Evidence payloads
        that predate `node_references`);
      * Any id embedded in a provenance label as a markdown link (covers
        two-letter prefixes like CriticalTerm's `CT###` and any class not
        otherwise structurally represented);
      * `Entity.node_id` for resolved Person / Place entities;
      * `document['entry_id']` on each vector document.

    All values are shape-validated by `is_valid_node_id` so external
    authority values (Wikidata Q-ids, idAKSency codes, ...) never leak into
    this group."""
    order: list = []
    seen: set = set()

    def _push(candidate):
        if not isinstance(candidate, str):
            return
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            return
        if not is_valid_node_id(candidate):
            return
        seen.add(candidate)
        order.append(candidate)

    for ref in graph.get("node_references", []) + vector.get("node_references", []):
        _push(ref.get("node_id") if isinstance(ref, dict) else None)

    for prov in graph.get("provenance", []) + vector.get("provenance", []):
        for k in ("work_id", "entry_id", "poem_or_critique_id", "entity_id"):
            _push(prov.get(k))
        # `label` embeds every id already rendered as a markdown link.
        for match in _MD_LINK_ID_RE.findall(prov.get("label") or ""):
            _push(match)

    for ent in graph.get("entities", []) + vector.get("entities", []):
        _push(ent.get("node_id"))

    for doc in vector.get("documents", []):
        _push(doc.get("entry_id"))
        _push(doc.get("work_id"))

    return order


# Extract the `<id>` from any `[<id>](<POETRYTALKS_BASE_URL><id>)` markdown
# link embedded in provenance labels — recovers ids (including two-letter
# prefixes like CriticalTerm's `CT###`) that `Provenance.*_id` fields don't
# structurally represent. Built from the single base-URL constant so this
# regex can never drift from the domain actually used to build links.
_MD_LINK_ID_RE = _re.compile(
    r"\[([A-Z]{1,2}\d{1,4})\]\(" + _re.escape(_PTW_BASE) + r"[A-Z]{1,2}\d{1,4}\)"
)

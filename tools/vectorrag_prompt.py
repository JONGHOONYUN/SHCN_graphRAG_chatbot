"""Pure per-document prompt preparation for the independent vectorRAG path
(text_rag.py) — work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md
§4 Phase 4.

`create_stuff_documents_chain`'s default `document_prompt` only stuffs
`page_content` into the LLM context — the rich metadata
`text_rag._build_light_retrieval_query` already returns (three-language
text, Entry/Work id+name, poetrytalks_link) never actually reached the
model. A custom `document_prompt` fixes that, but a raw `{field}`
placeholder in a LangChain PromptTemplate renders a missing/null metadata
value as the literal string "None" — so the language-ordered, null-omitted
text block and the provenance line are pre-computed HERE (in plain Python,
where "value is missing" is a normal conditional) into two new metadata
keys the document_prompt template simply references verbatim. The ORIGINAL
metadata keys are preserved unchanged alongside them, so
`tools.evidence.docs_to_evidence` can still normalize the raw fields
afterward for the deterministic citation-build step.

Kept in `tools/` (no streamlit/neo4j/llm import) so it is unit-testable
without triggering text_rag.py's module-level Neo4j/Gemini client
construction — the same reasoning documented in tools/graph_intent.py.
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document
from langchain_core.prompts import PromptTemplate

from tools.synthesis import source_text_priority

_TEXT_FIELD_LABELS = {
    "original_chinese": "Chinese",
    "korean_translation": "Korean",
    "english_translation": "English",
}
_LANG_CODE_TO_META_KEY = {
    "chi": "original_chinese", "kor": "korean_translation", "eng": "english_translation",
}
_BARE_TEXT_FIELD_TO_LANG_CODE = {"textChi": "chi", "textKor": "kor", "textEng": "eng"}


def quoted_text_block(meta: dict, language: str) -> str:
    """Language-ordered, null-omitted rendering of the Entry's three parallel
    source-text fields — never translated/altered, only reordered/omitted
    (work order §3.1, shared policy with graphRAG via
    `tools.synthesis.source_text_priority`)."""
    order = [_LANG_CODE_TO_META_KEY[_BARE_TEXT_FIELD_TO_LANG_CODE[f]]
            for f in source_text_priority(language)]
    lines = []
    for key in order:
        val = (meta or {}).get(key)
        if val:
            lines.append(f"[{_TEXT_FIELD_LABELS[key]}] {val}")
    return "\n".join(lines)


def provenance_block(meta: dict) -> str:
    """Entry ID/name/position + Work ID/name + poetrytalks_link, omitting
    whichever parts are absent — never a placeholder like 'Entry None'."""
    meta = meta or {}
    bits = []
    work_name = meta.get("source_work_kor") or meta.get("source_work_eng")
    if work_name:
        work_id = meta.get("source_work_id")
        bits.append(f"Work: {work_name}" + (f" ({work_id})" if work_id else ""))
    entry_bit = "Entry"
    entry_name = meta.get("entry_name_kor") or meta.get("entry_name_eng")
    if entry_name:
        entry_bit += f" {entry_name}"
    if meta.get("entry_position"):
        entry_bit += f" #{meta['entry_position']}"
    if meta.get("entry_id"):
        entry_bit += f" ({meta['entry_id']})"
    bits.append(entry_bit)
    if meta.get("poetrytalks_link"):
        bits.append(f"Link: {meta['poetrytalks_link']}")
    return " > ".join(bits)


def prepare_documents_for_prompt(docs: Any, language: str) -> list:
    """Return NEW Document objects (inputs never mutated) whose metadata
    gains two computed keys — `quoted_text_block` and `provenance_block` —
    that `document_prompt_for_lang` references. Every original metadata
    field is preserved unchanged. Accepts LangChain Document objects or
    plain dicts with `page_content`/`metadata` keys."""
    prepared = []
    for doc in docs or []:
        if isinstance(doc, dict):
            page_content = doc.get("page_content") or ""
            meta = dict(doc.get("metadata") or {})
        else:
            page_content = getattr(doc, "page_content", "") or ""
            meta = dict(getattr(doc, "metadata", None) or {})
        meta["quoted_text_block"] = quoted_text_block(meta, language)
        meta["provenance_block"] = provenance_block(meta)
        prepared.append(Document(page_content=page_content, metadata=meta))
    return prepared


def document_prompt_for_lang(language: str) -> PromptTemplate:
    """Custom per-document prompt (work order §4 Phase 4 item 2) — every
    document stuffed into `{context}` carries its provenance line and its
    language-ordered source-text block, so the LLM actually receives the
    metadata the retrieval query projects, not just page_content.

    `language` is accepted (rather than a single module-level constant)
    because the template text itself is language-invariant — the ORDERING
    already happened in `quoted_text_block` — but the parameter is kept so
    call sites read as intentionally language-aware and future per-language
    template variation stays trivial to add."""
    return PromptTemplate.from_template("{provenance_block}\n{quoted_text_block}")

"""Language-aware name/text-field helpers shared by formatting and citations.

책임: 응답 언어별 이름 선택, 병렬 source-text 필드(제시 순서만) 재정렬,
placeholder 금지 검사. 값은 절대 변형하지 않는다 — 순서만 바꾼다.
허용 의존성: 표준 라이브러리만. 외부 부작용: 없음.
기존 facade: tools/synthesis.py.

Moved verbatim from tools/synthesis.py (modularization work order Phase 3).
"""

from __future__ import annotations

import re as _re
from typing import Any, Optional


# ── Language-aware label rebuild helpers (fixes Korean-in-English-Sources) ───
def _pick_by_language(kor: Optional[str], eng: Optional[str],
                      chi: Optional[str], language: str) -> Optional[str]:
    """Return the name variant that matches the locked response language.
    Falls back through the other languages when the preferred one is empty.

    Priority order per language:
      * en → eng, kor, chi
      * zh → chi, kor, eng
      * ko (or unknown) → kor, eng, chi
    """
    if language == "en":
        return eng or kor or chi
    if language == "zh":
        return chi or kor or eng
    return kor or eng or chi


# ── Language-aware source-text presentation order (work order:
# CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §3.1) ──────────────
# Content is NEVER translated/altered here — only the PRESENTATION ORDER of
# the parallel textEng/textKor/textChi fields changes, so a reader gets the
# quotation in their own response language first. Both graphRAG formatters
# (_format_graph_block / _format_vector_block) and the independent vectorRAG
# path share this single source of truth.
_SOURCE_TEXT_PRIORITY = {
    "en": ("textEng", "textKor", "textChi"),
    "ko": ("textKor", "textEng", "textChi"),
    "zh": ("textChi", "textKor", "textEng"),
}


def source_text_priority(language: str) -> tuple:
    """Language-locked presentation order for the three parallel source-text
    fields. Unsupported/unknown languages fall back to the project's
    existing default language policy (`ko`)."""
    return _SOURCE_TEXT_PRIORITY.get(language, _SOURCE_TEXT_PRIORITY["ko"])


# Bare source-text field -> 2-letter language code, and the reverse mapping,
# used by `reorder_source_text_fields` to recognize both the bare
# (textEng/textKor/textChi) and role-prefixed (<role>_text_eng|kor|chi)
# field-family shapes.
_BARE_TEXT_FIELD_LANG = {"textEng": "eng", "textKor": "kor", "textChi": "chi"}
_LANG_TO_BARE_TEXT_FIELD = {v: k for k, v in _BARE_TEXT_FIELD_LANG.items()}
_ROLE_TEXT_FIELD_RE = _re.compile(r"^(.+)_text_(eng|kor|chi)$")


def _text_field_family(key: str):
    """Return (family_key, lang_code) if `key` is a recognized parallel
    source-text field, else None. `family_key` groups siblings that must be
    reordered together: bare `textEng/textKor/textChi` share family_key ""；
    role-prefixed `<role>_text_eng|kor|chi` (e.g. `critique_text_eng`) share
    family_key `<role>`."""
    if key in _BARE_TEXT_FIELD_LANG:
        return "", _BARE_TEXT_FIELD_LANG[key]
    m = _ROLE_TEXT_FIELD_RE.match(key)
    if m:
        return m.group(1), m.group(2)
    return None


def _family_member_key(family_key: str, lang_code: str) -> str:
    if family_key == "":
        return _LANG_TO_BARE_TEXT_FIELD[lang_code]
    return f"{family_key}_text_{lang_code}"


def reorder_source_text_fields(value: Any, language: str) -> Any:
    """Recursively return a NEW copy of `value` with every sibling family of
    parallel source-text fields (bare `textEng/textKor/textChi`, and
    role-prefixed `<role>_text_eng|kor|chi`, at ANY nesting depth inside
    dicts/lists — e.g. `collect()`/map results) presented in the response
    language's priority order (work order §3.1).

    Guarantees:
      * every text VALUE is passed through unchanged — byte-for-byte
        identical, never translated/summarized/normalized;
      * only the RELATIVE ORDER of a family's present members changes; a
        family with only one member present is a no-op;
      * every non-text-field key/value (including unrelated dict keys,
        nested structures, and list items) is preserved in its original
        relative position;
      * the input is never mutated — `value` and everything reachable from
        it are only ever read, and a fresh dict/list is built for the
        result whenever `value` is itself a dict/list."""
    if isinstance(value, list):
        return [reorder_source_text_fields(item, language) for item in value]
    if not isinstance(value, dict):
        return value

    priority_langs = [_BARE_TEXT_FIELD_LANG[f] for f in source_text_priority(language)]
    result: dict = {}
    emitted_families: set = set()
    for key, val in value.items():
        family = _text_field_family(key)
        if family is None:
            result[key] = reorder_source_text_fields(val, language)
            continue
        family_key, _lang_code = family
        if family_key in emitted_families:
            continue  # this family's members were already emitted in order
        emitted_families.add(family_key)
        for lang_code in priority_langs:
            member_key = _family_member_key(family_key, lang_code)
            if member_key in value:
                result[member_key] = reorder_source_text_fields(
                    value[member_key], language)
    return result


def _work_name_bilingual(prov: dict, language: str) -> Optional[str]:
    """Language-aware work name, bilingual when the answer language differs
    from the original (Korean) name — e.g. "Paegwan Chapki (패관잡기)" for
    English answers so English-language readers can still cross-reference
    the Korean original."""
    kor = prov.get("work_name_kor")
    eng = prov.get("work_name_eng")
    chi = prov.get("work_name_chi")
    if not any((kor, eng, chi)):
        return None
    primary = _pick_by_language(kor, eng, chi, language)
    if language == "ko":
        return primary
    # For en / zh, append the Korean original in parentheses when it differs
    # from the primary — informative bilingual reference.
    if primary and kor and primary != kor:
        return f"{primary} ({kor})"
    return primary


# Placeholder patterns that must never surface in a user-facing citation.
_PLACEHOLDER_RE = _re.compile(
    r"\(\?\)|\[\?\]|\(None\)|/None\b|\bEntry (?:None|0|-\d+)\b"
)


def _label_is_clean(label: str) -> bool:
    """True when a pre-built label carries none of the forbidden placeholder
    shapes ('(?)', 'Entry 0', 'Entry None', '(None)', '/None')."""
    return bool(label) and not _PLACEHOLDER_RE.search(label)

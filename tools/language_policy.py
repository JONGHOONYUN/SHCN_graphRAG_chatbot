"""Pure, deterministic language-detection and -routing policy.

work order: CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md

This module is the SINGLE source of truth for:
  * detecting the grammatical language a QUESTION is written in
    (`detect_question_language`) — a domain-specific heuristic for East
    Asian classical-literature questions where an inserted entity name
    (e.g. `杜甫`, `이규보`) must never override the sentence's own grammar;
  * detecting an explicit "answer in X" / "stop locking language" control
    phrase and the text span(s) it occupies (`detect_language_control`,
    `remove_language_control`);
  * resolving one turn's `question_language` (what to search with),
    `response_language` (what to answer in), and updated `locked_language`
    (`resolve_languages`).

Constraints (work order §2.2 — enforced by convention, not by a test that
imports streamlit here):
  * stdlib only (`re`, `unicodedata`, `dataclasses`, `typing`) — NO
    streamlit/agent/text_rag/Neo4j/LangChain/LLM import, so this module is
    testable without triggering `bot.py`'s page-config/auth side effects or
    `agent.py`/`text_rag.py`'s live Gemini/Neo4j client construction.
  * no network, no environment/DB access at import time or call time.
  * every regex is compiled once at module load.

This is a deterministic DOMAIN heuristic, not a general-purpose language
identifier: it is tuned to disambiguate "sentence grammar vs. an inserted
Korean/Chinese proper noun" for this project's classical-poetry questions,
and to the project's existing `ko`/`en`/`zh` three-language scope. Do not
extend it to more languages or replace it with an LLM/external API call —
see the work order's explicit non-goals.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Tuple

# ──────────────────────────────────────────────
# §4.4 scoring constants — a documented minimum behavioral contract, not a
# sacred set of numbers: any other values are acceptable as long as every
# fixture in §4.6/§9.1 and the tie-break order in §4.5 still hold.
# ──────────────────────────────────────────────
GRAMMAR_WEIGHT = 5
CONTENT_CAP = 6

SUPPORTED_LANGUAGES = ("ko", "en", "zh")
DEFAULT_LANGUAGE = "ko"


# ──────────────────────────────────────────────
# §4.1 input normalization
# ──────────────────────────────────────────────
def _normalize(text: Optional[str]) -> str:
    """None-safe NFC normalization. Used only for the DETECTION working
    copy — callers keep the original string for user-facing display."""
    return unicodedata.normalize("NFC", text or "")


# ──────────────────────────────────────────────
# §4.3 character-signal ranges
#   Hangul syllables:        U+AC00–U+D7A3 (완성형 한글, matches the
#                             project's pre-existing `[가-힣]` convention)
#   Han (CJK) characters:    U+4E00–U+9FFF (CJK Unified Ideographs) plus
#                             U+3400–U+4DBF (Extension A) for broader
#                             coverage of the "일반적으로 사용하는 Extension"
#                             note — neither range is required by any
#                             mandated fixture, kept conservative.
#   Latin word tokens:       a maximal run of ASCII + Latin-1
#                             Supplement/Latin Extended-A/B letters, with
#                             internal apostrophes so romanized forms like
#                             `Ch'ongnim`/`Hŏ` count as ONE token, matching
#                             this project's McCune-Reischauer spellings.
# ──────────────────────────────────────────────
_HANGUL_RE = re.compile(r"[가-힣]")
_HAN_RE = re.compile(r"[㐀-䶿一-鿿]")
_LATIN_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ'’]*")


# ──────────────────────────────────────────────
# §4.2 grammar signals
# ──────────────────────────────────────────────
_EN_GRAMMAR_WORDS = (
    "who", "what", "when", "where", "why", "how", "which",
    "am", "is", "are", "was", "were", "do", "does", "did",
    "has", "have", "had", "can", "could", "will", "would", "should",
    "may", "might",
)
_EN_GRAMMAR_PHRASES = ("tell me", "explain", "describe", "compare", "list", "show")
_EN_GRAMMAR_RE = re.compile(
    r"\b(?:" + "|".join(_EN_GRAMMAR_WORDS) + r")\b"
    r"|\b(?:" + "|".join(re.escape(p) for p in _EN_GRAMMAR_PHRASES) + r")\b",
    re.IGNORECASE,
)

# Korean question words + question/request sentence endings. Endings are
# matched as plain substrings (Korean has no reliable ASCII-style word
# boundary), but particles (below) are matched with an explicit
# hangul-before / non-hangul-after guard so a name's internal syllable is
# never mistaken for an attached particle.
_KO_GRAMMAR_PHRASES = (
    "누구", "무엇", "뭐", "어떻게", "어디", "왜", "언제", "어느",
    "인가요", "입니까", "습니까", "는가", "나요", "인가",
    "알려줘", "알려주세요", "설명해줘", "설명해주세요",
)
_KO_GRAMMAR_RE = re.compile(
    "|".join(re.escape(p) for p in _KO_GRAMMAR_PHRASES)
)
# Particle (조사) immediately attached to a hangul syllable and followed by a
# non-hangul boundary (or end of string) — never fires on a bracketed/
# Latin-suffixed name like `(杜甫)는` or `Fu는`, only on genuine Korean words.
_KO_PARTICLE_RE = re.compile(r"[가-힣](?:은|는|이|가|을|를|에서|에게)(?=[^가-힣]|$)")

# Chinese question/narrative grammar + sentence-final particles. Plain
# substring matching (no whitespace word boundaries in Chinese). Longer
# alternatives are listed before their prefixes (e.g. 哪些 before 哪) so a
# single scan does not need special-casing for the overlap.
_ZH_GRAMMAR_PHRASES = (
    "为什么", "為什麼", "如何", "什么", "什麼", "哪些", "谁", "誰", "哪",
    "是否", "怎么", "怎麼", "请问", "請問", "评价", "評價",
)
_ZH_GRAMMAR_RE = re.compile(
    "|".join(re.escape(p) for p in _ZH_GRAMMAR_PHRASES)
)
_ZH_SENTENCE_FINAL_RE = re.compile(r"[吗嗎呢]")


def _count(pattern: "re.Pattern", text: str) -> int:
    return len(pattern.findall(text))


def detect_question_language(text: Optional[str]) -> str:
    """Deterministic ko/en/zh classification of a QUESTION's grammar.

    Sentence grammar wins over an inserted entity name's script: `How is
    杜甫 critiqued?` is `en` because "How is ... critiqued" is English
    grammar and `杜甫` is just the object of the sentence. See the work
    order §4.6 fixture table — every listed input is a regression pin for
    this function.

    Never calls an LLM or external API; same input always yields the same
    output (pure function of the normalized text)."""
    normalized = _normalize(text)
    if not normalized:
        return DEFAULT_LANGUAGE

    latin_words = _count(_LATIN_WORD_RE, normalized)
    hangul_syllables = _count(_HANGUL_RE, normalized)
    han_chars = _count(_HAN_RE, normalized)

    if latin_words == 0 and hangul_syllables == 0 and han_chars == 0:
        return DEFAULT_LANGUAGE  # no script signal at all (digits/emoji/punct only)

    en_matches = _count(_EN_GRAMMAR_RE, normalized)
    ko_matches = _count(_KO_GRAMMAR_RE, normalized) + _count(_KO_PARTICLE_RE, normalized)
    zh_matches = _count(_ZH_GRAMMAR_RE, normalized) + _count(_ZH_SENTENCE_FINAL_RE, normalized)

    score_en = en_matches * GRAMMAR_WEIGHT + min(latin_words, CONTENT_CAP)
    score_ko = ko_matches * GRAMMAR_WEIGHT + min(hangul_syllables, CONTENT_CAP)
    score_zh = zh_matches * GRAMMAR_WEIGHT + min(han_chars, CONTENT_CAP)

    # §4.5 tie-break: highest score, then grammar-match count, then content
    # units, then the fixed fallback order ko -> zh -> en. Listing
    # candidates in that order and using `max()` (which keeps the FIRST
    # maximal item on a full tie) implements steps 1-4 in one expression —
    # deterministic regardless of dict/set iteration order.
    candidates = (
        ("ko", score_ko, ko_matches, hangul_syllables),
        ("zh", score_zh, zh_matches, han_chars),
        ("en", score_en, en_matches, latin_words),
    )
    best = max(candidates, key=lambda c: (c[1], c[2], c[3]))
    return best[0]


# ──────────────────────────────────────────────
# §6 — explicit response-language control phrases
#
# Ported verbatim (same intent, same trigger set) from the pre-existing
# `bot.py::EXPLICIT_LOCK_PATTERNS`/`RELEASE_LOCK_PATTERNS`, extended so a
# match's SPAN can be removed from the question text without leaving a
# dangling Korean/Chinese verb conjugation or sentence-final punctuation
# behind (e.g. "영어로 답변해줘. 두보는..." must strip the WHOLE "영어로
# 답변해줘." clause, not just "영어로 답변"). English patterns rely on the
# generic leading/trailing punctuation cleanup in `remove_language_control`
# instead, since English commands are naturally colon/period-separated from
# the following question rather than agglutinated onto it.
# ──────────────────────────────────────────────
_KO_CONJ_SUFFIX = r"(?:해\s*주(?:세요|십시오)|해\s*줘|해줘|줘|주세요|주십시오)?[.!?。！？]?"
_ZH_TERMINAL = r"[。！？.!?]?"

EXPLICIT_LOCK_PATTERNS: Tuple[Tuple["re.Pattern", str], ...] = (
    # English
    (re.compile(r"\b(?:answer|respond|reply|talk|speak|write|chat)\s+(?:to me\s+|with me\s+)?(?:in\s+)?english\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(?:answer|respond|reply|talk|speak|write|chat)\s+(?:to me\s+|with me\s+)?(?:in\s+)?korean\b", re.IGNORECASE), "ko"),
    (re.compile(r"\b(?:answer|respond|reply|talk|speak|write|chat)\s+(?:to me\s+|with me\s+)?(?:in\s+)?chinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"\b(?:please\s+)?use\s+english\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(?:please\s+)?use\s+korean\b", re.IGNORECASE), "ko"),
    (re.compile(r"\b(?:please\s+)?use\s+chinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"\b(?:switch|change)\s+to\s+english\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(?:switch|change)\s+to\s+korean\b", re.IGNORECASE), "ko"),
    (re.compile(r"\b(?:switch|change)\s+to\s+chinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"\bin\s+english\s+(?:please|from now on)\b", re.IGNORECASE), "en"),
    (re.compile(r"\bin\s+korean\s+(?:please|from now on)\b", re.IGNORECASE), "ko"),
    (re.compile(r"\bin\s+chinese\s+(?:please|from now on)\b", re.IGNORECASE), "zh"),
    # Korean (verb-conjugation + trailing terminator absorbed into the match)
    (re.compile(r"한국어로\s*(?:대답|답변|응답|답|말)" + _KO_CONJ_SUFFIX), "ko"),
    (re.compile(r"영어로\s*(?:대답|답변|응답|답|말)" + _KO_CONJ_SUFFIX), "en"),
    (re.compile(r"중국어로\s*(?:대답|답변|응답|답|말)" + _KO_CONJ_SUFFIX), "zh"),
    (re.compile(r"(?:앞으로|이제부터|계속)\s*한국어로" + _KO_CONJ_SUFFIX), "ko"),
    (re.compile(r"(?:앞으로|이제부터|계속)\s*영어로" + _KO_CONJ_SUFFIX), "en"),
    (re.compile(r"(?:앞으로|이제부터|계속)\s*중국어로" + _KO_CONJ_SUFFIX), "zh"),
    # Chinese — 请/請-prefixed forms (with optional verb + terminator) are
    # listed BEFORE the bare `用<lang>` forms so a full "请用英文回答。"
    # imperative clause is captured as one span rather than only its
    # "用英文回答" tail (which `.search()` would otherwise match first,
    # leaving a dangling "请" behind).
    (re.compile(r"请用中文\s*(?:回答|回复|说|回應|對話)?" + _ZH_TERMINAL), "zh"),
    (re.compile(r"請用中文\s*(?:回答|回覆|說|回應|對話)?" + _ZH_TERMINAL), "zh"),
    (re.compile(r"请用英(?:语|文)\s*(?:回答|回复|说|回應|對話)?" + _ZH_TERMINAL), "en"),
    (re.compile(r"請用英(?:語|文)\s*(?:回答|回覆|說|回應|對話)?" + _ZH_TERMINAL), "en"),
    (re.compile(r"请用韩(?:语|文)\s*(?:回答|回复|说|回應|對話)?" + _ZH_TERMINAL), "ko"),
    (re.compile(r"請用韓(?:語|文)\s*(?:回答|回覆|說|回應|對話)?" + _ZH_TERMINAL), "ko"),
    (re.compile(r"用中文\s*(?:回答|回复|说|回應|對話)" + _ZH_TERMINAL), "zh"),
    (re.compile(r"用英(?:语|文)\s*(?:回答|回复|说|回應|對話)" + _ZH_TERMINAL), "en"),
    (re.compile(r"用韩(?:语|文)\s*(?:回答|回复|说|回應|對話)" + _ZH_TERMINAL), "ko"),
)

RELEASE_LOCK_PATTERNS: Tuple["re.Pattern", ...] = (
    re.compile(r"\b(?:remove|cancel|stop|clear|reset|disable)\s+(?:the\s+)?(?:language\s+)?lock\b", re.IGNORECASE),
    re.compile(r"\bfollow\s+(?:my|the)\s+question\s+language\b", re.IGNORECASE),
    re.compile(r"\bauto[-\s]?detect\s+language\b", re.IGNORECASE),
    re.compile(r"언어\s*락\s*(?:해제|취소|초기화|리셋)"),
    re.compile(r"자동\s*(?:언어\s*감지|감지|판별)"),
    re.compile(r"(?:跟着|跟随|根据)我的语言"),
)


@dataclass(frozen=True)
class LanguageControl:
    """Result of scanning one message for an explicit response-language
    control phrase. `spans` are (start, end) index pairs into the text that
    was scanned, already merged and sorted — `remove_language_control`
    consumes them directly."""

    lock_language: Optional[str]
    release: bool
    spans: Tuple[Tuple[int, int], ...] = field(default_factory=tuple)


def _merge_spans(spans) -> Tuple[Tuple[int, int], ...]:
    if not spans:
        return ()
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        last = merged[-1]
        if s <= last[1]:            # overlapping or touching — merge
            last[1] = max(last[1], e)
        else:
            merged.append([s, e])
    return tuple((s, e) for s, e in merged)


def detect_language_control(text: Optional[str]) -> LanguageControl:
    """Find an explicit lock/release control phrase, if any.

    Matches the FIRST pattern (in the fixed list order above) for each of
    lock/release independently — same first-match-wins semantics as the
    pre-existing `bot.py::detect_explicit_lock`/`detect_release_request`,
    kept for behavioral compatibility. When BOTH a lock and a release
    phrase are present in one message, both spans are reported here (so
    both get stripped from the question text); `resolve_languages` applies
    the "lock wins" priority (work order §6.3) when deciding the action."""
    t = text or ""
    spans = []
    lock_language = None
    for pattern, lang_code in EXPLICIT_LOCK_PATTERNS:
        m = pattern.search(t)
        if m:
            lock_language = lang_code
            spans.append(m.span())
            break

    release = False
    for pattern in RELEASE_LOCK_PATTERNS:
        m = pattern.search(t)
        if m:
            release = True
            spans.append(m.span())
            break

    return LanguageControl(lock_language=lock_language, release=release,
                           spans=_merge_spans(spans))


_LEADING_JUNK_RE = re.compile(r"^[\s:,.。，、]+")
_TRAILING_JUNK_RE = re.compile(r"[\s:,.。，、]+$")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")


def remove_language_control(text: Optional[str], spans) -> str:
    """Remove the given (start, end) spans from `text` (back-to-front, so
    earlier indices never drift), then trim whatever leading/trailing
    punctuation and doubled whitespace remains at the cut points. Entity
    names, quotes, apostrophes, and the actual question wording are never
    touched — only the control-phrase spans and the punctuation directly
    left dangling by their removal."""
    t = text or ""
    for s, e in sorted(spans or (), key=lambda p: p[0], reverse=True):
        t = t[:s] + t[e:]
    t = _LEADING_JUNK_RE.sub("", t)
    t = _TRAILING_JUNK_RE.sub("", t)
    t = _MULTI_SPACE_RE.sub(" ", t)
    return t.strip()


@dataclass(frozen=True)
class LanguageResolution:
    """One turn's fully-resolved language state (work order §2.2)."""

    original_prompt: str
    question_text: str
    question_language: str
    response_language: str
    locked_language: Optional[str]
    action: str            # "lock" | "release" | "none"
    control_only: bool
    debug_info: dict = field(default_factory=dict)


def resolve_languages(prompt: Optional[str],
                      locked_language: Optional[str] = None) -> LanguageResolution:
    """Resolve `question_language`/`response_language`/`locked_language` for
    one turn (work order §5.1/§7).

    - Control phrases are detected and stripped BEFORE question-language
      detection, so an entity name is never confused with "the question",
      and a "who wrote 杜甫" grammar cue inside a lock command never leaks
      into the search text.
    - `question_language` is always computed from the cleaned
      `question_text` when one remains; a control-only message (§6.4) falls
      back to the control's own target language (lock) or the ORIGINAL
      message's grammar (release), and only "ko" when neither yields a
      signal.
    - `response_language` is `locked_language` (after this turn's lock/
      release update) or else `question_language`.
    """
    original = prompt or ""
    control = detect_language_control(original)
    question_text = remove_language_control(original, control.spans)

    if control.lock_language is not None:
        next_locked = control.lock_language
        action = "lock"
    elif control.release:
        next_locked = None
        action = "release"
    else:
        next_locked = locked_language
        action = "none"

    if question_text:
        question_language = detect_question_language(question_text)
    elif control.lock_language:
        question_language = control.lock_language
    elif control.release:
        question_language = detect_question_language(original)
    else:
        question_language = DEFAULT_LANGUAGE

    response_language = next_locked or question_language

    return LanguageResolution(
        original_prompt=original,
        question_text=question_text,
        question_language=question_language,
        response_language=response_language,
        locked_language=next_locked,
        action=action,
        control_only=not bool(question_text),
        debug_info={
            "lock_language": control.lock_language,
            "release": control.release,
            "spans": control.spans,
        },
    )

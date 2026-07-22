"""Deterministic final-answer assembly for the graphRAG pipeline.

The observed production defect: Gemini received fully pre-built citation lines
but rendered a Sources section with the URLs stripped, bullets collapsed, and
`(?)` placeholders. Prompt instructions alone cannot force output structure, so
this module moves the Sources boundary OUT of the LLM:

    LLM writes the answer BODY only
      -> strip_model_sources()      # remove any Sources section the model wrote
      -> link_entities_in_body()    # deterministic Poetry Talks links in the body
      -> render_sources_section()   # code-rendered, localized Sources
      -> assemble_final_answer()    # body + Sources, the ONLY user-facing shape

Pure module (stdlib + tools.evidence only) so every rule is unit-testable
without live Gemini/Neo4j.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from tools.evidence import poetrytalks_url

logger = logging.getLogger(__name__)


# ── Localized Sources headers (existing language policy) ─────────────────────
SOURCES_HEADERS = {"ko": "출처", "en": "Sources", "zh": "来源"}


def sources_header(language: str) -> str:
    return SOURCES_HEADERS.get(language, SOURCES_HEADERS["ko"])


# ── Model-generated Sources removal ──────────────────────────────────────────
# A model-written Sources section starts at a line that consists ONLY of one of
# these keywords (any language), optionally wrapped as a markdown header of any
# depth (# .. ######) or bold (**...**), optionally with a trailing colon.
# A mid-sentence word like "the source of this poem" never matches because the
# pattern is anchored to a full line.
_SOURCES_KEYWORDS = (
    "sources", "references",              # en
    "출처", "참고문헌",                     # ko
    "来源", "參考資料", "参考资料",          # zh
)
_MODEL_SOURCES_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*[ \t]*)?"
    r"(?:" + "|".join(_SOURCES_KEYWORDS) + r")"
    r"(?:[ \t]*\*\*)?[ \t]*[:：]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def strip_model_sources(body: str) -> tuple:
    """Remove any model-authored Sources/References/출처/来源 section.

    Cuts from the FIRST line that is a standalone Sources-style header (markdown
    header of any depth, bold, or bare keyword line, optional colon) to the end
    of the text — the final Sources section is appended deterministically by
    `assemble_final_answer`, so anything the model wrote there is untrusted.

    Returns (sanitized_body, removed: bool)."""
    if not body:
        return "", False
    match = _MODEL_SOURCES_RE.search(body)
    if not match:
        return body, False
    return body[: match.start()].rstrip(), True


# ── Deterministic body entity links (work order Phase 4) ─────────────────────
# Protected regions where we must never inject a link: existing markdown links,
# images, inline code, fenced code blocks, raw URLs.
_PROTECTED_RES = (
    re.compile(r"```.*?```", re.DOTALL),          # fenced code
    re.compile(r"`[^`\n]*`"),                     # inline code
    re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)"),     # markdown link/image
    re.compile(r"https?://\S+"),                  # bare URL
)


def _protected_spans(text: str) -> list:
    spans = []
    for pattern in _PROTECTED_RES:
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _in_spans(pos: int, end: int, spans: list) -> bool:
    return any(s < end and pos < e for s, e in spans)


def _entity_names(entity: Any) -> list:
    """Name variants for one entity (dict or Entity-like)."""
    if isinstance(entity, dict):
        get = entity.get
    else:
        get = lambda k: getattr(entity, k, None)  # noqa: E731
    names = []
    for key in ("name_kor", "name_chi", "name_eng"):
        value = get(key)
        if isinstance(value, str) and len(value.strip()) >= 2:
            names.append(value.strip())
    return names


def _entity_node_id(entity: Any) -> Optional[str]:
    node_id = entity.get("node_id") if isinstance(entity, dict) else \
        getattr(entity, "node_id", None)
    return node_id if isinstance(node_id, str) else None


def link_entities_in_body(body: str, entities: Any) -> str:
    """Deterministically link the FIRST mention of each evidence entity name to
    its Poetry Talks node URL.

    Guarantees (work order Phase 4):
      * only names present in the evidence entity list are ever linked;
      * a name mapping to MORE THAN ONE node id (동명이인) is never linked —
        no arbitrary pick;
      * only shape-valid node ids produce URLs (`poetrytalks_url`), so external
        Q-ids etc. can never become Poetry Talks links;
      * existing markdown links, code spans/blocks, and raw URLs are never
        rewritten (no double-linking);
      * any internal error leaves the body unchanged — linking can never fail
        the whole answer.
    """
    if not body or not entities:
        return body
    try:
        # name -> set of node ids claiming it (across ALL evidence entities).
        claims: dict = {}
        for entity in entities:
            node_id = _entity_node_id(entity)
            url = poetrytalks_url(node_id) if node_id else None
            if not url:
                continue
            for name in _entity_names(entity):
                claims.setdefault(name, set()).add(node_id)

        # Only names that resolve to exactly ONE node id are linkable.
        linkable = {name: next(iter(ids))
                    for name, ids in claims.items() if len(ids) == 1}
        if not linkable:
            return body

        # Longest names first so "허난설헌" wins over a hypothetical "허난".
        for name in sorted(linkable, key=len, reverse=True):
            node_id = linkable[name]
            url = poetrytalks_url(node_id)
            spans = _protected_spans(body)
            start = 0
            while True:
                idx = body.find(name, start)
                if idx < 0:
                    break
                end = idx + len(name)
                # Skip occurrences inside links/code/URLs or link-text brackets.
                if _in_spans(idx, end, spans) or \
                        (idx > 0 and body[idx - 1] == "[") or \
                        (end < len(body) and body[end] == "]"):
                    start = end
                    continue
                body = body[:idx] + f"[{name}]({url})" + body[end:]
                break  # first valid mention only
        return body
    except Exception:
        logger.warning("body entity linking failed — returning unlinked body",
                       exc_info=True)
        return body


# ── Deterministic Sources rendering + final assembly ─────────────────────────
def render_sources_section(citations: list, language: str) -> str:
    """Render the code-owned Sources section. Empty citations → empty string
    (never an orphan header)."""
    lines = [c for c in citations or [] if isinstance(c, str) and c.strip()]
    if not lines:
        return ""
    return "## " + sources_header(language) + "\n" + "\n".join(lines)


def assemble_final_answer(
    body: str,
    citations: list,
    language: str,
    entities: Any = None,
    correlation_id: str = "",
) -> str:
    """Assemble the ONLY user-facing answer shape:

        sanitized (model-Sources-stripped, entity-linked) body
        blank line
        localized Sources header + exact deterministic citation bullets

    The LLM cannot omit, reorder, truncate, or rewrite the Sources content —
    whatever it wrote in a Sources section is discarded and replaced by the
    `build_citations()` output rendered here."""
    sanitized, removed = strip_model_sources(body or "")
    linked = link_entities_in_body(sanitized, entities)
    section = render_sources_section(citations, language)

    parts = [p for p in (linked.rstrip(), section) if p and p.strip()]
    final = "\n\n".join(parts)

    logger.debug(
        "answer assembly [%s]: body=%s model_sources_removed=%s "
        "final_source_bullets=%d",
        correlation_id or "-", bool(sanitized.strip()), removed,
        len([c for c in citations or [] if isinstance(c, str) and c.strip()]),
    )
    return final

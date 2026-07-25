"""Intent detection for graph-first ranking/aggregation questions.

Ranking/aggregation questions ("who is most mentioned", "가장 많이 언급된
왕은?") must never be routed through free-form LLM Cypher generation, nor
through the person-biography authority-cue path (tools/orchestrator's
`_PERSON_CUES`) — both contributed to the malformed backtick-label Cypher
and the unwanted 10-of-14-candidate authority fetch documented in
CLAUDE_CODE_GRAPH_RANKING_RELIABILITY_FIX.md. This module is the single
place that decides (a) "is this a ranking/aggregation question" and
(b) "which role/type Topic, if any, does it name", so tools/cypher.py and
tools/orchestrator.py stay in agreement instead of re-implementing cue lists.
"""

from __future__ import annotations

from typing import Optional

# Cue substrings for "is this a corpus-wide rank/count question". English
# cues are matched against the lowercased question; Korean/Chinese cues are
# matched as-is (those scripts do not have a meaningful case distinction).
_AGGREGATION_CUES_EN = (
    "most mentioned", "most frequently mentioned", "most frequent",
    "least mentioned", "least frequent", "top ", "top-", "how many times",
    "how many", "highest count", "highest number", "ranking", "rank ",
)
_AGGREGATION_CUES_KO = (
    "가장 많이 언급", "가장 많이", "가장 자주", "가장 적게", "상위", "순위",
    "언급 횟수", "몇 번", "몇번",
)
_AGGREGATION_CUES_ZH = (
    "最多", "最常", "排名", "次数最多", "幾次", "几次", "最少",
)


def is_graph_aggregation_intent(question: str) -> bool:
    """True when the question asks for a corpus-wide rank/count answer.

    Checked regardless of the session's locked response language — a
    Korean-phrased question can arrive while the UI is in English mode (and
    vice versa), so all three cue sets are always checked rather than only
    the one matching `effective_language`."""
    if not isinstance(question, str) or not question.strip():
        return False
    q_lower = question.lower()
    if any(cue in q_lower for cue in _AGGREGATION_CUES_EN):
        return True
    if any(cue in question for cue in _AGGREGATION_CUES_KO):
        return True
    if any(cue in question for cue in _AGGREGATION_CUES_ZH):
        return True
    return False


# ── Role/type cues → Topic lookup ────────────────────────────────────────
# Registry of "which Topic node does this role word mean" (work order
# §3-P0-B item 3). Verified live against the graph:
#   * HAS_TYPE -> Topic T1052 {nameKor:'왕', nameEng:'king', nameChi:'王'},
#     20 Person nodes connected.
#   * HAS_OFFICE has NO king/왕/王-matching Topic in this dataset — the
#     original failing Cypher's `HAS_OFFICE` assumption was wrong.
#
# IMPORTANT — these values are matched with EXACT equality (`=`), never
# CONTAINS, when resolving the Topic node. A live CONTAINS probe during
# implementation showed why: `nameKor CONTAINS '왕'` / `nameEng CONTAINS
# 'king'` / `nameChi CONTAINS '王'` also matches T1069 (queen consort, 5
# Person), T1070 (queen, 1 Person), T183 ("poems by kings and queens"), and
# even T527 ("kingfisher" — 'king' as a English substring) — silently
# inflating the "king" cohort with unrelated Topics. Exact equality against
# these three fields returns only T1052. Only the *cue* strings live here;
# the actual Topic node is always resolved at query time by
# `tools.cypher.resolve_role_topic_query` (parameterized, never hardcoded by
# id), so this stays correct even if the dataset's node ids change — but a
# newly added Topic with the exact same name string would need the same
# scrutiny this comment documents.
ROLE_TYPE_CUES = {
    "king": {
        "en": ("king",),
        "ko": ("왕",),
        "zh": ("王",),
    },
}

# The graph relationship that encodes a role/type Topic for a Person, per
# the live verification above. HAS_OFFICE is a real, populated relationship
# for other purposes (see tools/cypher.py's HAS_TYPE-usage rule) but does
# NOT carry the "king" Topic in this dataset.
ROLE_RELATIONSHIP = "HAS_TYPE"


def detect_role_cue(question: str) -> Optional[str]:
    """Return the first registered role key (e.g. "king") whose cue word(s)
    appear in the question, or None.

    Meaningful only together with `is_graph_aggregation_intent` — a bare
    role word alone does not imply a ranking question (e.g. "Tell me about
    King Sejong" names a role but asks a biography question, not a rank)."""
    if not isinstance(question, str) or not question.strip():
        return None
    q_lower = question.lower()
    for role, cues in ROLE_TYPE_CUES.items():
        if any(c in q_lower for c in cues["en"]):
            return role
        if any(c in question for c in cues["ko"]):
            return role
        if any(c in question for c in cues["zh"]):
            return role
    return None


# ── Canonical "mention count" definition ─────────────────────────────────
# Confirmed against the live graph (see IMPLEMENTATION_NOTE.md): comparing
# Entry-only HAS_SUBJECT_PERSON counts against Entry+Poem+Critique counts for
# the "king" cohort produced the SAME #1 (선조/Sŏnjo) under both definitions,
# with no tie in either case. Entry-only is used as the canonical unit
# because an Entry and the Poems/Critiques it HAS_PART can each separately
# carry a HAS_SUBJECT_PERSON edge to the same Person, which would double
# count one narrative mention if all three node types were summed together.
# Per work order §3-P0-B, this is the mandated fallback default in the
# absence of a separate user-confirmed definition, and is recorded here
# (not just in prose) as the single source of truth for both the ranking
# query builder and any future re-verification.
MENTION_COUNT_UNIT = "entry"


# ── Deterministic ranking template builder ───────────────────────────────
# Pure (no streamlit/neo4j/llm imports) so it is unit-testable without a
# live Neo4j — tools/cypher.py's `retrieve_role_ranking_evidence` is the
# thin, side-effecting wrapper that actually calls `_safe_graph.query()`.
DEFAULT_RANKING_LIMIT = 20


def build_role_ranking_query(role: str, limit: int = DEFAULT_RANKING_LIMIT):
    """Parameterized Cypher + params for "most mentioned <role>".

    `role` must be a key of `ROLE_TYPE_CUES`. The role's Topic is matched by
    EXACT equality on nameKor/nameEng/nameChi — never CONTAINS. A live probe
    during implementation showed CONTAINS '왕'/'king'/'王' also matches T1069
    (queen consort), T1070 (queen), T183 ("poems by kings and queens"), and
    even T527 ("kingfisher", via the English substring) — silently inflating
    the cohort with unrelated Topics; exact equality returns only the
    intended Topic (see ROLE_TYPE_CUES comment).

    Mentions are counted as DISTINCT Entry-level `HAS_SUBJECT_PERSON`
    relationships — the canonical unit confirmed against the live graph and
    recorded in `MENTION_COUNT_UNIT` (an Entry and the Poems/Critiques
    nested under it via HAS_PART can each separately carry a
    HAS_SUBJECT_PERSON edge to the same Person, which would double-count one
    narrative mention if all three node types were summed)."""
    cues = ROLE_TYPE_CUES[role]
    # NOTE: `limit` is embedded as a literal integer, not a `$limit`
    # parameter. Neo4j's LIMIT parameterization support is version-dependent
    # and `SafeNeo4jGraph._ensure_limit` (tools/cypher_safety.py) only
    # recognizes a trailing `LIMIT <digits>` when deciding whether to append
    # its own cap — a `LIMIT $limit` placeholder would be invisible to that
    # check and produce an invalid double-LIMIT query. `limit` is always an
    # int from this module's own call sites, never raw user text, so
    # interpolating it here carries no injection risk.
    cypher = f"""
MATCH (p:Person)-[:{ROLE_RELATIONSHIP}]->(topic:Topic)
WHERE topic.nameKor = $topic_ko OR topic.nameEng = $topic_en OR topic.nameChi = $topic_zh
MATCH (e:Entry)-[:HAS_SUBJECT_PERSON]->(p)
RETURN p.ID AS person_id,
       p.nameKor AS person_name_kor,
       p.nameChi AS person_name_chi,
       p.nameEng AS person_name_eng,
       p.nameMR AS person_name_mr,
       p.idWikidata AS wikidata_id,
       p.idAKSdigerati AS aks_digerati_id,
       p.idAKSency AS aks_ency_id,
       p.idLOC AS loc_id,
       count(DISTINCT e) AS mention_count
ORDER BY mention_count DESC
LIMIT {int(limit)}
"""
    params = {
        "topic_ko": cues["ko"][0],
        "topic_en": cues["en"][0],
        "topic_zh": cues["zh"][0],
    }
    return cypher, params


def rank_rows_by_mention_count(rows: list) -> tuple:
    """(top_count, [row, ...]) — winners are every row tied for the highest
    mention_count. `rows` must already be ORDER BY mention_count DESC (as
    `build_role_ranking_query` produces), so the first row's count is the
    maximum. Returns (None, []) for an empty input."""
    if not rows:
        return None, []
    top_count = rows[0].get("mention_count")
    winners = [r for r in rows if r.get("mention_count") == top_count]
    return top_count, winners

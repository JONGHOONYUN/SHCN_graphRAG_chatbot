"""Internal Poetry Talks node-id registry and URL scheme (single source of truth).

책임: node ID prefix registry, shape validation, poetrytalks URL 생성.
입력/출력: 순수 문자열/값. 외부 부작용: 없음 (환경변수 1회 read).
허용 의존성: 표준 라이브러리만.
기존 facade: tools/evidence.py (모든 심볼 re-export).

Moved verbatim from tools/evidence.py (modularization work order Phase 2.2) —
prefixes, 정규식, fail-closed 동작 불변.
"""

from __future__ import annotations

import logging
import os as _os
import re as _re
from typing import Any, Optional

# 기존 운영 로그/테스트(assertLogs("tools.evidence"))와의 연속성을 위해 원래
# facade의 logger 이름을 유지한다 — 새 이름의 두 번째 로그 계보를 만들지 않는다.
logger = logging.getLogger("tools.evidence")


ENTITY_TYPES = (
    "Person", "Work", "Entry", "Poem", "Critique", "Place", "Topic", "Era",
)
SOURCE_TYPES = (
    "neo4j_graph", "neo4j_vector", "wikidata", "aks_digerati", "aks_digerati_place",
    "loc", "open_library", "cbdb", "yale_lux", "aks_ency", "britannica", "bnf",
    "world_history",
)
EVIDENCE_KINDS = ("graph", "vector", "external")

# Node types eligible for external authority enrichment.
ENRICHABLE_TYPES = ("Person", "Place")

# ──────────────────────────────────────────────
# Poetry Talks wiki URL scheme  ("poetrytalks wikidata")
#
# EVERY graph node — regardless of class — has an `id` property on the Neo4j
# node whose value resolves deterministically to
# `<POETRYTALKS_BASE_URL><id>`. The final Sources section refers to these
# URLs as **"poetrytalks wikidata"** links (per user's naming convention);
# they are the canonical, unconditional reference URL for any node cited in
# an answer.
#
# AUTHORITATIVE DOMAIN DECISION (all-node-coverage work order, Phase 0):
# the repository, deployment, and prior behavior all use
# `https://poetrytalks.org/`; the alternative spelling `poetrtalks.org` in a
# requirement draft has NO DNS record (verified) and is treated as a typo.
# The base URL is a SINGLE configurable constant here — override via the
# POETRYTALKS_BASE_URL environment variable; every prompt, regex, and
# renderer derives from this value rather than hardcoding the domain.
#
# NODE ID SCHEMA (measured against neo4j_import_nodes.jsonl — 8,232 ids):
# a registered prefix of 1–2 uppercase letters followed by 1–4 digits.
# The digit ceiling and the prefix ALLOWLIST both reject external-authority
# IDs (`E0063034` 7-digit idAKSency values, `Q464558` Wikidata Q-ids, ...);
# column-name filtering (`_is_external_id_key`) provides a second guard.
# ──────────────────────────────────────────────
POETRYTALKS_BASE_URL = (
    _os.environ.get("POETRYTALKS_BASE_URL", "https://poetrytalks.org/").strip()
)
if not POETRYTALKS_BASE_URL.endswith("/"):
    POETRYTALKS_BASE_URL += "/"

# Legacy alias — kept for existing imports; same object, single source.
POETRYTALKS_BASE = POETRYTALKS_BASE_URL

# Canonical citation category name for these URLs.
POETRYTALKS_WIKIDATA_LABEL = "poetrytalks wikidata"

# Data-derived node-id prefix registry (single source of truth). Keys are the
# literal ID prefixes found in the source data; values are the node classes.
NODE_ID_PREFIXES = {
    "B": "Work",
    "E": "Entry",
    "M": "Poem",
    "C": "Critique",
    "P": "Person",
    "L": "Place",
    "T": "Topic",
    "H": "Era",
    "CT": "CriticalTerm",
}

# 1–2 uppercase letters + 1–4 digits. Longest-prefix match ("CT017" → CT,
# "C017" → C) keeps Critique and CriticalTerm ids unambiguous.
_NODE_ID_RE = _re.compile(r"^([A-Z]{1,2})(\d{1,4})$")


def split_node_id(value: Any) -> Optional[tuple]:
    """Return (prefix, digits) for a registered node id, else None.

    Fail-closed: unregistered prefixes ('Q464558', 'K123'), 5+ digit suffixes
    ('E0063034'), lowercase, and non-strings all return None."""
    if not isinstance(value, str):
        return None
    m = _NODE_ID_RE.match(value.strip())
    if not m:
        return None
    prefix = m.group(1)
    if prefix not in NODE_ID_PREFIXES:
        return None
    return prefix, m.group(2)


def is_valid_node_id(value: Any) -> bool:
    """True if `value` is a registered-prefix node id (see NODE_ID_PREFIXES).

    Covers every node class in the source data, including CriticalTerm's
    two-letter `CT###` ids (692 ids that the previous single-letter rule
    dropped)."""
    return split_node_id(value) is not None


def node_type_for_id(value: Any) -> Optional[str]:
    """Node class name ('Person', 'CriticalTerm', …) for a valid id, else None."""
    parts = split_node_id(value)
    return NODE_ID_PREFIXES[parts[0]] if parts else None


def poetrytalks_url(node_id: Any) -> Optional[str]:
    """Return the Poetry Talks wiki URL ("poetrytalks wikidata" link) for any
    node ID matching the canonical shape. Returns None for anything else —
    callers must never fabricate a URL from an unrelated string (e.g.
    `idAKSency` values, Wikidata Q-ids)."""
    if not isinstance(node_id, str):
        return None
    node_id = node_id.strip()
    if not node_id:
        return None
    if is_valid_node_id(node_id):
        return POETRYTALKS_BASE_URL + node_id
    return None


def _linked_id(node_id: Optional[str]) -> str:
    """Format a node ID as a markdown link if it fits the node-id shape,
    else return it verbatim.

    CONTRACT (work order Phase 2): empty/None returns '' — NEVER a user-facing
    '?' placeholder. Callers must omit the ID portion entirely when this
    returns an empty string; `(?)`, `(None)`, `[?](...)` must not exist in any
    user-visible provenance or citation."""
    if not node_id:
        return ""
    url = poetrytalks_url(node_id)
    return f"[{node_id}]({url})" if url else str(node_id)


def normalize_entry_position(value: Any) -> Optional[int]:
    """Normalize a retrieved Entry position to a positive int, else None.

    None / 0 / negative / bool / non-numeric values are all treated as
    'position unknown' — a position of 0 is not a valid ordinal in this
    corpus, and `Entry 0` must never render as normal provenance."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            iv = int(stripped)
            return iv if iv > 0 else None
    return None

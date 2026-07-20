"""Cypher read-only validator (application-layer defence).

`allow_dangerous_requests=True` on LangChain's GraphCypherQAChain is merely an
acknowledgement that the LLM may emit any Cypher — it is NOT a safety switch.
This module enforces a strict read-only policy on every LLM-generated Cypher
before it reaches Neo4j.

Layered defence:
  1. `validate_read_only_cypher(query)` inspects a Cypher string, strips
     comments and string literals (so keyword matches inside data are ignored),
     and rejects anything containing a write / schema / permission / dynamic
     keyword or an un-allowlisted CALL. It also caps the outer result set with
     a `LIMIT` if the query has none.
  2. `SafeNeo4jGraph` wraps `Neo4jGraph`; its `.query()` runs the validator
     before delegating to the wrapped graph. Passed to `GraphCypherQAChain`
     via `graph=safe_graph(...)`, so both `cypher_qa` and
     `cypher_qa_structured` are protected transparently without touching
     `Neo4jChatMessageHistory` (which legitimately CREATEs history nodes and
     must NOT be validated).
  3. `UnsafeCypherError` deliberately does NOT carry the offending query — the
     caller logs a correlation code + short reason; the raw text never enters
     the synthesis prompt, user output, or Streamlit exception page.

Neo4j read-only account remains the primary defence. This module is a
defense-in-depth so a compromised or over-privileged account is still safe.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Configurable row cap ─────────────────────────────────────────────────────
# The Cypher generator sometimes omits LIMIT. We enforce a bounded cap to
# prevent runaway result sets that would starve the LLM and Neo4j alike.
DEFAULT_MAX_ROWS = 50


# ── Forbidden keywords (case-insensitive) ────────────────────────────────────
# These constitute the complete write / schema / permission / dynamic surface
# in Cypher 5. Any token match against the stripped query is an immediate
# rejection. Case is normalized to upper before comparison.
_FORBIDDEN_KEYWORDS = frozenset({
    # Data mutation
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE",
    # Schema / admin
    "DROP", "ALTER", "RENAME",
    # Permissions
    "GRANT", "DENY", "REVOKE",
    # I/O and update-loops
    "LOAD", "FOREACH",
    # Database switching / execution context
    "USE",
})


# ── CALL allowlist ───────────────────────────────────────────────────────────
# CALL is rejected by default (both procedure form AND CALL{...} subquery
# form). Extend only with vetted read-only procedures — never wildcards.
# Format: lowercase dotted procedure name.
_CALL_ALLOWLIST: frozenset = frozenset()  # e.g. {"db.labels", "db.propertyKeys"}


class UnsafeCypherError(ValueError):
    """Raised when a Cypher query fails the read-only validator.

    The offending query is NOT stored on the exception. Callers log a short
    reason with a correlation id, and never propagate query text to the LLM
    or the user."""

    def __init__(self, reason: str, correlation_id: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.correlation_id = correlation_id or uuid.uuid4().hex[:8]


# ── String / comment stripping ───────────────────────────────────────────────
# Perform in this order so an escaped quote inside a string doesn't leak into
# the next state:
#   1. `//` line comments  → whitespace
#   2. `/* ... */` block comments → whitespace
#   3. Double-quoted strings → `""` (contents removed)
#   4. Single-quoted strings → `''` (contents removed)
#   5. Backtick-quoted identifiers → `__IDENT__` (contents removed so an LLM
#      that once wrote `Entry OR text`:Poem can't smuggle keywords through)
_STRING_LITERAL_PATTERNS = (
    (re.compile(r"//[^\n]*"),                    " "),
    (re.compile(r"/\*.*?\*/", re.DOTALL),        " "),
    (re.compile(r'"(?:\\.|[^"\\])*"'),           ' "" '),
    (re.compile(r"'(?:\\.|[^'\\])*'"),           " '' "),
    (re.compile(r"`[^`]*`"),                     " __IDENT__ "),
)


def _strip_strings_and_comments(query: str) -> str:
    """Return `query` with comments and string / backtick literal contents
    replaced by placeholders. Idempotent."""
    for pattern, replacement in _STRING_LITERAL_PATTERNS:
        query = pattern.sub(replacement, query)
    return query


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|;|\{|\}|\*")


def _tokenize_upper(stripped: str):
    """Yield uppercase tokens from a stripped query."""
    for match in _TOKEN_RE.finditer(stripped):
        yield match.group(0).upper()


def validate_read_only_cypher(query: str,
                              max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Return the query (possibly LIMIT-augmented) if it is read-only.
    Raise `UnsafeCypherError` otherwise.

    Rejects:
      * non-string / empty
      * multi-statement (any interior `;`)
      * any forbidden write / schema / permission / dynamic keyword
      * any CALL (procedure or subquery) not in `_CALL_ALLOWLIST`
      * queries with no RETURN clause (nothing to read)

    Enforces:
      * an outer LIMIT ≤ `max_rows` (added if absent, lowered if too large)
    """
    if not isinstance(query, str):
        raise UnsafeCypherError("query is not a string")

    stripped = _strip_strings_and_comments(query).strip()
    if not stripped:
        raise UnsafeCypherError("query is empty after stripping")

    tokens = list(_tokenize_upper(stripped))

    # Multi-statement rejection: any semicolon that is not the trailing one.
    # (A single trailing semicolon is tolerated.)
    if ";" in tokens:
        # Position of last ';': anything after it besides whitespace/tokens is
        # a second statement. Since tokens ignores whitespace, if any token
        # follows the last ';' there is a second statement. And any ';' NOT at
        # the tail means either (a) another ';' follows OR (b) real tokens
        # follow → multi-statement.
        last_semi = len(tokens) - 1 - tokens[::-1].index(";")
        if any(t != ";" for t in tokens[last_semi + 1:]):
            raise UnsafeCypherError("multi-statement query not allowed")
        if tokens.count(";") > 1:
            raise UnsafeCypherError("multi-statement query not allowed")

    upper = set(tokens)

    banned = upper & _FORBIDDEN_KEYWORDS
    if banned:
        raise UnsafeCypherError(
            f"forbidden keyword(s) present: {sorted(banned)}")

    # CALL — enforce allowlist for procedure form; reject subquery form
    # outright (the read-only Cypher we generate does not need CALL{...}).
    for i, tok in enumerate(tokens):
        if tok != "CALL":
            continue
        # Walk forward to the next meaningful token.
        following = None
        for nxt in tokens[i + 1:]:
            following = nxt
            break
        if following == "{":
            raise UnsafeCypherError("CALL { ... } subqueries are not allowed")
        # Procedure name: consecutive identifier tokens (no delimiter yet).
        proc_parts = []
        for nxt in tokens[i + 1:]:
            if nxt in (";", "{", "*") or nxt in _FORBIDDEN_KEYWORDS:
                break
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", nxt):
                break
            proc_parts.append(nxt.lower())
            if len(proc_parts) >= 6:
                break
        proc_name = ".".join(proc_parts)
        if proc_name not in _CALL_ALLOWLIST:
            raise UnsafeCypherError(
                f"CALL procedure not in allowlist: {proc_name or '(none)'}")

    if "RETURN" not in upper:
        raise UnsafeCypherError("query has no RETURN clause")

    return _ensure_limit(query, max_rows)


# ── LIMIT enforcement ────────────────────────────────────────────────────────
_TRAILING_LIMIT_RE = re.compile(
    r"\bLIMIT\s+(\d+)\s*;?\s*$", re.IGNORECASE)


def _ensure_limit(query: str, max_rows: int) -> str:
    """Ensure the outer query has a `LIMIT` ≤ `max_rows`.

    Only the tail of the (stripped) query is inspected — a LIMIT inside a
    subquery / pattern does not count as the outer bound. If the tail LIMIT is
    absent, we append `LIMIT max_rows`. If it exceeds max_rows we rewrite it.
    """
    tail_stripped = _strip_strings_and_comments(query).rstrip()
    m = _TRAILING_LIMIT_RE.search(tail_stripped)
    if m is not None:
        current = int(m.group(1))
        if current > max_rows:
            return _TRAILING_LIMIT_RE.sub(f"LIMIT {max_rows}", query.rstrip())
        return query
    body = query.rstrip().rstrip(";")
    return f"{body}\nLIMIT {max_rows}"


# ── SafeNeo4jGraph proxy ─────────────────────────────────────────────────────
# Two variants are provided:
#
# 1. `SafeNeo4jGraph` — plain-Python proxy. Used by unit tests and by any
#    caller passing a mock. It never inherits from Neo4jGraph, so pydantic
#    validation against `GraphStore` would fail. For that reason production
#    code MUST wrap real Neo4jGraph instances via `safe_graph()`, which
#    picks the pydantic-compatible subclass variant below.
#
# 2. `_SafeNeo4jGraphSubclass` — subclass of Neo4jGraph. Its __init__ copies
#    the already-connected inner instance's __dict__ instead of chaining to
#    Neo4jGraph.__init__ (which would open a second Bolt driver). This
#    variant IS an instance of GraphStore, so it passes pydantic isinstance
#    validation on `GraphCypherQAChain(graph=...)`.
#
# `safe_graph()` dispatches: real Neo4jGraph → subclass variant; anything
# else (test doubles) → plain-Python proxy.

class SafeNeo4jGraph:
    """Plain-Python proxy variant. Wraps ANY object exposing a `.query()`
    method (real Neo4jGraph or a test mock) and enforces read-only
    validation. Not a `GraphStore` subclass — use `safe_graph()` when
    handing the result to pydantic-validated LangChain chains."""

    def __init__(self, inner: Any, max_rows: int = DEFAULT_MAX_ROWS):
        self._inner = inner
        self._max_rows = max_rows

    def query(self, query: str, params: Optional[dict] = None):
        try:
            safe_query = validate_read_only_cypher(query, self._max_rows)
        except UnsafeCypherError as e:
            logger.warning(
                "blocked unsafe cypher [%s]: %s",
                e.correlation_id, e.reason,
            )
            raise
        if params is None:
            return self._inner.query(safe_query)
        return self._inner.query(safe_query, params=params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


try:
    from langchain_neo4j import Neo4jGraph as _Neo4jGraph  # type: ignore

    class _SafeNeo4jGraphSubclass(_Neo4jGraph):  # type: ignore[misc]
        """Pydantic-compatible variant. Same read-only guarantees as
        `SafeNeo4jGraph`, but it IS a `Neo4jGraph` — so
        `GraphCypherQAChain(graph=...)` accepts it."""

        def __init__(self, inner: Any,
                     max_rows: int = DEFAULT_MAX_ROWS) -> None:
            # Skip Neo4jGraph.__init__ — it would open a second driver and
            # re-probe the schema. Clone the connected inner's state so all
            # reads and the driver reference remain identical.
            for key, value in inner.__dict__.items():
                object.__setattr__(self, key, value)
            object.__setattr__(self, "_max_rows", max_rows)

        def query(self, query: str, params: Optional[dict] = None):  # type: ignore[override]
            try:
                safe_query = validate_read_only_cypher(
                    query, object.__getattribute__(self, "_max_rows"))
            except UnsafeCypherError as e:
                logger.warning(
                    "blocked unsafe cypher [%s]: %s",
                    e.correlation_id, e.reason,
                )
                raise
            if params is None:
                return super().query(safe_query)
            return super().query(safe_query, params=params)

    _HAVE_NEO4J = True

except ImportError:
    _HAVE_NEO4J = False


def safe_graph(inner: Any, max_rows: int = DEFAULT_MAX_ROWS):
    """Return a read-only wrapper around `inner`.

    Dispatches on `inner`'s type:
      * Real `Neo4jGraph` instance → `_SafeNeo4jGraphSubclass`, which is a
        `Neo4jGraph` subclass and passes pydantic `isinstance(graph, ...)`
        validation on `GraphCypherQAChain`.
      * Anything else (unit-test doubles) → plain `SafeNeo4jGraph` proxy.

    Both variants call the same `validate_read_only_cypher` and reject writes
    identically."""
    if _HAVE_NEO4J and isinstance(inner, _Neo4jGraph):
        return _SafeNeo4jGraphSubclass(inner, max_rows=max_rows)
    return SafeNeo4jGraph(inner, max_rows=max_rows)

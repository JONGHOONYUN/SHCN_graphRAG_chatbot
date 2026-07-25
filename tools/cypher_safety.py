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
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Bounded read-only retry for transient Bolt/transport failures ───────────
# (graph-ranking-reliability work order §3 P1). Applies ONLY inside this
# module's `.query()` wrappers — i.e. only to read-only graph retrieval
# (free-form LLM Cypher via GraphCypherQAChain, and the deterministic ranking
# template in tools/cypher.py both route through here). It is never applied
# to `Neo4jChatMessageHistory` (writes) or external-authority HTTP fetches,
# because neither goes through `SafeNeo4jGraph`/`_SafeNeo4jGraphSubclass`.
#
# Overridable via environment variables so ops can tune without a redeploy;
# invalid/missing values fall back to the documented defaults below.
def _retry_env_int(name: str, default: int) -> int:
    import os
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _retry_env_float(name: str, default: float) -> float:
    import os
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


READ_RETRY_MAX_ATTEMPTS = _retry_env_int("GRAPH_READ_RETRY_MAX_ATTEMPTS", 3)
READ_RETRY_BACKOFF_SECONDS = _retry_env_float("GRAPH_READ_RETRY_BACKOFF_SECONDS", 0.5)

try:
    from neo4j.exceptions import (  # type: ignore
        ServiceUnavailable as _ServiceUnavailable,
        SessionExpired as _SessionExpired,
        TransientError as _TransientError,
    )

    READ_RETRY_EXCEPTIONS: tuple = (
        _ServiceUnavailable, _SessionExpired, _TransientError, OSError,
    )
except ImportError:
    # neo4j not installed in this environment (e.g. a lightweight unit-test
    # sandbox) — retry on OSError only; test doubles raise arbitrary
    # exceptions that must NOT be caught/retried here.
    READ_RETRY_EXCEPTIONS = (OSError,)


def _read_with_retry(fn, correlation_id: str):
    """Run `fn()` with a bounded retry on transient transport failures.

    Retries ONLY `READ_RETRY_EXCEPTIONS` (Bolt/session transport-level —
    ServiceUnavailable, SessionExpired, TransientError, OSError), up to
    `READ_RETRY_MAX_ATTEMPTS` total attempts, with a doubling backoff starting
    at `READ_RETRY_BACKOFF_SECONDS`. Any other exception (including
    `UnsafeCypherError`, `CypherSyntaxError`, or a plain `ClientError`)
    propagates on the FIRST attempt — those are not transport failures and
    retrying them would just repeat the same rejection or logic error."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except READ_RETRY_EXCEPTIONS as e:
            if attempt >= READ_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "read-only graph query [%s] failed after %d attempt(s), "
                    "giving up: %s: %s",
                    correlation_id, attempt, type(e).__name__, e,
                )
                raise
            delay = READ_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.info(
                "read-only graph query [%s] transient failure on attempt "
                "%d/%d (%s: %s) — retrying in %.1fs",
                correlation_id, attempt, READ_RETRY_MAX_ATTEMPTS,
                type(e).__name__, e, delay,
            )
            time.sleep(delay)


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


# ── Machine-classifiable rejection reasons (graph-ranking-reliability work
# order, P0-A §3) ─────────────────────────────────────────────────────────────
# `reason_code` lets callers branch on WHY a query was rejected without
# parsing `reason` strings. `RECOVERABLE_REASON_CODES` names the shapes that
# are a plausible LLM slip (fixable by regenerating once with a hint) rather
# than a deliberate/dangerous query — those never get a second chance.
REASON_MALFORMED_LABEL_PREDICATE = "malformed_label_predicate"
REASON_MISSING_RETURN = "missing_return"
REASON_FORBIDDEN_KEYWORD = "forbidden_keyword"
REASON_DISALLOWED_CALL = "disallowed_call"
REASON_MULTI_STATEMENT = "multi_statement"
REASON_EMPTY_QUERY = "empty_query"
REASON_NOT_A_STRING = "not_a_string"

RECOVERABLE_REASON_CODES = frozenset({
    REASON_MALFORMED_LABEL_PREDICATE,
    REASON_MISSING_RETURN,
})


class UnsafeCypherError(ValueError):
    """Raised when a Cypher query fails the read-only validator.

    The offending query is NOT stored on the exception. Callers log a short
    reason with a correlation id, and never propagate query text to the LLM
    or the user. `reason_code` (see REASON_* constants above) lets callers
    decide, without string-matching, whether the rejection is a recoverable
    query-shape slip or a deliberate/dangerous query that must never be
    retried."""

    def __init__(self, reason: str, correlation_id: Optional[str] = None,
                 reason_code: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.reason_code = reason_code
        self.correlation_id = correlation_id or uuid.uuid4().hex[:8]

    @property
    def recoverable(self) -> bool:
        """True iff this rejection is a plausible query-SHAPE slip (malformed
        backtick label predicate, missing RETURN) that a single, hint-guided
        regeneration may fix. False for anything write/schema/permission/
        dynamic-related — those are never retried, only blocked."""
        return self.reason_code in RECOVERABLE_REASON_CODES


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


# ── Malformed multi-label backtick predicate (P0-A §1/§2) ───────────────────
# Observed failure: an LLM wrote `text:\`Entry OR text\`:Poem OR text:Critique`,
# intending "text has label Entry OR Poem OR Critique" but instead producing a
# SINGLE backtick-quoted label literally named "Entry OR text". Neo4j accepts
# this as valid syntax (it's just an unusual label name) and returns zero rows
# with an UnknownLabelWarning — which looks exactly like a normal empty result
# unless specifically detected. Because the multi-hop label filter here landed
# adjacent to `RETURN`, the backtick swallowed the RETURN keyword too,
# producing the OTHER observed symptom ("query has no RETURN clause").
#
# Detection runs on the RAW query, BEFORE string/comment stripping, and is
# NARROW by design (§3-P0-A-2 — do not broadly block ordinary backtick-quoted
# identifiers): it fires only on the exact signature of the observed abuse —
# a backtick-quoted identifier that (a) contains the word "OR" AND (b) is
# IMMEDIATELY followed by another `:label` — i.e. a second label predicate was
# chained onto what should have been a closed, single label. A backtick
# identifier that merely CONTAINS "OR" but is not followed by another `:`
# (e.g. an odd-but-harmless single label like `` n:`OR DELETE` ``, whose
# contents are neutralized by string/comment stripping regardless) is NOT
# malformed and must keep passing — this is exactly what
# TestValidatorRejectsBypasses.test_backtick_label_cannot_smuggle_keyword
# already pins down.
_MALFORMED_LABEL_BACKTICK_RE = re.compile(r"`[^`]*\bOR\b[^`]*`\s*:")


def _detect_malformed_label_predicate(raw_query: str) -> bool:
    """True if `raw_query` contains a backtick-quoted identifier containing
    'OR' that is immediately chained to another `:label` — the multi-label-
    filter-abuse shape. Runs on the UNSTRIPPED query so it fires even when the
    malformed backtick would otherwise swallow the RETURN keyword during
    stripping."""
    if not isinstance(raw_query, str):
        return False
    return bool(_MALFORMED_LABEL_BACKTICK_RE.search(raw_query))


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
      * malformed multi-label backtick predicate (checked BEFORE stripping —
        recoverable: a single safe-hint regeneration may fix it)
      * multi-statement (any interior `;`)
      * any forbidden write / schema / permission / dynamic keyword
      * any CALL (procedure or subquery) not in `_CALL_ALLOWLIST`
      * queries with no RETURN clause (nothing to read — recoverable)

    Enforces:
      * an outer LIMIT ≤ `max_rows` (added if absent, lowered if too large)

    Every rejection carries a `reason_code` (see REASON_* constants) so
    callers can decide whether a single, hint-guided regeneration is
    warranted (`UnsafeCypherError.recoverable`) without string-matching.
    """
    if not isinstance(query, str):
        raise UnsafeCypherError("query is not a string",
                                reason_code=REASON_NOT_A_STRING)

    # Malformed multi-label backtick predicate: detected on the RAW query,
    # before stripping, so it fires even when the malformed backtick would
    # otherwise swallow RETURN during stripping (see docstring above
    # `_detect_malformed_label_predicate`).
    if _detect_malformed_label_predicate(query):
        raise UnsafeCypherError(
            "backtick-quoted label predicate contains 'OR' or ':' — "
            "multi-label filter must use separate label predicates",
            reason_code=REASON_MALFORMED_LABEL_PREDICATE)

    stripped = _strip_strings_and_comments(query).strip()
    if not stripped:
        raise UnsafeCypherError("query is empty after stripping",
                                reason_code=REASON_EMPTY_QUERY)

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
            raise UnsafeCypherError("multi-statement query not allowed",
                                    reason_code=REASON_MULTI_STATEMENT)
        if tokens.count(";") > 1:
            raise UnsafeCypherError("multi-statement query not allowed",
                                    reason_code=REASON_MULTI_STATEMENT)

    upper = set(tokens)

    banned = upper & _FORBIDDEN_KEYWORDS
    if banned:
        raise UnsafeCypherError(
            f"forbidden keyword(s) present: {sorted(banned)}",
            reason_code=REASON_FORBIDDEN_KEYWORD)

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
            raise UnsafeCypherError("CALL { ... } subqueries are not allowed",
                                    reason_code=REASON_DISALLOWED_CALL)
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
                f"CALL procedure not in allowlist: {proc_name or '(none)'}",
                reason_code=REASON_DISALLOWED_CALL)

    if "RETURN" not in upper:
        raise UnsafeCypherError("query has no RETURN clause",
                                reason_code=REASON_MISSING_RETURN)

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
                "blocked unsafe cypher [%s] reason_code=%s: %s",
                e.correlation_id, e.reason_code, e.reason,
            )
            raise
        correlation_id = uuid.uuid4().hex[:8]
        if params is None:
            return _read_with_retry(
                lambda: self._inner.query(safe_query), correlation_id)
        return _read_with_retry(
            lambda: self._inner.query(safe_query, params=params), correlation_id)

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
                    "blocked unsafe cypher [%s] reason_code=%s: %s",
                    e.correlation_id, e.reason_code, e.reason,
                )
                raise
            correlation_id = uuid.uuid4().hex[:8]
            sup = super()
            if params is None:
                return _read_with_retry(
                    lambda: sup.query(safe_query), correlation_id)
            return _read_with_retry(
                lambda: sup.query(safe_query, params=params), correlation_id)

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

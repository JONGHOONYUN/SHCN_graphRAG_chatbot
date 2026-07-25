"""Phase 1 — Cypher read-only validator tests.

Direct unit tests of `tools.cypher_safety.validate_read_only_cypher` and
`SafeNeo4jGraph`. Includes the exact patterns the work order lists as MUST
BLOCK (Section 4 Phase 1 필수 테스트) plus a set of adversarial variants.
"""

import unittest
from unittest.mock import MagicMock

from tools.cypher_safety import (
    RECOVERABLE_REASON_CODES,
    REASON_DISALLOWED_CALL,
    REASON_EMPTY_QUERY,
    REASON_FORBIDDEN_KEYWORD,
    REASON_MALFORMED_LABEL_PREDICATE,
    REASON_MISSING_RETURN,
    REASON_MULTI_STATEMENT,
    REASON_NOT_A_STRING,
    UnsafeCypherError,
    SafeNeo4jGraph,
    safe_graph,
    validate_read_only_cypher,
)


class TestValidatorAllowsReadOnly(unittest.TestCase):
    """Legitimate read-only queries must pass through unchanged (except LIMIT
    normalization) and reach the underlying graph."""

    def test_match_return_limit(self):
        q = "MATCH (p:Person) RETURN p LIMIT 20"
        self.assertEqual(validate_read_only_cypher(q), q)

    def test_optional_match_with_where(self):
        q = ("MATCH (p:Person) OPTIONAL MATCH (p)-[:HAS_CREATOR]->(w:Work) "
             "WHERE p.yearBirth > 1500 RETURN p, w LIMIT 20")
        self.assertEqual(validate_read_only_cypher(q), q)

    def test_unwind_and_aggregation(self):
        q = ("UNWIND [1,2,3] AS x WITH x MATCH (n) WHERE n.id CONTAINS "
             "toString(x) RETURN count(n) LIMIT 10")
        self.assertEqual(validate_read_only_cypher(q), q)

    def test_string_literals_containing_forbidden_words_pass(self):
        # 'SET' / 'DELETE' appear in string literals but are NOT keywords here.
        q = 'MATCH (p:Person) WHERE p.nameKor = "SET DELETE" RETURN p LIMIT 5'
        self.assertEqual(validate_read_only_cypher(q), q)

    def test_apostrophe_style_string(self):
        q = "MATCH (p:Person) WHERE p.nameEng = 'don''t merge' RETURN p LIMIT 5"
        # Note: '' inside '...' is Cypher's escaped apostrophe — the outer
        # string swallows it. `MERGE` inside is protected by the string strip.
        # The validator MUST NOT falsely reject.
        self.assertEqual(validate_read_only_cypher(q), q)


class TestValidatorRejectsWrites(unittest.TestCase):
    def test_create(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("CREATE (n:Person {nameKor: 'x'}) RETURN n")

    def test_merge(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("MERGE (n:Person {id: 'P001'}) RETURN n")

    def test_set(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "MATCH (p:Person) SET p.nameKor = 'x' RETURN p")

    def test_delete(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("MATCH (p:Person) DELETE p")

    def test_detach_delete(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("MATCH (p:Person) DETACH DELETE p")

    def test_remove(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "MATCH (p:Person) REMOVE p.nameKor RETURN p")

    def test_drop(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("DROP INDEX my_index")

    def test_load_csv(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "LOAD CSV FROM 'http://x' AS r RETURN r")

    def test_foreach(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "MATCH (p:Person) FOREACH (x IN [1,2,3] | SET p.n = x)")

    def test_grant(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("GRANT READ ON GRAPH * TO role")

    def test_use_switch(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("USE system MATCH (n) RETURN n")


class TestValidatorRejectsBypasses(unittest.TestCase):
    """Adversarial variants the LLM has produced or could produce."""

    def test_lowercase_keyword(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("match (p:Person) delete p")

    def test_mixed_case(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "MATCH (p:Person) DeTaCh DeLeTe p")

    def test_multiline_write(self):
        query = "MATCH (p:Person)\nSET\n  p.nameKor = 'x'\nRETURN p"
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(query)

    def test_comment_hidden_write_not_hidden(self):
        # A block comment /* MATCH ... */ would strip out the MATCH but a
        # write keyword OUTSIDE the comment must still be caught.
        query = "/* MATCH ok */ CREATE (n:Person) RETURN n"
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(query)

    def test_line_comment_before_delete(self):
        query = "MATCH (p:Person) // safe read\nDELETE p"
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(query)

    def test_multi_statement_semicolon(self):
        query = "MATCH (p:Person) RETURN p LIMIT 5; MATCH (n) RETURN n"
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(query)

    def test_multi_statement_second_hidden_write(self):
        query = "MATCH (p) RETURN p; CREATE (n:Bad) RETURN n"
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(query)

    def test_trailing_semicolon_allowed(self):
        query = "MATCH (p:Person) RETURN p LIMIT 5;"
        # Just a trailing terminator — this should pass. LIMIT already ≤ cap.
        result = validate_read_only_cypher(query)
        self.assertIn("RETURN p", result)

    def test_backtick_label_cannot_smuggle_keyword(self):
        # The LLM has been known to write `Entry OR text`:Poem — the backtick
        # contents are stripped by the validator so no keyword smuggling.
        query = "MATCH (n) WHERE n:`OR DELETE` RETURN n LIMIT 5"
        # The `DELETE` inside backticks is neutralized, but there is NO
        # forbidden token outside strings/backticks/comments → this passes.
        result = validate_read_only_cypher(query)
        self.assertIn("RETURN n", result)


class TestValidatorRejectsCall(unittest.TestCase):
    def test_arbitrary_call_procedure(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "CALL db.labels() YIELD label RETURN label LIMIT 5")

    def test_apoc_dynamic_call(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "CALL apoc.cypher.doIt('CREATE (n) RETURN n', {}) YIELD value "
                "RETURN value")

    def test_call_subquery_form_rejected(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher(
                "CALL { MATCH (n) RETURN n LIMIT 1 } RETURN n LIMIT 5")


class TestValidatorRequiresReturn(unittest.TestCase):
    def test_no_return_rejected(self):
        with self.assertRaises(UnsafeCypherError):
            validate_read_only_cypher("MATCH (p:Person) WHERE p.id = 'P001'")


class TestLimitEnforcement(unittest.TestCase):
    def test_adds_limit_when_missing(self):
        q = "MATCH (p:Person) RETURN p"
        result = validate_read_only_cypher(q, max_rows=50)
        self.assertRegex(result, r"(?is)LIMIT\s+50\s*$")

    def test_lowers_excessive_limit(self):
        q = "MATCH (p:Person) RETURN p LIMIT 100000"
        result = validate_read_only_cypher(q, max_rows=50)
        self.assertRegex(result, r"(?is)LIMIT\s+50\s*$")

    def test_keeps_acceptable_limit(self):
        q = "MATCH (p:Person) RETURN p LIMIT 10"
        result = validate_read_only_cypher(q, max_rows=50)
        self.assertEqual(result, q)


class TestUnsafeCypherErrorSecrecy(unittest.TestCase):
    """The exception must NOT include the offending query text — callers must
    log a correlation id and reason only."""

    def test_error_message_does_not_include_query(self):
        try:
            validate_read_only_cypher("CREATE (n:Person) RETURN n")
        except UnsafeCypherError as e:
            self.assertNotIn("CREATE (n:Person)", str(e))
            self.assertNotIn("(n:Person)", str(e))
            self.assertTrue(e.correlation_id)
            self.assertGreater(len(e.correlation_id), 4)
        else:
            self.fail("UnsafeCypherError was not raised")


class TestSafeNeo4jGraphProxy(unittest.TestCase):
    def test_read_only_query_reaches_inner(self):
        inner = MagicMock()
        inner.query.return_value = [{"n": 1}]
        proxy = SafeNeo4jGraph(inner)
        rows = proxy.query("MATCH (n:Person) RETURN n LIMIT 5")
        self.assertEqual(rows, [{"n": 1}])
        inner.query.assert_called_once()
        called_with = inner.query.call_args[0][0]
        self.assertIn("RETURN n", called_with)

    def test_write_query_never_reaches_inner(self):
        inner = MagicMock()
        proxy = SafeNeo4jGraph(inner)
        with self.assertRaises(UnsafeCypherError):
            proxy.query("MATCH (p:Person) DELETE p")
        inner.query.assert_not_called()

    def test_multi_statement_never_reaches_inner(self):
        inner = MagicMock()
        proxy = SafeNeo4jGraph(inner)
        with self.assertRaises(UnsafeCypherError):
            proxy.query("MATCH (p) RETURN p; CREATE (n:Bad) RETURN n")
        inner.query.assert_not_called()

    def test_call_procedure_never_reaches_inner(self):
        inner = MagicMock()
        proxy = SafeNeo4jGraph(inner)
        with self.assertRaises(UnsafeCypherError):
            proxy.query(
                "CALL apoc.cypher.doIt('CREATE (n) RETURN n', {}) YIELD value "
                "RETURN value")
        inner.query.assert_not_called()

    def test_limit_augmentation_visible_to_inner(self):
        inner = MagicMock()
        inner.query.return_value = []
        proxy = SafeNeo4jGraph(inner, max_rows=25)
        proxy.query("MATCH (p:Person) RETURN p")
        called_with = inner.query.call_args[0][0]
        self.assertRegex(called_with, r"(?is)LIMIT\s+25\s*$")

    def test_proxy_delegates_other_attributes(self):
        inner = MagicMock()
        inner.some_other_attr = "hello"
        proxy = safe_graph(inner)
        self.assertEqual(proxy.some_other_attr, "hello")


class TestMalformedLabelPredicate(unittest.TestCase):
    """graph-ranking-reliability work order §5.1 — the exact observed failure
    shape and its two correct alternatives."""

    def test_observed_malformed_query_rejected(self):
        query = (
            "MATCH (text)\n"
            "WHERE text:`Entry OR text`:Poem OR text:Critique\n"
            "RETURN text.ID"
        )
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher(query)
        self.assertEqual(ctx.exception.reason_code, REASON_MALFORMED_LABEL_PREDICATE)
        self.assertTrue(ctx.exception.recoverable)
        self.assertIn(REASON_MALFORMED_LABEL_PREDICATE, RECOVERABLE_REASON_CODES)

    def test_separate_label_predicates_form_passes(self):
        query = ("MATCH (text) WHERE text:Entry OR text:Poem OR text:Critique "
                 "RETURN text.ID LIMIT 20")
        result = validate_read_only_cypher(query)
        self.assertIn("RETURN text.ID", result)

    def test_any_labels_form_passes(self):
        query = (
            "MATCH (text) WHERE any(label IN labels(text) "
            "WHERE label IN ['Entry', 'Poem', 'Critique']) "
            "RETURN text.ID LIMIT 20"
        )
        result = validate_read_only_cypher(query)
        self.assertIn("RETURN text.ID", result)

    def test_query_text_never_in_exception(self):
        query = "MATCH (text) WHERE text:`Entry OR text`:Poem RETURN text.ID"
        try:
            validate_read_only_cypher(query)
        except UnsafeCypherError as e:
            self.assertNotIn("Entry OR text", str(e))
            self.assertNotIn("text.ID", str(e))
        else:
            self.fail("UnsafeCypherError was not raised")


class TestReasonCodeClassification(unittest.TestCase):
    """graph-ranking-reliability work order §5.1 items 3-4: every rejection
    carries a mechanically-branchable reason_code, and only the query-SHAPE
    codes are recoverable — write/call/multi-statement stay blocked."""

    def test_missing_return_is_recoverable(self):
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher("MATCH (p:Person) WHERE p.ID = 'P001'")
        self.assertEqual(ctx.exception.reason_code, REASON_MISSING_RETURN)
        self.assertTrue(ctx.exception.recoverable)

    def test_forbidden_keyword_not_recoverable(self):
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher("MATCH (p:Person) DELETE p")
        self.assertEqual(ctx.exception.reason_code, REASON_FORBIDDEN_KEYWORD)
        self.assertFalse(ctx.exception.recoverable)

    def test_disallowed_call_not_recoverable(self):
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher("CALL db.labels() YIELD label RETURN label")
        self.assertEqual(ctx.exception.reason_code, REASON_DISALLOWED_CALL)
        self.assertFalse(ctx.exception.recoverable)

    def test_multi_statement_not_recoverable(self):
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher(
                "MATCH (p:Person) RETURN p LIMIT 5; MATCH (n) RETURN n")
        self.assertEqual(ctx.exception.reason_code, REASON_MULTI_STATEMENT)
        self.assertFalse(ctx.exception.recoverable)

    def test_empty_query_not_recoverable(self):
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher("   ")
        self.assertEqual(ctx.exception.reason_code, REASON_EMPTY_QUERY)
        self.assertFalse(ctx.exception.recoverable)

    def test_non_string_query_not_recoverable(self):
        with self.assertRaises(UnsafeCypherError) as ctx:
            validate_read_only_cypher(None)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.reason_code, REASON_NOT_A_STRING)
        self.assertFalse(ctx.exception.recoverable)

    def test_recoverable_set_is_exactly_shape_errors(self):
        self.assertEqual(
            RECOVERABLE_REASON_CODES,
            frozenset({REASON_MALFORMED_LABEL_PREDICATE, REASON_MISSING_RETURN}),
        )


class TestBoundedReadRetry(unittest.TestCase):
    """graph-ranking-reliability work order §3 P1 / §5.2 items 4-5: read-only
    graph queries retry a BOUNDED number of times on transport-transient
    failures, then give up; non-transient exceptions are never retried."""

    def test_retries_transient_then_succeeds(self):
        from neo4j.exceptions import ServiceUnavailable

        inner = MagicMock()
        inner.query.side_effect = [
            ServiceUnavailable("defunct connection"),
            ServiceUnavailable("defunct connection"),
            [{"n": 1}],
        ]
        proxy = SafeNeo4jGraph(inner)
        with unittest.mock.patch("tools.cypher_safety.time.sleep"):
            rows = proxy.query("MATCH (n:Person) RETURN n LIMIT 5")
        self.assertEqual(rows, [{"n": 1}])
        self.assertEqual(inner.query.call_count, 3)

    def test_gives_up_after_max_attempts(self):
        from neo4j.exceptions import ServiceUnavailable
        from tools.cypher_safety import READ_RETRY_MAX_ATTEMPTS

        inner = MagicMock()
        inner.query.side_effect = ServiceUnavailable("still down")
        proxy = SafeNeo4jGraph(inner)
        with unittest.mock.patch("tools.cypher_safety.time.sleep"):
            with self.assertRaises(ServiceUnavailable):
                proxy.query("MATCH (n:Person) RETURN n LIMIT 5")
        self.assertEqual(inner.query.call_count, READ_RETRY_MAX_ATTEMPTS)

    def test_non_transient_exception_never_retried(self):
        inner = MagicMock()
        inner.query.side_effect = RuntimeError("unrelated bug")
        proxy = SafeNeo4jGraph(inner)
        with self.assertRaises(RuntimeError):
            proxy.query("MATCH (n:Person) RETURN n LIMIT 5")
        inner.query.assert_called_once()

    def test_unsafe_query_never_reaches_retry_layer(self):
        # A write/shape rejection must fail on the FIRST validation pass —
        # never retried, never reaching `inner.query` at all.
        inner = MagicMock()
        proxy = SafeNeo4jGraph(inner)
        with self.assertRaises(UnsafeCypherError):
            proxy.query("MATCH (p:Person) DELETE p")
        inner.query.assert_not_called()


if __name__ == "__main__":
    unittest.main()

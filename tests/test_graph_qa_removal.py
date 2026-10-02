"""Graph QA removal contracts: direct rows, evidence parity, safety and legacy."""
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import _observability_fixtures as fx
from chatbot.observability import telemetry
from chatbot.observability.sinks import MemorySink
from chatbot.retrieval.graph_query import _extract_intermediate, retrieve_graph_evidence


class TestResultShapes(unittest.TestCase):
    def test_legacy_context_remains_supported(self):
        result = {"result": "unused prose", "intermediate_steps": [
            {"query": "q"}, {"context": fx.GRAPH_ROWS}]}
        self.assertEqual(_extract_intermediate(result), ("q", fx.GRAPH_ROWS))

    def test_direct_rows_are_read_from_result(self):
        result = {"result": fx.GRAPH_ROWS, "intermediate_steps": [{"query": "q"}]}
        self.assertEqual(_extract_intermediate(result), ("q", fx.GRAPH_ROWS))

    def test_empty_direct_rows_remain_empty(self):
        self.assertEqual(_extract_intermediate({"result": [], "intermediate_steps": [
            {"query": "q"}]}), ("q", []))

    def test_explicit_empty_context_is_not_replaced(self):
        self.assertEqual(_extract_intermediate({"result": fx.GRAPH_ROWS,
                                               "intermediate_steps": [{"context": []}]}),
                         (None, []))

    def test_prose_is_never_treated_as_rows(self):
        for value in ("prose", None, {}, 12):
            with self.subTest(value=value):
                self.assertEqual(_extract_intermediate({"result": value}), (None, []))


def retrieve(*, direct=True, rows=None, replies=None, source=None):
    source = source if source is not None else fx.RowSource(fx.GRAPH_ROWS if rows is None else rows)
    if replies is None:
        replies = [(fx.GRAPHRAG_CYPHER, fx.usage(120, 20))]
        if not direct:
            replies.append(("legacy prose", fx.usage(300, 40)))
    chain = fx.structured_graph_chain(fx.scripted_llm(*replies), source, direct=direct)
    sink = MemorySink()
    with telemetry.use_sink(sink), telemetry.request_scope(mode="graphrag"):
        evidence = retrieve_graph_evidence("허초희는 누구인가?", chain=chain)
    return evidence, sink, source


class TestStructuredDirectRetrieval(unittest.TestCase):
    def test_direct_evidence_matches_previous_chain(self):
        old, old_sink, old_source = retrieve(direct=False)
        new, new_sink, new_source = retrieve(direct=True)
        self.assertEqual(old.to_dict(), new.to_dict())
        self.assertEqual(dict(fx.purposes(old_sink)), {"cypher_generation": 1, "graph_qa": 1})
        self.assertEqual(dict(fx.purposes(new_sink)), {"cypher_generation": 1})
        self.assertEqual((old_source.calls, new_source.calls), (1, 1))

    def test_top_k_and_order_preserved(self):
        rows = [{"person_id": f"P{i:03d}", "person_name_kor": f"인물{i}"} for i in range(1, 15)]
        old, _, _ = retrieve(direct=False, rows=rows)
        new, _, _ = retrieve(rows=rows)
        self.assertEqual(old.to_dict(), new.to_dict())
        self.assertEqual(len(new.documents), 10)

    def test_empty_graph_skips_qa_and_preserves_evidence(self):
        old, _, _ = retrieve(direct=False, rows=[])
        new, sink, _ = retrieve(rows=[])
        self.assertEqual(old.to_dict(), new.to_dict())
        self.assertEqual(dict(fx.purposes(sink)), {"cypher_generation": 1})

    def test_write_query_remains_blocked_without_db_call(self):
        evidence, sink, source = retrieve(replies=[("MATCH (n) DELETE n", None)])
        self.assertEqual(source.calls, 0)
        self.assertIn({"type": "status", "outcome": "invalid_query"}, evidence.claims)
        self.assertEqual(dict(fx.purposes(sink)), {"cypher_generation": 1})

    def test_syntax_regeneration_still_runs_exactly_once(self):
        from neo4j.exceptions import CypherSyntaxError

        class FirstSyntaxError(fx.RowSource):
            def query(self, query, params=None):
                if self.calls == 0:
                    self.calls += 1
                    raise CypherSyntaxError("syntax fixture")
                return super().query(query, params)

        source = FirstSyntaxError(fx.GRAPH_ROWS)
        evidence, sink, _ = retrieve(source=source, replies=[
            (fx.GRAPHRAG_CYPHER, None), (fx.GRAPHRAG_CYPHER, None)])
        self.assertEqual(source.calls, 2)
        self.assertTrue(evidence.documents)
        self.assertEqual(dict(fx.purposes(sink)), {"cypher_generation": 2})

    def test_unrecoverable_provider_failure_is_still_status_evidence(self):
        source = fx.RowSource(error=RuntimeError("provider fixture"))
        evidence, sink, _ = retrieve(source=source)
        self.assertIn({"type": "status", "outcome": "temporarily_unavailable"}, evidence.claims)
        self.assertEqual(dict(fx.purposes(sink)), {"cypher_generation": 1})

    def test_legacy_prose_tool_keeps_its_qa_call(self):
        from chatbot.legacy.graph_qa import cypher_qa_safe
        source = fx.RowSource(fx.GRAPH_ROWS)
        chain = fx.structured_graph_chain(fx.scripted_llm(
            (fx.GRAPHRAG_CYPHER, None), ("legacy prose", None)), source, direct=False)
        sink = MemorySink()
        with telemetry.use_sink(sink), telemetry.request_scope(mode="graphrag"):
            answer = cypher_qa_safe("허초희?", chain=chain)
        self.assertEqual(answer, "legacy prose")
        self.assertEqual(dict(fx.purposes(sink)), {"cypher_generation": 1, "graph_qa": 1})


class TestFullPipelineParity(unittest.TestCase):
    def test_answer_citations_and_persistence_match_before(self):
        before, before_sink, before_parts = fx.run_graphrag(direct=False)
        after, after_sink, after_parts = fx.run_graphrag(direct=True)
        self.assertEqual(before, after)
        self.assertEqual(before_parts.history.messages, after_parts.history.messages)
        self.assertEqual(before_sink.of("request.completed")[0]["citation_count"],
                         after_sink.of("request.completed")[0]["citation_count"])
        self.assertEqual(before_sink.of("request.completed")[0]["llm_call_count"], 3)
        self.assertEqual(after_sink.of("request.completed")[0]["llm_call_count"], 2)

    def test_empty_graph_vector_answer_parity(self):
        before, _, _ = fx.run_graphrag(direct=False, graph_source=fx.RowSource([]))
        after, sink, _ = fx.run_graphrag(direct=True, graph_source=fx.RowSource([]))
        self.assertEqual(before, after)
        self.assertEqual(dict(fx.purposes(sink)), {"cypher_generation": 1, "final_synthesis": 1})


class TestBenchmarkStatistics(unittest.TestCase):
    def test_partial_usage_is_not_averaged_as_complete(self):
        from scripts.graph_qa_report import summarize
        row = {"outcome": "success", "llm_calls_by_purpose": {}, "llm_call_count": 2,
               "usage_complete": False, "input_tokens": 100, "output_tokens": None,
               "total_tokens": 100, "duration_ms": 1, "graph_duration_ms": 1}
        result = summarize([row])
        self.assertIsNone(result["mean_total_tokens"])
        self.assertEqual(result["complete_usage_requests"], 0)

    def test_nearest_rank_and_zero_denominator(self):
        from scripts.graph_qa_report import percentile, reduction
        self.assertEqual(percentile([1, 2, 3, 4, 5, 6], 0.95), 6)
        self.assertIsNone(reduction(0, 0))
        self.assertIsNone(reduction(None, 1))
        self.assertEqual(reduction(100, 70), 30)

    def test_changed_model_blocks_comparison(self):
        from scripts.graph_qa_report import compare
        with self.assertRaisesRegex(ValueError, "model"):
            compare({"model": "a"}, [], {"model": "b"}, [])


if __name__ == "__main__":
    unittest.main()

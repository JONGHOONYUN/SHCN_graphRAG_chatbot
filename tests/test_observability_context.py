"""Request context lifecycle (work order §7, §13.2).

  * every request gets a fresh request_id; nested scopes reuse the parent's
    (ownership-aware — no second start/complete, no early reset);
  * the context is reset after normal exit AND after an exception, and the
    original exception propagates unchanged;
  * `request.completed` is emitted exactly once per root request;
  * counters never leak between sequential requests or across threads.
"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot.observability import events as ev, telemetry  # noqa: E402
from chatbot.observability.sinks import MemorySink  # noqa: E402


def _completed(sink):
    return sink.of(ev.REQUEST_COMPLETED)


class TestRequestIds(unittest.TestCase):
    def test_each_request_gets_a_new_id_and_children_see_it(self):
        sink = MemorySink()
        seen = []
        with telemetry.use_sink(sink):
            for _ in range(3):
                with telemetry.request_scope(mode="graphRAG") as ctx:
                    seen.append(ctx.request_id)
                    # a callee reads the same context without any argument
                    self.assertIs(telemetry.current_request(), ctx)
        self.assertEqual(len(set(seen)), 3)
        for rid in seen:
            self.assertRegex(rid, r"^[0-9a-f]{32}$")
        self.assertEqual([e["request_id"] for e in _completed(sink)], seen)

    def test_mode_and_route_normalization(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="textRAG"):
                pass
            with telemetry.request_scope(mode="graphRAG", question_language="en",
                                         response_language="ko"):
                pass
        first, second = _completed(sink)
        self.assertEqual((first["mode"], first["route"]), ("vectorrag", "vectorrag"))
        self.assertEqual((second["mode"], second["route"]), ("graphrag", "graphrag"))
        self.assertEqual((second["question_language"], second["response_language"]),
                         ("en", "ko"))


class TestLifecycle(unittest.TestCase):
    def test_context_is_cleared_after_exit(self):
        with telemetry.use_sink(MemorySink()):
            with telemetry.request_scope(mode="graphRAG"):
                self.assertIsNotNone(telemetry.current_request())
        self.assertIsNone(telemetry.current_request())

    def test_exception_resets_context_and_emits_completed_once(self):
        sink = MemorySink()
        original = RuntimeError("boom")
        with telemetry.use_sink(sink):
            with self.assertRaises(RuntimeError) as caught:
                with telemetry.request_scope(mode="graphRAG"):
                    raise original
        self.assertIs(caught.exception, original)
        self.assertIsNone(telemetry.current_request())
        (done,) = _completed(sink)
        self.assertEqual(done["outcome"], "error")
        self.assertEqual(done["status"], "error")
        self.assertEqual(done["error_type"], "RuntimeError")

    def test_nested_scope_reuses_parent_and_does_not_complete_early(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG") as outer:
                with telemetry.request_scope(mode="textRAG") as inner:
                    self.assertIs(inner, outer)
                self.assertIs(telemetry.current_request(), outer)
                self.assertEqual(_completed(sink), [])
        self.assertEqual(len(sink.of(ev.REQUEST_STARTED)), 1)
        self.assertEqual(len(_completed(sink)), 1)
        self.assertEqual(_completed(sink)[0]["mode"], "graphrag")

    def test_nested_exception_handled_by_outer_is_not_an_error(self):
        """agent.py catches pipeline exceptions inside the root scope; only
        what escapes the ROOT scope counts as a request error."""
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG"):
                try:
                    with telemetry.request_scope(mode="graphRAG"):
                        raise ValueError("handled upstream")
                except ValueError:
                    pass
        self.assertEqual(_completed(sink)[0]["outcome"], "success")

    def test_outcome_mapping(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG"):
                telemetry.set_outcome("short_circuit")
            with telemetry.request_scope(mode="graphRAG"):
                telemetry.mark_fallback()
            with telemetry.request_scope(mode="graphRAG"):
                telemetry.mark_fallback()
                telemetry.set_outcome("error")
            with self.assertRaises(KeyError):
                with telemetry.request_scope(mode="graphRAG"):
                    telemetry.mark_fallback()
                    raise KeyError("x")
            with telemetry.request_scope(mode="graphRAG"):
                telemetry.set_outcome("not-an-outcome")      # ignored
        self.assertEqual([e["outcome"] for e in _completed(sink)],
                         ["short_circuit", "fallback_success", "fallback_error",
                          "fallback_error", "success"])
        self.assertEqual([e["fallback_used"] for e in _completed(sink)],
                         [False, True, True, True, False])

    def test_handled_backend_error_links_the_log_correlation_id(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG"):
                try:
                    raise ConnectionError("backend init failed")
                except ConnectionError as exc:           # handled like bot.py
                    telemetry.record_request_error(exc, correlation_id="c0ffee12")
        (done,) = _completed(sink)
        self.assertEqual((done["outcome"], done["error_type"], done["correlation_id"]),
                         ("error", "ConnectionError", "c0ffee12"))

    def test_route_update_keeps_request_id(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG") as ctx:
                telemetry.emit(ev.SYNTHESIS, status="error")
                telemetry.update_request(route="react_fallback")
                telemetry.emit(ev.FALLBACK, status="success")
        synth, fallback = sink.of(ev.SYNTHESIS)[0], sink.of(ev.FALLBACK)[0]
        self.assertEqual(synth["route"], "graphrag")
        self.assertEqual(fallback["route"], "react_fallback")
        self.assertEqual(synth["request_id"], fallback["request_id"], ctx.request_id)
        self.assertEqual(_completed(sink)[0]["route"], "react_fallback")

    def test_disabled_observability_still_runs_the_body(self):
        from chatbot.observability.sinks import NullSink

        with telemetry.use_sink(NullSink()):
            with telemetry.request_scope(mode="graphRAG") as ctx:
                value = "answer"
        self.assertEqual(value, "answer")
        self.assertIsNotNone(ctx)
        self.assertIsNone(telemetry.current_request())


class TestCounterIsolation(unittest.TestCase):
    def test_counters_do_not_leak_between_requests(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG") as ctx:
                ctx.record_llm_call("graph_qa", input_tokens=10, output_tokens=2,
                                    total_tokens=12, usage_available=True)
                telemetry.count_embedding_call()
                telemetry.set_answer_stats(answer_chars=100, citation_count=3)
            with telemetry.request_scope(mode="graphRAG"):
                pass
        first, second = _completed(sink)
        self.assertEqual(first["llm_call_count"], 1)
        self.assertEqual(first["llm_calls_by_purpose"]["graph_qa"], 1)
        self.assertEqual(first["total_tokens"], 12)
        self.assertEqual(first["embedding_call_count"], 1)
        self.assertEqual((first["answer_chars"], first["citation_count"]), (100, 3))
        self.assertEqual(second["llm_call_count"], 0)
        self.assertIsNone(second["total_tokens"])        # never reported ≠ 0
        self.assertIsNone(second["citation_count"])
        self.assertEqual(second["embedding_call_count"], 0)

    def test_token_sum_only_adds_reported_values(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.request_scope(mode="graphRAG") as ctx:
                ctx.record_llm_call("cypher_generation", input_tokens=5,
                                    usage_available=True)
                ctx.record_llm_call("graph_qa")                 # no usage
        (done,) = _completed(sink)
        self.assertEqual(done["llm_call_count"], 2)
        self.assertEqual(done["usage_available_call_count"], 1)
        self.assertEqual(done["input_tokens"], 5)
        self.assertIsNone(done["output_tokens"])

    def test_threads_have_independent_contexts(self):
        sink = MemorySink()
        results = {}
        barrier = threading.Barrier(2)

        def worker(name, calls):
            with telemetry.use_sink(sink):
                with telemetry.request_scope(mode="graphRAG") as ctx:
                    barrier.wait()
                    for _ in range(calls):
                        ctx.record_llm_call("final_synthesis")
                    barrier.wait()
                    results[name] = (ctx.request_id,
                                     telemetry.current_request().request_id)

        threads = [threading.Thread(target=worker, args=("a", 1)),
                   threading.Thread(target=worker, args=("b", 3))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["a"][0], results["a"][1])
        self.assertEqual(results["b"][0], results["b"][1])
        self.assertNotEqual(results["a"][0], results["b"][0])
        by_id = {e["request_id"]: e["llm_call_count"] for e in _completed(sink)}
        self.assertEqual(by_id[results["a"][0]], 1)
        self.assertEqual(by_id[results["b"][0]], 3)
        self.assertIsNone(telemetry.current_request())


if __name__ == "__main__":
    unittest.main()

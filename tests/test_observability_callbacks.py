"""LangChain callback instrumentation and token-usage normalization (§10, §13.3).

  * one real model lifecycle → exactly one `llm.completed`; chain/parser
    callbacks never count as model calls;
  * purpose labels come from leaf-LLM metadata; unknown → `other`;
  * the two LLMs INSIDE GraphCypherQAChain are separated
    (cypher_generation / graph_qa) via the production builder;
  * usage metadata in several provider shapes normalizes to the standard
    fields; absent usage stays null with usage_available=false; nothing is
    estimated from characters or summed twice;
  * model errors are recorded with their class and still propagate; callback
    failures never touch the model result; prompt/output text never appears.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult  # noqa: E402
from langchain_core.output_parsers import StrOutputParser  # noqa: E402
from langchain_core.prompts import ChatPromptTemplate  # noqa: E402

import _observability_fixtures as fx  # noqa: E402
from chatbot.observability import events as ev, telemetry  # noqa: E402
from chatbot.observability.callbacks import (  # noqa: E402
    LLMTelemetryHandler,
    PURPOSE_METADATA_KEY,
    with_llm_purpose,
)
from chatbot.observability.context import RequestContext  # noqa: E402
from chatbot.observability.sinks import MemorySink  # noqa: E402
from chatbot.observability.usage import extract_usage  # noqa: E402


class _FailingChat(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "failing"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("provider down MARKER_ERROR_TEXT")


def _chain(llm):
    return (ChatPromptTemplate.from_messages([("human", "{q}")]) | llm
            | StrOutputParser())


def _run(fn, mode="graphRAG"):
    sink = MemorySink()
    with telemetry.use_sink(sink):
        with telemetry.request_scope(mode=mode):
            result = fn()
    return result, sink


def _result(message, *, llm_output=None, generation_info=None):
    return LLMResult(generations=[[ChatGeneration(
        message=message, generation_info=generation_info)]], llm_output=llm_output)


class TestModelLifecycle(unittest.TestCase):
    def test_one_model_call_is_one_event_and_chain_runs_do_not_count(self):
        llm = fx.scripted_llm(("hello", fx.usage(3, 2)))
        out, sink = _run(lambda: _chain(with_llm_purpose(llm, "final_synthesis"))
                         .invoke({"q": "hi"}))
        self.assertEqual(out, "hello")
        (event,) = sink.of(ev.LLM)
        self.assertEqual(event["purpose"], "final_synthesis")
        self.assertEqual(event["status"], "success")
        self.assertIsNone(event["attempt_count"])       # provider retries unobservable
        self.assertEqual(sink.of(ev.REQUEST_COMPLETED)[0]["llm_call_count"], 1)

    def test_purpose_normalization(self):
        llm = fx.scripted_llm(("a", None), ("b", None))
        _, sink = _run(lambda: (
            _chain(with_llm_purpose(llm, "no_such_purpose")).invoke({"q": "x"}),
            _chain(llm).invoke({"q": "y"})))            # untagged
        self.assertEqual([e["purpose"] for e in sink.of(ev.LLM)], ["other", "other"])

    def test_with_llm_purpose_wraps_the_same_model(self):
        llm = fx.scripted_llm(("x", None))
        bound = with_llm_purpose(llm, "graph_qa")
        self.assertIs(bound.bound, llm)
        self.assertEqual(bound.config["metadata"][PURPOSE_METADATA_KEY], "graph_qa")
        self.assertEqual(bound.kwargs, {})             # no model kwargs changed

    def test_graph_cypher_chain_separates_its_two_llms(self):
        source = fx.RowSource(fx.GRAPH_ROWS)
        llm = fx.scripted_llm((fx.GRAPHRAG_CYPHER, fx.usage(10, 5)),
                              ("qa prose", fx.usage(20, 4)))
        chain = fx.structured_graph_chain(llm, source, direct=False)
        result, sink = _run(lambda: chain.invoke({"query": "누가?"}))
        self.assertEqual(result["intermediate_steps"][1]["context"], fx.GRAPH_ROWS)
        self.assertEqual(result["result"], "qa prose")   # Graph QA still generated
        self.assertEqual([e["purpose"] for e in sink.of(ev.LLM)],
                         ["cypher_generation", "graph_qa"])
        counts = sink.of(ev.REQUEST_COMPLETED)[0]["llm_calls_by_purpose"]
        self.assertEqual((counts["cypher_generation"], counts["graph_qa"]), (1, 1))
        self.assertEqual(source.calls, 1)              # query not re-executed

    def test_production_builder_binds_distinct_purposes(self):
        chain = fx.structured_graph_chain(fx.scripted_llm(), fx.RowSource())
        gen_llm = chain.cypher_generation_chain.steps[1]
        qa_llm = chain.qa_chain.steps[1]
        self.assertEqual(gen_llm.config["metadata"][PURPOSE_METADATA_KEY],
                         "cypher_generation")
        self.assertEqual(qa_llm.config["metadata"][PURPOSE_METADATA_KEY], "graph_qa")
        self.assertTrue(chain.return_intermediate_steps)
        self.assertTrue(chain.return_direct)           # structured retrieval skips QA

    def test_model_error_recorded_and_propagated(self):
        chain = _chain(with_llm_purpose(_FailingChat(), "general_chat"))
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with self.assertRaises(RuntimeError):
                with telemetry.request_scope(mode="graphRAG"):
                    chain.invoke({"q": "x"})
        (event,) = sink.of(ev.LLM)
        self.assertEqual((event["status"], event["error_type"]), ("error", "RuntimeError"))
        self.assertIsNone(event["input_tokens"])
        self.assertFalse(event["usage_available"])
        self.assertNotIn("MARKER_ERROR_TEXT", json.dumps(sink.events))
        self.assertEqual(sink.of(ev.REQUEST_COMPLETED)[0]["llm_call_count"], 1)

    def test_streamed_call_is_one_event_and_usage_is_not_summed_twice(self):
        llm = with_llm_purpose(fx.scripted_llm(("stream me please", fx.usage(8, 3))),
                               "text_rag_answer")
        chunks, sink = _run(lambda: list(_chain(llm).stream({"q": "x"})))
        self.assertEqual("".join(chunks), "stream me please")
        (event,) = sink.of(ev.LLM)
        self.assertEqual((event["input_tokens"], event["output_tokens"],
                          event["total_tokens"]), (8, 3, 11))
        self.assertEqual(sink.of(ev.REQUEST_COMPLETED)[0]["total_tokens"], 11)

    def test_no_prompt_or_output_text_in_events(self):
        llm = fx.scripted_llm(("OUTPUT_MARKER_7781", fx.usage(1, 1)))
        _, sink = _run(lambda: _chain(with_llm_purpose(llm, "final_synthesis"))
                       .invoke({"q": "PROMPT_MARKER_5512"}))
        blob = json.dumps(sink.events, ensure_ascii=False)
        self.assertNotIn("PROMPT_MARKER_5512", blob)
        self.assertNotIn("OUTPUT_MARKER_7781", blob)
        event = sink.of(ev.LLM)[0]
        self.assertEqual(event["input_chars"], len("PROMPT_MARKER_5512"))
        self.assertEqual(event["output_chars"], len("OUTPUT_MARKER_7781"))

    def test_no_callbacks_outside_a_request(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            _chain(fx.scripted_llm(("x", None))).invoke({"q": "y"})
        self.assertEqual(sink.of(ev.LLM), [])


class TestHandlerRobustness(unittest.TestCase):
    def _handler(self):
        return LLMTelemetryHandler(RequestContext.new(mode="graphRAG"))

    def test_duplicate_end_is_counted_once(self):
        handler = self._handler()
        sink = MemorySink()
        run_id = uuid4()
        with telemetry.use_sink(sink):
            handler.on_chat_model_start({}, [[]], run_id=run_id,
                                        metadata={PURPOSE_METADATA_KEY: "graph_qa"})
            response = _result(AIMessage(content="x"))
            handler.on_llm_end(response, run_id=run_id)
            handler.on_llm_end(response, run_id=run_id)
            handler.on_llm_end(response, run_id=uuid4())      # end without start
        self.assertEqual(len(sink.of(ev.LLM)), 1)
        self.assertEqual(handler._ctx.llm_call_count, 1)

    def test_provider_and_model_come_from_langchain_metadata(self):
        handler = self._handler()
        sink = MemorySink()
        run_id = uuid4()
        with telemetry.use_sink(sink):
            handler.on_chat_model_start(
                {}, [[]], run_id=run_id,
                metadata={"ls_provider": "google_genai",
                          "ls_model_name": "gemini-2.5-flash",
                          PURPOSE_METADATA_KEY: "cypher_generation"})
            handler.on_llm_end(_result(AIMessage(content="x")), run_id=run_id)
        event = sink.of(ev.LLM)[0]
        self.assertEqual((event["provider"], event["model"]),
                         ("google_genai", "gemini-2.5-flash"))

    def test_callback_failure_never_changes_the_model_result(self):
        from chatbot.observability import emitter

        llm = fx.scripted_llm(("still fine", None))
        emitter._internal_counts.clear()   # warning budget is per process
        with patch.object(RequestContext, "record_llm_call",
                          side_effect=RuntimeError("counter broke")), \
                self.assertLogs("chatbot.observability.internal", level="WARNING"):
            out, sink = _run(lambda: _chain(with_llm_purpose(llm, "graph_qa"))
                             .invoke({"q": "x"}))
        self.assertEqual(out, "still fine")
        self.assertEqual(sink.of(ev.LLM), [])          # the failed record is dropped
        self.assertEqual(len(sink.of(ev.REQUEST_COMPLETED)), 1)


class TestUsageNormalization(unittest.TestCase):
    def test_langchain_standard_usage_metadata(self):
        u = extract_usage(_result(AIMessage(content="x", usage_metadata={
            "input_tokens": 11, "output_tokens": 7, "total_tokens": 18})))
        self.assertEqual((u.input_tokens, u.output_tokens, u.total_tokens), (11, 7, 18))
        self.assertTrue(u.available)

    def test_google_raw_response_metadata(self):
        u = extract_usage(_result(AIMessage(content="x", response_metadata={
            "usage_metadata": {"prompt_token_count": 30,
                               "candidates_token_count": 9,
                               "total_token_count": 39}})))
        self.assertEqual((u.input_tokens, u.output_tokens, u.total_tokens), (30, 9, 39))

    def test_openai_style_llm_output(self):
        u = extract_usage(_result(AIMessage(content="x"), llm_output={
            "token_usage": {"prompt_tokens": 5, "completion_tokens": 3,
                            "total_tokens": 8}}))
        self.assertEqual((u.input_tokens, u.output_tokens, u.total_tokens), (5, 3, 8))

    def test_generation_info_container(self):
        u = extract_usage(_result(AIMessage(content="x"), generation_info={
            "usage_metadata": {"input_tokens": 2, "output_tokens": 1,
                               "total_tokens": 3}}))
        self.assertEqual(u.total_tokens, 3)

    def test_partial_usage_is_not_completed_by_arithmetic(self):
        u = extract_usage(_result(AIMessage(content="x"), llm_output={
            "usage": {"prompt_tokens": 4}}))
        self.assertEqual((u.input_tokens, u.output_tokens, u.total_tokens), (4, None, None))
        self.assertTrue(u.available)

    def test_absent_usage_is_null_not_zero_nor_estimated(self):
        u = extract_usage(_result(AIMessage(content="a long answer " * 50)))
        self.assertEqual((u.input_tokens, u.output_tokens, u.total_tokens),
                         (None, None, None))
        self.assertFalse(u.available)

    def test_first_source_wins_so_usage_is_never_summed_twice(self):
        u = extract_usage(_result(
            AIMessage(content="x", usage_metadata={"input_tokens": 10,
                                                   "output_tokens": 1,
                                                   "total_tokens": 11}),
            llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 1,
                                        "total_tokens": 11}}))
        self.assertEqual(u.total_tokens, 11)

    def test_malformed_values_ignored(self):
        u = extract_usage(_result(AIMessage(content="x"), llm_output={
            "token_usage": {"prompt_tokens": "12", "completion_tokens": True,
                            "total_tokens": -5}}))
        self.assertFalse(u.available)
        self.assertFalse(extract_usage(object()).available)

    def test_usage_absent_end_to_end(self):
        llm = fx.scripted_llm(("no usage here", None))
        _, sink = _run(lambda: _chain(with_llm_purpose(llm, "graph_qa")).invoke({"q": "x"}))
        event = sink.of(ev.LLM)[0]
        self.assertFalse(event["usage_available"])
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertIn(key, event)
            self.assertIsNone(event[key])
        done = sink.of(ev.REQUEST_COMPLETED)[0]
        self.assertEqual(done["usage_available_call_count"], 0)
        self.assertIsNone(done["total_tokens"])


if __name__ == "__main__":
    unittest.main()

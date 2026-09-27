"""Observability core — event schema, spans, sinks, failure isolation.

Covers work order §13.1: JSON-serializable events with the common envelope,
non-negative durations, span success/error events with the ORIGINAL exception
re-raised, sink failures never reaching the application, NullSink being inert,
MemorySink preserving order — plus the allowlist-only schema contract.
"""

import json
import logging
import math
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot.observability import emitter, events as ev, spans, telemetry  # noqa: E402
from chatbot.observability.sinks import (  # noqa: E402
    EVENTS_LOGGER_NAME,
    LoggingSink,
    MemorySink,
    NullSink,
)


class _ExplodingSink:
    def __init__(self):
        self.calls = 0

    def emit(self, event):
        self.calls += 1
        raise RuntimeError("sink is down")


class TestEventSchema(unittest.TestCase):
    def test_common_envelope_and_json(self):
        event = ev.build_event(ev.LLM, {"purpose": "graph_qa", "status": "success",
                                        "duration_ms": 12.3456})
        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(event["event_name"], "llm.completed")
        self.assertRegex(event["timestamp"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(event["duration_ms"], 12.346)
        # one-line, deterministic, never repr()-based
        line = ev.serialize(event)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line), event)

    def test_unknown_event_name_is_rejected(self):
        self.assertIsNone(ev.build_event("totally.made.up", {}))

    def test_unknown_fields_and_objects_are_dropped_not_stringified(self):
        class Secret:
            def __repr__(self):
                return "SECRET_REPR"

        event = ev.build_event(ev.RETRIEVAL_GRAPH, {
            "retriever": "graph", "status": "success",
            "not_in_schema": "x", "result_count": Secret(),
            "prompt": "raw prompt", "cypher": "MATCH (n) RETURN n"})
        self.assertNotIn("not_in_schema", event)
        self.assertNotIn("result_count", event)
        self.assertNotIn("prompt", event)
        self.assertNotIn("cypher", event)
        self.assertNotIn("SECRET_REPR", ev.serialize(event))

    def test_schema_never_declares_raw_content_fields(self):
        self.assertFalse(ev.ALLOWED_FIELDS & ev.FORBIDDEN_FIELD_NAMES)
        for name in ("question", "prompt", "messages", "answer", "cypher",
                     "query_text", "evidence_text", "response_body",
                     "api_key", "password", "embedding_vector"):
            self.assertIn(name, ev.FORBIDDEN_FIELD_NAMES)
            self.assertNotIn(name, ev.ALLOWED_FIELDS)

    def test_bounded_enums_normalize(self):
        e = ev.build_event(ev.LLM, {"purpose": "brand_new_purpose",
                                    "mode": "graphRAG", "route": "somewhere"})
        self.assertEqual(e["purpose"], "other")
        self.assertEqual(e["mode"], "unknown")        # raw value never passes
        self.assertEqual(e["route"], "unknown")
        emb = ev.build_event(ev.EMBEDDING, {"purpose": "graph_qa"})
        self.assertEqual(emb["purpose"], "other")      # embedding enum is separate

    def test_counts_distinguish_unknown_from_zero(self):
        e = ev.build_event(ev.NEO4J_QUERY, {"row_count": None, "attempt_count": 0,
                                            "input_tokens": -1, "output_chars": True})
        self.assertIsNone(e["row_count"])             # unknown stays null
        self.assertEqual(e["attempt_count"], 0)       # known zero stays zero
        self.assertNotIn("input_tokens", e)           # invalid dropped
        self.assertNotIn("output_chars", e)           # bool is not a count

    def test_invalid_durations_dropped(self):
        for bad in (-1, math.nan, math.inf, "12", True):
            self.assertNotIn("duration_ms",
                             ev.build_event(ev.SYNTHESIS, {"duration_ms": bad}))

    def test_request_id_cannot_be_overridden_by_fields(self):
        common = {"request_id": "a" * 32, "mode": "graphrag", "route": "graphrag"}
        e = ev.build_event(ev.SYNTHESIS, {"request_id": "b" * 32}, common)
        self.assertEqual(e["request_id"], "a" * 32)

    def test_purpose_counts_only_known_keys(self):
        e = ev.build_event(ev.REQUEST_COMPLETED, {"llm_calls_by_purpose": {
            "graph_qa": 2, "evil_key": 5, "final_synthesis": -3}})
        counts = e["llm_calls_by_purpose"]
        self.assertEqual(set(counts), set(ev.LLM_PURPOSES))
        self.assertEqual(counts["graph_qa"], 2)
        self.assertEqual(counts["final_synthesis"], 0)

    def test_error_type_is_a_class_name_only(self):
        e = ev.build_event(ev.LLM, {"error_type": "ValueError: token=abc"})
        self.assertNotIn("error_type", e)
        e = ev.build_event(ev.LLM, {"error_type": "TransientProviderError"})
        self.assertEqual(e["error_type"], "TransientProviderError")


class TestSpans(unittest.TestCase):
    def test_success_span(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.span(ev.EVIDENCE_FORMAT) as sp:
                sp.set(evidence_chars=10)
        (event,) = sink.of(ev.EVIDENCE_FORMAT)
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["evidence_chars"], 10)
        self.assertGreaterEqual(event["duration_ms"], 0)

    def test_error_span_reraises_original_exception(self):
        sink = MemorySink()
        original = KeyError("missing")
        with telemetry.use_sink(sink):
            with self.assertRaises(KeyError) as caught:
                with telemetry.span(ev.SYNTHESIS):
                    raise original
        self.assertIs(caught.exception, original)
        (event,) = sink.of(ev.SYNTHESIS)
        self.assertEqual(event["status"], "error")
        self.assertEqual(event["error_type"], "KeyError")

    def test_explicit_status_survives_exception(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with self.assertRaises(ValueError):
                with telemetry.span(ev.NEO4J_QUERY) as sp:
                    sp.set(status="skipped")
                    raise ValueError("x")
        self.assertEqual(sink.of(ev.NEO4J_QUERY)[0]["status"], "skipped")

    def test_attempt_counting_and_unknown(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.span(ev.RETRIEVAL_GRAPH, count_attempts=True):
                telemetry.note_attempt()
                telemetry.note_attempt()
            with telemetry.span(ev.RETRIEVAL_VECTOR, count_attempts=True):
                pass
        self.assertEqual(sink.of(ev.RETRIEVAL_GRAPH)[0]["attempt_count"], 2)
        self.assertIsNone(sink.of(ev.RETRIEVAL_VECTOR)[0]["attempt_count"])

    def test_note_attempt_targets_innermost_counting_span(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.span(ev.RETRIEVAL_GRAPH, count_attempts=True):
                telemetry.note_attempt()
                with telemetry.span(ev.NEO4J_QUERY, count_attempts=True):
                    telemetry.note_attempt()
                    telemetry.note_attempt()
        self.assertEqual(sink.of(ev.NEO4J_QUERY)[0]["attempt_count"], 2)
        self.assertEqual(sink.of(ev.RETRIEVAL_GRAPH)[0]["attempt_count"], 1)

    def test_annotate_targets_innermost_span_and_is_noop_outside(self):
        sink = MemorySink()
        telemetry.annotate(error_type="Nope")     # no span: harmless
        with telemetry.use_sink(sink):
            with telemetry.span(ev.RETRIEVAL_VECTOR):
                telemetry.annotate(error_type="TimeoutError")
        self.assertEqual(sink.of(ev.RETRIEVAL_VECTOR)[0]["error_type"], "TimeoutError")

    def test_manual_span_finishes_once(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            sp = telemetry.start_span(ev.EVIDENCE_GATHER)
            sp.finish(evidence_graph_count=1)
            sp.finish(evidence_graph_count=2)
        events = sink.of(ev.EVIDENCE_GATHER)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["evidence_graph_count"], 1)

    def test_neo4j_operation_label_is_scoped(self):
        self.assertEqual(telemetry.current_neo4j_operation(), ("other", "unknown"))
        with telemetry.neo4j_operation("vector_query", "framework"):
            self.assertEqual(telemetry.current_neo4j_operation(),
                             ("vector_query", "framework"))
        self.assertEqual(telemetry.current_neo4j_operation(), ("other", "unknown"))


class TestSinks(unittest.TestCase):
    def test_memory_sink_preserves_order(self):
        sink = MemorySink()
        with telemetry.use_sink(sink):
            for name in (ev.HISTORY_READ, ev.EVIDENCE_GATHER, ev.SYNTHESIS):
                telemetry.emit(name, status="success")
        self.assertEqual(sink.names(),
                         [ev.HISTORY_READ, ev.EVIDENCE_GATHER, ev.SYNTHESIS])

    def test_null_sink_is_inert_and_spans_become_noop(self):
        with telemetry.use_sink(NullSink()):
            self.assertFalse(telemetry.is_enabled())
            self.assertIs(telemetry.span(ev.SYNTHESIS), spans.NOOP_SPAN)
            telemetry.emit(ev.SYNTHESIS, status="success")    # no error

    def test_sink_failure_never_reaches_the_caller(self):
        sink = _ExplodingSink()
        emitter._internal_counts.clear()   # warning budget is per process
        before = sum(emitter.internal_error_counts().values())
        with telemetry.use_sink(sink):
            with self.assertLogs("chatbot.observability.internal", level="WARNING") as logs:
                with telemetry.span(ev.SYNTHESIS):
                    value = 42
                telemetry.emit(ev.HISTORY_READ, status="success")
        self.assertEqual(value, 42)
        self.assertGreaterEqual(sink.calls, 2)
        self.assertGreater(sum(emitter.internal_error_counts().values()), before)
        # the internal report is itself a schema-valid observability.error line
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual(record["event_name"], "observability.error")
        self.assertEqual(record["component"], "sink")
        self.assertEqual(record["error_type"], "RuntimeError")

    def test_logging_sink_emits_one_json_line_without_touching_root(self):
        root_handlers = list(logging.getLogger().handlers)
        with self.assertLogs(EVENTS_LOGGER_NAME, level="INFO") as logs:
            with telemetry.use_sink(LoggingSink()):
                telemetry.emit(ev.SYNTHESIS, status="success", duration_ms=1.0)
        (line,) = logs.output
        payload = json.loads(line.split(":", 2)[2])
        self.assertEqual(payload["event_name"], "synthesis.completed")
        self.assertEqual(list(logging.getLogger().handlers), root_handlers)

    def test_logging_sink_skips_work_when_level_disabled(self):
        sink = LoggingSink(logger_name="chatbot.observability.test_silent",
                           level=logging.DEBUG)
        with patch("chatbot.observability.sinks.serialize") as ser:
            sink.emit({"event_name": "x"})
        ser.assert_not_called()

    def test_env_configuration(self):
        saved = emitter._state.sink
        try:
            with patch.dict(os.environ, {"CHATBOT_OBSERVABILITY": "off"}):
                telemetry.configure(from_env=True)
                self.assertFalse(telemetry.is_enabled())
            with patch.dict(os.environ, {"CHATBOT_OBSERVABILITY": "log",
                                         "CHATBOT_OBSERVABILITY_LOG_LEVEL": "DEBUG"}):
                telemetry.configure(from_env=True)
                sink = telemetry.active_sink()
                self.assertIsInstance(sink, LoggingSink)
                self.assertEqual(sink.level, logging.DEBUG)
        finally:
            emitter._state.sink = saved


class TestCoreImportPurity(unittest.TestCase):
    """The observability core must import with no Streamlit, LangChain, Neo4j,
    requests or provider SDK — only `callbacks.py` may touch langchain_core."""

    def test_core_modules_import_nothing_heavy(self):
        import subprocess

        repo = Path(__file__).resolve().parent.parent
        code = (
            "import sys\n"
            "import chatbot.observability.events, chatbot.observability.sinks\n"
            "import chatbot.observability.context, chatbot.observability.emitter\n"
            "import chatbot.observability.spans, chatbot.observability.telemetry\n"
            "import chatbot.observability.usage\n"
            "heavy = ('streamlit','langchain','langchain_core','langchain_neo4j',"
            "'langchain_google_genai','neo4j','requests')\n"
            "print(sorted({m.split('.')[0] for m in sys.modules} & set(heavy)))\n")
        env = dict(os.environ, PYTHONPATH=str(repo), PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(repo), env=env,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")

    def test_no_streamlit_or_sdk_import_in_the_package_source(self):
        import ast

        pkg = Path(__file__).resolve().parent.parent / "chatbot" / "observability"
        for path in pkg.glob("*.py"):
            roots = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    roots |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots.add(node.module.split(".")[0])
            for banned in ("streamlit", "neo4j", "requests", "langchain_neo4j",
                           "langchain_google_genai", "opentelemetry", "prometheus_client",
                           "sentry_sdk", "datadog", "boto3"):
                self.assertNotIn(banned, roots, f"{path.name} imports {banned}")
            if path.name != "callbacks.py":
                self.assertNotIn("langchain_core", roots, path.name)


if __name__ == "__main__":
    unittest.main()

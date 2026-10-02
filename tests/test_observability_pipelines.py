"""Pipeline-level observability — the work-order baseline scenarios (§13.4–§14).

Every scenario runs the REAL production code (GraphCypherQAChain from the
production builder, the read-only Cypher safety wrapper, the evidence
orchestrator, Neo4jVector's similarity path, the GoogleEmbeddings retry client,
both pipelines, agent.py's fallback policy) with only the outermost I/O
replaced by deterministic doubles. No real API, internet, or Neo4j.

Baseline scenarios (§14):
    normal GraphRAG                   cypher_generation 1 · graph_qa 0 · final_synthesis 1
    graph empty + vector success      statuses separated, synthesis still runs
    all retrieval failed              short_circuit, final_synthesis 0
    normal VectorRAG                  embedding · vector query · text_rag_answer separated
    transient error → ReAct success   same request_id, fallback_success
    provider usage absent             tokens null, usage unavailable
    authority cache hit               HTTP 0, cache_hit true
    observability sink failure        user-visible result unchanged
"""

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(REPO))

import _observability_fixtures as fx  # noqa: E402
from chatbot.observability import emitter, events as ev, telemetry  # noqa: E402
from chatbot.observability.sinks import MemorySink, NullSink  # noqa: E402


class _ExplodingSink:
    def emit(self, event):
        raise RuntimeError("sink down")


def _one(sink, name, **match):
    found = sink.where(name, **match)
    assert len(found) == 1, (name, match, [e.get("event_name") for e in sink.events])
    return found[0]


# ── GraphRAG ─────────────────────────────────────────────────────────────────
class TestGraphRagBaseline(unittest.TestCase):
    def setUp(self):
        self.output, self.sink, self.parts = fx.run_graphrag()
        self.done = _one(self.sink, ev.REQUEST_COMPLETED)

    def test_two_llm_purposes_and_absent_graph_qa(self):
        self.assertEqual(dict(fx.purposes(self.sink)),
                         {"cypher_generation": 1, "final_synthesis": 1})
        by_purpose = self.done["llm_calls_by_purpose"]
        self.assertEqual((by_purpose["cypher_generation"], by_purpose["graph_qa"],
                          by_purpose["final_synthesis"]), (1, 0, 1))
        self.assertEqual(self.done["llm_call_count"], len(self.sink.of(ev.LLM)))

    def test_request_token_totals_are_the_sum_of_reported_usage(self):
        llm_events = self.sink.of(ev.LLM)
        self.assertEqual(self.done["input_tokens"],
                         sum(e["input_tokens"] for e in llm_events))
        self.assertEqual(self.done["total_tokens"], 1120)
        self.assertEqual(self.done["usage_available_call_count"], 2)

    def test_pipeline_and_retrieval_events(self):
        pipeline = _one(self.sink, ev.PIPELINE_GRAPHRAG)
        self.assertEqual((pipeline["status"], pipeline["outcome"]), ("success", "success"))
        self.assertTrue(pipeline["synthesis_executed"])
        self.assertFalse(pipeline["short_circuit"])
        graph = _one(self.sink, ev.RETRIEVAL_GRAPH)
        self.assertEqual((graph["retriever"], graph["status"], graph["result_count"],
                          graph["attempt_count"]), ("graph", "success", 2, 1))
        vector = _one(self.sink, ev.RETRIEVAL_VECTOR)
        self.assertEqual((vector["status"], vector["result_count"], vector["attempt_count"]),
                         ("success", 2, 1))
        authority = _one(self.sink, ev.RETRIEVAL_AUTHORITY)
        self.assertEqual(authority["status"], "skipped")   # no biographical cue

    def test_retrieval_order_is_graph_vector_authority(self):
        names = [n for n in self.sink.names() if n.startswith("retrieval.")]
        self.assertEqual(names, [ev.RETRIEVAL_GRAPH, ev.RETRIEVAL_VECTOR,
                                 ev.RETRIEVAL_AUTHORITY])

    def test_neo4j_generated_query_and_vector_query_are_distinct(self):
        generated = _one(self.sink, ev.NEO4J_QUERY, operation="generated_graph_query")
        self.assertEqual((generated["query_origin"], generated["safety_result"],
                          generated["row_count"], generated["attempt_count"]),
                         ("generated", "passed", 2, 1))
        vector = _one(self.sink, ev.NEO4J_QUERY, operation="vector_query")
        self.assertEqual(vector["row_count"], 2)
        self.assertIsNone(vector["attempt_count"])       # driver retry unobservable
        # measurement never re-executes a query
        self.assertEqual(self.parts.graph_source.calls, 1)
        self.assertEqual(self.parts.store.db_calls, 1)

    def test_embedding_is_separate_from_the_vector_query(self):
        emb = _one(self.sink, ev.EMBEDDING)
        self.assertEqual((emb["purpose"], emb["input_count"], emb["dimension"],
                          emb["attempt_count"]), ("vector_query", 1, 3, 1))
        self.assertEqual(self.done["embedding_call_count"], 1)

    def test_pipeline_stage_spans(self):
        for name in (ev.HISTORY_READ, ev.EVIDENCE_GATHER, ev.EVIDENCE_FORMAT,
                     ev.SYNTHESIS, ev.CITATIONS_BUILD, ev.ANSWER_RENDER,
                     ev.HISTORY_WRITE):
            event = _one(self.sink, name)
            self.assertEqual(event["status"], "success", name)
            self.assertGreaterEqual(event["duration_ms"], 0, name)
        gather = _one(self.sink, ev.EVIDENCE_GATHER)
        self.assertEqual((gather["evidence_graph_count"], gather["evidence_vector_count"],
                          gather["evidence_external_count"]), (2, 2, 0))
        self.assertGreater(_one(self.sink, ev.EVIDENCE_FORMAT)["evidence_chars"], 0)
        self.assertEqual(len(self.sink.where(ev.NEO4J_QUERY, operation="history_read")), 1)
        self.assertEqual(len(self.sink.where(ev.NEO4J_QUERY, operation="history_write")), 1)

    def test_answer_and_citation_stats_match_the_returned_answer(self):
        self.assertEqual(self.done["answer_chars"], len(self.output))
        citations = self.output.split("## 출처", 1)[1].strip().splitlines()
        self.assertEqual(self.done["citation_count"], len(citations))
        self.assertEqual(_one(self.sink, ev.CITATIONS_BUILD)["citation_count"],
                         len(citations))

    def test_history_written_exactly_once(self):
        self.assertEqual(len(self.parts.history.messages), 2)

    def test_single_request_id_for_every_event(self):
        ids = {e["request_id"] for e in self.sink.events}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.done["outcome"], "success")

    def test_output_identical_with_observability_disabled(self):
        disabled, sink, _ = fx.run_graphrag(sink=NullSink())
        self.assertEqual(disabled, self.output)

    def test_output_identical_when_the_sink_fails(self):
        emitter._internal_counts.clear()
        with self.assertLogs("chatbot.observability.internal", level="WARNING"):
            failing, _, _ = fx.run_graphrag(sink=_ExplodingSink())
        self.assertEqual(failing, self.output)

    def test_pipeline_opens_its_own_root_when_called_directly(self):
        _, sink, _ = fx.run_graphrag(open_root=False)
        self.assertEqual(len(sink.of(ev.REQUEST_STARTED)), 1)
        self.assertEqual(_one(sink, ev.REQUEST_COMPLETED)["llm_call_count"], 2)


class TestGraphRagEdgeScenarios(unittest.TestCase):
    def test_graph_empty_vector_success(self):
        output, sink, _ = fx.run_graphrag(graph_source=fx.RowSource([]))
        graph = _one(sink, ev.RETRIEVAL_GRAPH)
        self.assertEqual((graph["status"], graph["result_count"]), ("empty", 0))
        self.assertEqual(_one(sink, ev.RETRIEVAL_VECTOR)["status"], "success")
        self.assertEqual(_one(sink, ev.NEO4J_QUERY,
                              operation="generated_graph_query")["status"], "empty")
        self.assertTrue(_one(sink, ev.SYNTHESIS)["synthesis_executed"])
        self.assertEqual(dict(fx.purposes(sink)),
                         {"cypher_generation": 1, "final_synthesis": 1})
        self.assertEqual(_one(sink, ev.REQUEST_COMPLETED)["outcome"], "success")
        self.assertTrue(output)

    def test_all_retrieval_failed_short_circuits_without_llm(self):
        from chatbot.synthesis.evidence_format import retrieval_failure_message

        def boom(q, lang, h=None):
            raise RuntimeError("provider down")

        with self.assertLogs("tools.orchestrator", level="WARNING"):
            output, sink, parts = fx.run_graphrag(graph_retriever=boom,
                                                  vector_retriever=boom)
        self.assertEqual(output, retrieval_failure_message("ko"))   # unchanged answer
        done = _one(sink, ev.REQUEST_COMPLETED)
        self.assertEqual(done["outcome"], "short_circuit")
        self.assertEqual(done["llm_call_count"], 0)
        self.assertEqual(done["llm_calls_by_purpose"]["final_synthesis"], 0)
        self.assertEqual(done["citation_count"], 0)
        synthesis = _one(sink, ev.SYNTHESIS)
        self.assertEqual((synthesis["status"], synthesis["synthesis_executed"]),
                         ("skipped", False))
        for name in (ev.RETRIEVAL_GRAPH, ev.RETRIEVAL_VECTOR):
            event = _one(sink, name)
            self.assertEqual((event["status"], event["error_type"]),
                             ("error", "RuntimeError"))
            self.assertRegex(event["correlation_id"], r"^[0-9a-f]{8}$")
        self.assertTrue(_one(sink, ev.PIPELINE_GRAPHRAG)["short_circuit"])
        self.assertEqual(sink.of(ev.HISTORY_WRITE), [])
        self.assertEqual(parts.history.messages, [])

    def test_provider_usage_absent(self):
        llm = fx.scripted_llm((fx.GRAPHRAG_CYPHER, None), ("답변", None))
        _, sink, _ = fx.run_graphrag(llm=llm)
        for event in sink.of(ev.LLM):
            self.assertFalse(event["usage_available"])
            self.assertIsNone(event["total_tokens"])
        done = _one(sink, ev.REQUEST_COMPLETED)
        self.assertEqual(done["llm_call_count"], 2)
        self.assertEqual(done["usage_available_call_count"], 0)
        self.assertIsNone(done["input_tokens"])


# ── VectorRAG ────────────────────────────────────────────────────────────────
class TestVectorRagBaseline(unittest.TestCase):
    def setUp(self):
        self.output, self.sink, self.parts = fx.run_vectorrag()
        self.done = _one(self.sink, ev.REQUEST_COMPLETED)

    def test_embedding_vector_query_and_answer_are_separated(self):
        emb = _one(self.sink, ev.EMBEDDING)
        self.assertEqual(emb["purpose"], "vector_query")
        vq = _one(self.sink, ev.NEO4J_QUERY, operation="vector_query")
        self.assertEqual(vq["row_count"], 2)
        self.assertEqual(dict(fx.purposes(self.sink)), {"text_rag_answer": 1})
        retrieval = _one(self.sink, ev.RETRIEVAL_VECTOR)
        self.assertEqual((retrieval["result_count"], retrieval["attempt_count"]), (2, 1))
        order = self.sink.names()
        self.assertLess(order.index(ev.EMBEDDING), order.index(ev.NEO4J_QUERY))
        self.assertLess(order.index(ev.RETRIEVAL_VECTOR), order.index(ev.LLM))

    def test_request_summary_has_the_same_schema_as_graphrag(self):
        _, graph_sink, _ = fx.run_graphrag()
        graph_done = _one(graph_sink, ev.REQUEST_COMPLETED)
        self.assertEqual(set(self.done), set(graph_done))
        self.assertEqual((self.done["mode"], self.done["route"]), ("vectorrag", "vectorrag"))
        self.assertEqual(self.done["llm_call_count"], 1)
        self.assertEqual(self.done["embedding_call_count"], 1)
        self.assertEqual(self.done["answer_chars"], len(self.output))
        self.assertGreater(self.done["citation_count"], 0)
        self.assertEqual(_one(self.sink, ev.PIPELINE_VECTORRAG)["status"], "success")

    def test_output_identical_with_observability_disabled(self):
        disabled, _, _ = fx.run_vectorrag(sink=NullSink())
        self.assertEqual(disabled, self.output)
        self.assertEqual(self.parts.session.post.call_count, 1)   # one embedding call


# ── Neo4j boundary ───────────────────────────────────────────────────────────
class TestNeo4jBoundary(unittest.TestCase):
    def test_safety_rejection_records_no_db_call(self):
        from tools.cypher_safety import SafeNeo4jGraph, UnsafeCypherError

        source = fx.RowSource(fx.GRAPH_ROWS)
        sink = MemorySink()
        with telemetry.use_sink(sink), \
                telemetry.neo4j_operation("generated_graph_query", "generated"):
            with self.assertRaises(UnsafeCypherError):
                SafeNeo4jGraph(source).query("MATCH (n) DETACH DELETE n RETURN 1")
        self.assertEqual(source.calls, 0)
        event = _one(sink, ev.NEO4J_QUERY)
        self.assertEqual((event["status"], event["safety_result"], event["attempt_count"]),
                         ("skipped", "rejected", 0))
        self.assertIsNone(event["row_count"])
        self.assertEqual(event["error_type"], "UnsafeCypherError")
        self.assertNotIn("DELETE", json.dumps(sink.events))

    def test_transport_retries_are_counted_exactly(self):
        from tools.cypher_safety import READ_RETRY_MAX_ATTEMPTS, SafeNeo4jGraph

        source = fx.RowSource(error=OSError("connection reset"))
        sink = MemorySink()
        with telemetry.use_sink(sink), patch("tools.cypher_safety.time.sleep"):
            with self.assertRaises(OSError):
                SafeNeo4jGraph(source).query("MATCH (n) RETURN n.ID AS id LIMIT 1")
        event = _one(sink, ev.NEO4J_QUERY)
        self.assertEqual((event["status"], event["attempt_count"], event["error_type"]),
                         ("error", READ_RETRY_MAX_ATTEMPTS, "OSError"))
        self.assertEqual(source.calls, READ_RETRY_MAX_ATTEMPTS)
        self.assertEqual(event["operation"], "other")    # unlabelled caller

    def test_production_subclass_variant_is_instrumented_identically(self):
        from langchain_neo4j import Neo4jGraph
        from tools.cypher_safety import safe_graph

        record = SimpleNamespace(data=lambda: {"person_id": "P1"})
        driver = MagicMock()
        driver.execute_query.return_value = ([record, record], None, None)
        inner = object.__new__(Neo4jGraph)
        inner.__dict__.update(_driver=driver, _database="neo4j", timeout=None,
                              sanitize=False)
        wrapped = safe_graph(inner)
        self.assertIsInstance(wrapped, Neo4jGraph)       # the pydantic-safe variant
        sink = MemorySink()
        with telemetry.use_sink(sink), \
                telemetry.neo4j_operation("deterministic_lookup", "deterministic"):
            rows = wrapped.query("MATCH (p:Person) RETURN p.ID AS person_id LIMIT 2")
        self.assertEqual(rows, [{"person_id": "P1"}, {"person_id": "P1"}])
        event = _one(sink, ev.NEO4J_QUERY)
        self.assertEqual((event["operation"], event["query_origin"], event["row_count"],
                          event["safety_result"], event["attempt_count"]),
                         ("deterministic_lookup", "deterministic", 2, "passed", 1))
        self.assertEqual(driver.execute_query.call_count, 1)

    def test_role_ranking_is_a_deterministic_lookup(self):
        from chatbot.retrieval import graph_query
        from tools.cypher_safety import SafeNeo4jGraph

        source = fx.RowSource([{"person_id": "P900", "person_name_kor": "세종",
                                "mention_count": 12}])
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with telemetry.span(ev.RETRIEVAL_GRAPH, retriever="role_ranking",
                                count_attempts=True):
                evidence = graph_query.retrieve_role_ranking_evidence(
                    "king", graph=SafeNeo4jGraph(source))
        self.assertTrue(evidence.documents)
        event = _one(sink, ev.NEO4J_QUERY)
        self.assertEqual((event["operation"], event["query_origin"]),
                         ("deterministic_lookup", "deterministic"))
        self.assertEqual(_one(sink, ev.RETRIEVAL_GRAPH)["attempt_count"], 1)


# ── Embedding client ─────────────────────────────────────────────────────────
class TestEmbeddingClient(unittest.TestCase):
    def test_retry_attempts_counted_and_dimension_recorded(self):
        retry = fx.embedding_response(status=429)
        retry.headers["Retry-After"] = "0"
        client, session = fx.fake_embeddings(
            responses=[retry, retry, fx.embedding_response(dim=4)])
        sink = MemorySink()
        with telemetry.use_sink(sink), patch("llm.time.sleep"):
            with telemetry.request_scope(mode="textRAG"):
                vector = client.embed_query("질문")
        self.assertEqual(len(vector), 4)
        event = _one(sink, ev.EMBEDDING)
        self.assertEqual((event["status"], event["attempt_count"], event["dimension"],
                          event["input_chars"]), ("success", 3, 4, 2))
        self.assertEqual(_one(sink, ev.REQUEST_COMPLETED)["embedding_call_count"], 1)

    def test_provider_rejection_recorded_and_propagated(self):
        from errors import ConfigurationError

        client, _ = fx.fake_embeddings(responses=[fx.embedding_response(status=400)])
        sink = MemorySink()
        with telemetry.use_sink(sink):
            with self.assertRaises(ConfigurationError):
                client.embed_query("x")
        event = _one(sink, ev.EMBEDDING)
        self.assertEqual((event["status"], event["error_type"], event["attempt_count"]),
                         ("error", "ConfigurationError", 1))

    def test_document_indexing_purpose_and_empty_input(self):
        client, session = fx.fake_embeddings(responses=[SimpleNamespace(
            status_code=200, headers={"content-type": "application/json"},
            json=MagicMock(return_value={"embeddings": [{"values": [1.0, 2.0]},
                                                        {"values": [3.0, 4.0]}]}),
            raise_for_status=MagicMock())])
        sink = MemorySink()
        with telemetry.use_sink(sink):
            self.assertEqual(client.embed_documents([]), [])   # no call, no event
            client.embed_documents(["a", "bb"])
        event = _one(sink, ev.EMBEDDING)
        self.assertEqual((event["purpose"], event["input_count"], event["input_chars"],
                          event["dimension"]), ("document_indexing", 2, 3, 2))
        self.assertEqual(session.post.call_count, 1)


# ── External authority ───────────────────────────────────────────────────────
class TestAuthority(unittest.TestCase):
    def setUp(self):
        from chatbot.authority.cache import clear_authority_cache

        clear_authority_cache()
        self.addCleanup(clear_authority_cache)

    def test_cache_miss_then_hit(self):
        from chatbot.authority.service import fetch_authority

        calls = []

        def fetcher(url):
            calls.append(url)
            return {"entities": {"Q1": {"labels": {"en": {"value": "Heo"}}}}}

        sink = MemorySink()
        with telemetry.use_sink(sink):
            first = fetch_authority("wikidata", "Q1", fetcher=fetcher)
            second = fetch_authority("wikidata", "Q1", fetcher=fetcher)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        miss, hit = sink.of(ev.RETRIEVAL_AUTHORITY_SOURCE)
        self.assertEqual((miss["cache_hit"], miss["http_fetched"], miss["attempt_count"],
                          miss["status"]), (False, True, 1, "success"))
        self.assertEqual((hit["cache_hit"], hit["http_fetched"], hit["attempt_count"],
                          hit["status"]), (True, False, 0, "success"))
        self.assertEqual((miss["source"], miss["node_type"]), ("wikidata", "Person"))

    def test_failures_are_not_cached_and_link_only_is_skipped(self):
        from chatbot.authority.service import fetch_authority

        sink = MemorySink()
        with telemetry.use_sink(sink):
            fetch_authority("wikidata", "Q2", fetcher=lambda url: None)
            fetch_authority("wikidata", "Q2", fetcher=lambda url: None)
            fetch_authority("aks_ency", "E0043772")
            fetch_authority("nlk", "whatever")
        failed_1, failed_2, link_only, unsupported = sink.of(ev.RETRIEVAL_AUTHORITY_SOURCE)
        for failed in (failed_1, failed_2):
            self.assertEqual((failed["status"], failed["cache_hit"], failed["http_fetched"]),
                             ("error", False, True))
        self.assertEqual((link_only["status"], link_only["http_fetched"],
                          link_only["attempt_count"]), ("skipped", False, 0))
        self.assertEqual(unsupported["status"], "skipped")

    def test_http_client_records_status_and_size_only(self):
        from chatbot.authority import client

        ok = MagicMock(status_code=200, headers={"content-type": "application/json"},
                       content=b'{"a":1}')
        ok.json.return_value = {"a": 1}
        missing = MagicMock(status_code=404, headers={}, content=b"")
        sink = MemorySink()
        with telemetry.use_sink(sink), \
                patch.object(client.requests, "get", side_effect=[ok, missing]):
            self.assertEqual(client._fetch("https://secret.example/Q999?token=abc"), {"a": 1})
            self.assertIsNone(client._fetch("https://secret.example/Q998"))
        good, bad = sink.of(ev.AUTHORITY_HTTP)
        self.assertEqual((good["status"], good["http_status_code"],
                          good["http_status_class"], good["response_bytes"]),
                         ("success", 200, "2xx", 7))
        self.assertEqual((bad["status"], bad["http_status_code"], bad["http_status_class"]),
                         ("error", 404, "4xx"))
        blob = json.dumps(sink.events)
        for leaked in ("secret.example", "Q999", "token", "abc"):
            self.assertNotIn(leaked, blob)

    def test_orchestrator_authority_span(self):
        from tools.evidence import Entity, Evidence
        from tools.orchestrator import gather_graphrag_evidence

        def graph_r(q, lang, h=None):
            ev_ = Evidence(kind="graph", documents=[{"person_id": "P553"}])
            ev_.entities.append(Entity(node_id="P553", node_type="Person",
                                       authority_ids={"wikidata": "Q1"}))
            return ev_

        def fetcher(source, ext_id, language, node_type="Person"):
            return {"source": source, "label": "Wikidata", "status": "ok",
                    "url": "https://x", "data": {}}

        sink = MemorySink()
        with telemetry.use_sink(sink):
            gather_graphrag_evidence("허초희의 생몰년은?", "ko", graph_retriever=graph_r,
                                     vector_retriever=lambda q, l: Evidence(kind="vector"),
                                     authority_fetcher=fetcher)
        authority = _one(sink, ev.RETRIEVAL_AUTHORITY)
        self.assertEqual((authority["status"], authority["result_count"]), ("success", 1))
        self.assertEqual(_one(sink, ev.RETRIEVAL_VECTOR)["status"], "empty")


# ── agent.py wiring + fallback (isolated interpreter, stubbed clients) ──────
_AGENT_DRIVER = textwrap.dedent(r'''
    import json, sys, types
    sys.path.insert(0, {repo!r})
    from unittest.mock import patch
    sys.path.insert(0, {tests!r})
    from langchain_core.chat_history import InMemoryChatMessageHistory
    from langchain_core.messages import AIMessage
    from langchain_neo4j import Neo4jGraph
    from _observability_fixtures import UsageStreamingChat   # ReAct streams its LLM

    # Stub the two client modules BEFORE agent.py imports them: no secrets,
    # no Neo4j connection, no Gemini. Everything else is the real code.
    fake_graph = object.__new__(Neo4jGraph)
    fake_graph.__dict__.update(
        structured_schema={{"node_props": {{}}, "rel_props": {{}}, "relationships": []}},
        _enhanced_schema=False, schema="", _database="neo4j", timeout=None,
        sanitize=False)
    graph_mod = types.ModuleType("graph"); graph_mod.graph = fake_graph
    react_reply = AIMessage(
        content="Thought: Do I need to use a tool? No\nFinal Answer: react-answer",
        usage_metadata={{"input_tokens": 50, "output_tokens": 9, "total_tokens": 59}})
    llm_mod = types.ModuleType("llm")
    llm_mod.llm = UsageStreamingChat(messages=iter([react_reply] * 20))
    llm_mod.embeddings = object()
    sys.modules["graph"] = graph_mod
    sys.modules["llm"] = llm_mod

    import streamlit as st
    import agent, text_rag
    import tools.cypher, tools.vector
    from chatbot.legacy import react_agent
    from chatbot.observability import telemetry
    from chatbot.observability.sinks import MemorySink
    from errors import ConfigurationError, TransientProviderError

    def purpose(binding):
        assert binding.bound is llm_mod.llm, "purpose binding must wrap the same model"
        return binding.config["metadata"]["llm_purpose"]

    report = {{"wiring": {{
        "structured_cypher": purpose(tools.cypher.cypher_qa_structured.cypher_generation_chain.steps[1]),
        "structured_qa": purpose(tools.cypher.cypher_qa_structured.qa_chain.steps[1]),
        "legacy_cypher": purpose(tools.cypher.cypher_qa.cypher_generation_chain.steps[1]),
        "legacy_qa": purpose(tools.cypher.cypher_qa.qa_chain.steps[1]),
        "synthesis": purpose(agent.synthesis_chain.steps[1]),
        "react": purpose(agent._react_llm),
        "general_chat": purpose(agent._general_chat_llm),
        "legacy_vector": purpose(tools.vector._legacy_answer_llm),
        "text_rag": purpose(text_rag._answer_llm),
    }}}}
    report["return_direct"] = {{
        "structured": tools.cypher.cypher_qa_structured.return_direct,
        "legacy": tools.cypher.cypher_qa.return_direct,
    }}
    agent.chat_agent = react_agent.build_chat_agent(
        agent.agent_executor, lambda sid: InMemoryChatMessageHistory())

    def run(exc, *, bot_scope):
        sink = MemorySink()
        def boom(*a, **k):
            raise exc
        with telemetry.use_sink(sink), \
                patch.dict(st.session_state, {{"response_language": "en",
                                              "question_language": "en"}}, clear=False), \
                patch.object(agent, "synthesize_answer", side_effect=boom):
            if bot_scope:      # mimic bot.py: root scope + catch-all handler
                with telemetry.request_scope(mode="graphRAG", question_language="en",
                                             response_language="en"):
                    try:
                        out = agent.generate_response("who?")
                    except Exception as caught:
                        telemetry.record_request_error(caught, correlation_id="feedbeef")
                        out = "INIT_FAILURE"
                    telemetry.set_answer_stats(answer_chars=len(out))
            else:              # CLI: agent.generate_response owns the scope
                out = agent.generate_response("who?")
        return {{"out": out, "events": sink.events}}

    report["transient"] = run(TransientProviderError("gemini 503",
                                                      correlation_id="abcd1234"),
                              bot_scope=True)
    report["transient_cli"] = run(TransientProviderError("x"), bot_scope=False)
    report["configuration"] = run(ConfigurationError("no key"), bot_scope=True)

    class _Broken:
        def invoke(self, *a, **k):
            raise TransientProviderError("react also down")
    agent.chat_agent = _Broken()
    report["fallback_error"] = run(TransientProviderError("gemini 503"), bot_scope=True)
    print("REPORT" + json.dumps(report, ensure_ascii=False))
''')


class TestAgentWiringAndFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-c",
             _AGENT_DRIVER.format(repo=str(REPO), tests=str(TESTS))],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=300)
        lines = [l for l in proc.stdout.splitlines() if l.startswith("REPORT")]
        if proc.returncode != 0 or not lines:
            raise AssertionError(f"agent driver failed:\n{proc.stdout[-3000:]}\n"
                                 f"{proc.stderr[-3000:]}")
        cls.report = json.loads(lines[-1][len("REPORT"):])

    def _events(self, scenario, name):
        return [e for e in self.report[scenario]["events"] if e["event_name"] == name]

    def test_production_modules_bind_the_right_purpose_to_the_same_model(self):
        self.assertEqual(self.report["wiring"], {
            "structured_cypher": "cypher_generation", "structured_qa": "graph_qa",
            "legacy_cypher": "cypher_generation", "legacy_qa": "graph_qa",
            "synthesis": "final_synthesis", "react": "react_iteration",
            "general_chat": "general_chat", "legacy_vector": "legacy_vector_answer",
            "text_rag": "text_rag_answer"})

    def test_only_structured_production_chain_bypasses_graph_qa(self):
        self.assertEqual(self.report["return_direct"], {"structured": True, "legacy": False})

    def test_transient_error_falls_back_with_the_same_request_id(self):
        scenario = self.report["transient"]
        self.assertEqual(scenario["out"], "react-answer")
        ids = {e["request_id"] for e in scenario["events"]}
        self.assertEqual(len(ids), 1)
        (fallback,) = self._events("transient", "fallback.completed")
        self.assertEqual((fallback["status"], fallback["route"],
                          fallback["trigger_error_type"], fallback["correlation_id"]),
                         ("success", "react_fallback", "TransientProviderError", "abcd1234"))
        (llm,) = self._events("transient", "llm.completed")
        self.assertEqual((llm["purpose"], llm["route"], llm["total_tokens"]),
                         ("react_iteration", "react_fallback", 59))
        (done,) = self._events("transient", "request.completed")
        self.assertEqual((done["outcome"], done["fallback_used"], done["route"],
                          done["llm_call_count"]),
                         ("fallback_success", True, "react_fallback", 1))
        self.assertEqual(done["answer_chars"], len("react-answer"))
        self.assertEqual(len(self._events("transient", "request.started")), 1)

    def test_cli_entry_owns_its_request_scope(self):
        (done,) = self._events("transient_cli", "request.completed")
        self.assertEqual(done["outcome"], "fallback_success")
        self.assertEqual(self.report["transient_cli"]["out"], "react-answer")

    def test_non_transient_error_never_falls_back(self):
        scenario = self.report["configuration"]
        self.assertEqual(self._events("configuration", "fallback.completed"), [])
        self.assertEqual(self._events("configuration", "llm.completed"), [])
        (done,) = self._events("configuration", "request.completed")
        self.assertEqual((done["outcome"], done["fallback_used"], done["route"]),
                         ("error", False, "graphrag"))
        self.assertNotEqual(scenario["out"], "react-answer")

    def test_fallback_failure_is_fallback_error(self):
        self.assertEqual(self.report["fallback_error"]["out"], "INIT_FAILURE")
        (fallback,) = self._events("fallback_error", "fallback.completed")
        self.assertEqual((fallback["status"], fallback["error_type"]),
                         ("error", "TransientProviderError"))
        (done,) = self._events("fallback_error", "request.completed")
        self.assertEqual((done["outcome"], done["fallback_used"]), ("fallback_error", True))
        # bot.py's catch-all links its user-visible [ref: ...] code to the event
        self.assertEqual((done["error_type"], done["correlation_id"]),
                         ("TransientProviderError", "feedbeef"))


if __name__ == "__main__":
    unittest.main()

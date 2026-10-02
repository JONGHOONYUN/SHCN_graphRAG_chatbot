"""Privacy regression (work order §6, §13.7).

Unique marker strings are planted in every place raw content could leak from —
the question, the conversation history, the generated Cypher, graph rows,
vector document text, the LLM outputs, exception messages, external URLs and
external response bodies. Every event produced by complete GraphRAG and
VectorRAG runs (plus Neo4j, embedding and authority boundaries) is serialized
to JSON and must contain none of the markers and none of the forbidden keys.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import _observability_fixtures as fx  # noqa: E402
from chatbot.observability import events as ev, telemetry  # noqa: E402
from chatbot.observability.sinks import MemorySink  # noqa: E402

M = {
    "question": "MK_QUESTION_91a",
    "history": "MK_HISTORY_22b",
    "cypher": "MK_CYPHER_33c",
    "graph_row": "MK_GRAPHROW_44d",
    "vector_text": "MK_VECTOR_55e",
    "graph_qa": "MK_GRAPHQA_66f",
    "answer": "MK_ANSWER_77g",
    "exception": "MK_EXCEPTION_88h",
    "url": "MK_URL_99i",
    "external_id": "Q7654321",
    "external_body": "MK_BODY_00j",
}

FORBIDDEN_KEYS = {"question", "prompt", "messages", "answer", "cypher",
                  "query_text", "evidence_text", "response_body", "api_key",
                  "password", "embedding_vector"}


def _all_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(key)
            _all_keys(inner, found)
    elif isinstance(value, list):
        for inner in value:
            _all_keys(inner, found)
    return found


class TestNoRawContentInEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sink = MemorySink()
        cls.outputs = []
        cypher = (f"MATCH (p:Person) WHERE p.nameKor CONTAINS '{M['cypher']}' "
                  "RETURN p.ID AS person_id, p.nameKor AS person_name_kor LIMIT 5")
        rows = [{"person_id": "P553", "person_name_kor": M["graph_row"]}]
        vector_rows = [dict(fx.VECTOR_ROWS[0], text=M["vector_text"],
                            metadata=dict(fx.VECTOR_ROWS[0]["metadata"],
                                          korean_translation=M["vector_text"]))]
        history = fx.History()
        history.add_user_message(M["history"])
        history.add_ai_message(M["history"])

        # 1) full GraphRAG run with markers everywhere
        llm = fx.scripted_llm((cypher, fx.usage(1, 1)),
                              (M["answer"], fx.usage(2, 2)))
        out, _, _ = fx.run_graphrag(question=M["question"], sink=cls.sink, llm=llm,
                                    graph_source=fx.RowSource(rows),
                                    vector_rows=vector_rows, history=history)
        cls.outputs.append(out)

        # Graph QA is now only used by the legacy prose path. Keep its marker
        # coverage as well as the normal direct-retrieval pipeline coverage.
        legacy = fx.structured_graph_chain(
            fx.scripted_llm((cypher, fx.usage(1, 1)), (M["graph_qa"], None)),
            fx.RowSource(rows), direct=False)
        with telemetry.use_sink(cls.sink), telemetry.request_scope(mode="graphRAG"):
            cls.legacy_result = legacy.invoke({"query": M["question"]})["result"]

        # 2) failing graph source whose exception message carries a marker
        with patch("tools.cypher_safety.time.sleep"):
            fx.run_graphrag(question=M["question"], sink=cls.sink,
                            llm=fx.scripted_llm((cypher, None), (M["answer"], None)),
                            graph_source=fx.RowSource(error=RuntimeError(M["exception"])))

        # 3) VectorRAG run
        out, _, _ = fx.run_vectorrag(question=M["question"], sink=cls.sink,
                                     llm=fx.scripted_llm((M["answer"], None)),
                                     rows=vector_rows)
        cls.outputs.append(out)

        # 4) external authority: URL, identifier and body markers
        from chatbot.authority import client
        from chatbot.authority.cache import clear_authority_cache
        from chatbot.authority.service import fetch_authority

        clear_authority_cache()
        body = json.dumps({"entities": {M["external_id"]: {"labels": {
            "en": {"value": M["external_body"]}}}}}).encode()
        resp = MagicMock(status_code=200, headers={"content-type": "application/json"},
                         content=body)
        resp.json.return_value = json.loads(body)
        with telemetry.use_sink(cls.sink), \
                patch.object(client.requests, "get", return_value=resp):
            with telemetry.request_scope(mode="graphRAG"):
                result = fetch_authority("wikidata", M["external_id"])
                client._fetch(f"https://example.org/{M['url']}?q={M['external_id']}")
        clear_authority_cache()
        cls.authority_result = result

        cls.blob = json.dumps(cls.sink.events, ensure_ascii=False)

    def test_markers_really_flowed_through_the_system(self):
        """Guard against a vacuous test: the answers and results DO contain
        the markers — only the telemetry must not."""
        self.assertIn(M["answer"], self.outputs[0])
        self.assertIn(M["answer"], self.outputs[1])
        self.assertEqual(self.legacy_result, M["graph_qa"])
        self.assertIn(M["external_body"], json.dumps(self.authority_result))
        self.assertGreater(len(self.sink.events), 40)

    def test_no_marker_in_any_event(self):
        for name, marker in M.items():
            self.assertNotIn(marker, self.blob, f"{name} marker leaked into telemetry")

    def test_no_forbidden_key_anywhere(self):
        keys = _all_keys(self.sink.events)
        self.assertFalse(keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS)
        self.assertFalse(keys & ev.FORBIDDEN_FIELD_NAMES)
        # every key in every event belongs to the declared schema
        schema = ev.ALLOWED_FIELDS | set(ev.LLM_PURPOSES)
        self.assertLessEqual(keys, schema, keys - schema)

    def test_derived_sizes_are_present_instead_of_content(self):
        done = self.sink.of(ev.REQUEST_COMPLETED)[0]
        self.assertIn("answer_chars", done)
        neo4j = self.sink.where(ev.NEO4J_QUERY, operation="generated_graph_query")
        self.assertTrue(neo4j and "query_origin" in neo4j[0])

    def test_error_events_carry_class_names_only(self):
        errors = [e for e in self.sink.events if e.get("error_type")]
        self.assertTrue(errors)
        for event in errors:
            self.assertRegex(event["error_type"], r"^[A-Za-z_][A-Za-z0-9_]*$")
            self.assertNotIn("error_message", event)


if __name__ == "__main__":
    unittest.main()

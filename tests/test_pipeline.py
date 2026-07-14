"""Unit tests for the graphRAG evidence pipeline.

Runs with the stdlib unittest runner (no pytest, no live Neo4j, no API keys):

    python -m unittest tests.test_pipeline -v

Neo4j, the vector retriever, and HTTP are all mocked via dependency injection,
so none of these tests import streamlit-bound modules (graph.py / llm.py) or
make a network call.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.evidence import (  # noqa: E402
    Entity,
    Evidence,
    collect_person_entities,
    docs_to_evidence,
    graph_rows_to_evidence,
    merge_entities,
)
from tools.orchestrator import gather_graphrag_evidence, needs_authority  # noqa: E402
from tools import external_authority as ea  # noqa: E402
from tools import synthesis  # noqa: E402


def person(node_id=None, wikidata=None, aks=None, name="이규보"):
    return Entity(
        node_id=node_id, node_type="Person", name_kor=name,
        wikidata_id=wikidata, aks_digerati_id=aks,
    )


class RecordingFetcher:
    """Fake authority fetcher; records (source, id) calls."""

    def __init__(self, status="ok", data=None):
        self.calls = []
        self.status = status
        self.data = data or {"primary_name": "李奎報", "birth_time": "+1168-00-00T00:00:00Z"}

    def __call__(self, source, ext_id, language):
        self.calls.append((source, ext_id))
        if self.status != "ok":
            return {"source": source, "id": ext_id, "status": self.status,
                    "error": "unavailable"}
        return {"source": source, "id": ext_id, "status": "ok",
                "url": f"http://example/{ext_id}", "data": dict(self.data)}


class TestAuthorityGating(unittest.TestCase):
    def test_poem_list_question_needs_no_authority(self):
        # Criterion 4
        self.assertFalse(needs_authority("황진이는 어떤 시를 썼나요?"))
        self.assertFalse(needs_authority("이규보가 쓴 시를 보여주세요"))
        self.assertFalse(needs_authority("show me poems written by Yi Kyubo"))

    def test_biography_question_needs_authority(self):
        self.assertTrue(needs_authority("이규보에 대해 자세히 알려줘"))
        self.assertTrue(needs_authority("이규보의 생몰년은?"))
        self.assertTrue(needs_authority("tell me about Yi Kyubo's biography"))
        self.assertTrue(needs_authority("李奎報的生平"))


class TestOrchestrator(unittest.TestCase):
    def _run(self, graph_ev, vector_ev, fetcher, question="이규보에 대해 자세히 알려줘", **kw):
        return gather_graphrag_evidence(
            question, "ko",
            graph_retriever=lambda q, l: graph_ev,
            vector_retriever=lambda q, l: vector_ev,
            authority_fetcher=fetcher,
            **kw,
        )

    def test_both_ids_trigger_one_lookup_each(self):
        # Criterion 1
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1", aks="koreanPerson_1")])
        v = Evidence(kind="vector")
        f = RecordingFetcher()
        self._run(g, v, f)
        self.assertEqual(sorted(f.calls), [("aks_digerati", "koreanPerson_1"), ("wikidata", "Q1")])

    def test_duplicate_entities_one_lookup_per_id(self):
        # Criterion 2 — same person from graph and vector; ids split across sources
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1")])
        v = Evidence(kind="vector", entities=[person("P1", aks="koreanPerson_1")])
        f = RecordingFetcher()
        r = self._run(g, v, f)
        self.assertEqual(len(r["persons"]), 1)
        self.assertEqual(sorted(f.calls), [("aks_digerati", "koreanPerson_1"), ("wikidata", "Q1")])

    def test_vector_only_person_enriched_for_biography(self):
        # Criterion 3
        g = Evidence(kind="graph")
        v = Evidence(kind="vector", entities=[person("P9", wikidata="Q9")])
        f = RecordingFetcher()
        self._run(g, v, f, question="이 인물에 대해 자세히 알려줘")
        self.assertEqual(f.calls, [("wikidata", "Q9")])

    def test_poem_list_calls_no_authority(self):
        # Criterion 4
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1", aks="koreanPerson_1")])
        v = Evidence(kind="vector")
        f = RecordingFetcher()
        r = self._run(g, v, f, question="황진이는 어떤 시를 썼나요?")
        self.assertEqual(f.calls, [])
        self.assertFalse(r["authority_attempted"])

    def test_missing_ids_no_name_lookup(self):
        # Criterion 5 — person with no authority id, biography question
        g = Evidence(kind="graph", entities=[person(node_id="P2", name="아무개")])
        v = Evidence(kind="vector")
        f = RecordingFetcher()
        self._run(g, v, f, question="이 인물의 생애를 자세히 알려줘")
        self.assertEqual(f.calls, [])

    def test_failed_authority_keeps_evidence_no_invention(self):
        # Criterion 6
        g = Evidence(kind="graph", documents=[{"person_name_kor": "이규보"}],
                     entities=[person("P1", wikidata="Q1")])
        v = Evidence(kind="vector", documents=[{"textKor": "원문"}])
        f = RecordingFetcher(status="unavailable")
        r = self._run(g, v, f)
        self.assertTrue(r["graph"].documents)      # graph evidence retained
        self.assertTrue(r["vector"].documents)      # vector evidence retained
        claim = r["external"].claims[0]
        self.assertEqual(claim["status"], "unavailable")
        self.assertNotIn("data", claim)             # no invented external fact

    def test_authority_cap(self):
        # Criterion 7 — cap defaults to 3 people
        ents = [person(f"P{i}", wikidata=f"Q{i}") for i in range(10)]
        g = Evidence(kind="graph", entities=ents)
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f)
        self.assertEqual(len(f.calls), 3)


class TestSynthesisFormatting(unittest.TestCase):
    def test_source_labels_and_bounded_payload(self):
        # Criterion 8 — labels present, raw unbounded payload excluded/truncated
        big = "X" * 50000
        evidence = {
            "graph": Evidence(kind="graph", documents=[{"person_name_kor": "이규보", "blob": big}]),
            "vector": Evidence(kind="vector", documents=[{"textKor": "원문", "entry_id": "E003"}]),
            "external": Evidence(kind="external", claims=[{
                "person": "이규보", "source": "wikidata", "status": "ok",
                "url": "http://x", "data": {"primary_name": "李奎報", "raw_blob": big},
            }]),
        }
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("## Graph Evidence", out)
        self.assertIn("## Vector Evidence", out)
        self.assertIn("## External Authority Evidence", out)
        # raw_blob is not in the wikidata allowlist -> excluded
        self.assertNotIn("raw_blob", out)
        # even the graph blob is truncated: no single 50k run survives
        self.assertNotIn("X" * 5000, out)

    def test_conflicting_dates_shown_separately(self):
        # Criterion 7 — graph date and external date both present, not merged
        evidence = {
            "graph": Evidence(kind="graph", documents=[{"person_name_kor": "이규보", "yearBirth": 1168}]),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external", claims=[{
                "person": "이규보", "source": "wikidata", "status": "ok",
                "url": "http://x", "data": {"birth_time": "+1200-00-00T00:00:00Z"},
            }]),
        }
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("1168", out)   # graph value
        self.assertIn("1200", out)   # external value, separately labelled block

    def test_unavailable_authority_marked(self):
        evidence = {
            "graph": Evidence(kind="graph"),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external", claims=[{
                "person": "이규보", "source": "aks_digerati", "status": "unavailable",
            }]),
        }
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("UNAVAILABLE", out)

    def test_build_citations_never_fabricates_link(self):
        evidence = {
            "graph": Evidence(kind="graph"),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external", claims=[
                {"person": "A", "source": "wikidata", "status": "ok", "url": "http://wd/Q1"},
                {"person": "B", "source": "aks_digerati", "status": "ok"},  # no url
                {"person": "C", "source": "wikidata", "status": "unavailable"},
            ]),
        }
        cites = synthesis.build_citations(evidence)
        joined = "\n".join(cites)
        self.assertIn("Wikidata: [A](http://wd/Q1)", joined)
        self.assertNotIn("B", joined)   # no url -> no citation
        self.assertNotIn("C", joined)   # unavailable -> no citation


class TestExternalAuthority(unittest.TestCase):
    def setUp(self):
        ea.clear_authority_cache()

    def test_fetch_ok_and_cache(self):
        seen = []

        def fake(url):
            seen.append(url)
            return {"entities": {"Q1": {"labels": {"en": {"value": "Yi Kyubo"}}}}}

        r1 = ea.fetch_authority("wikidata", "Q1", fetcher=fake)
        r2 = ea.fetch_authority("wikidata", "Q1", fetcher=fake)  # cached
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(r1["data"]["primary_name"], "Yi Kyubo")
        self.assertEqual(len(seen), 1)  # second call served from cache

    def test_fetch_unavailable_not_cached(self):
        calls = []

        def failing(url):
            calls.append(url)
            return None

        r1 = ea.fetch_authority("wikidata", "Q2", fetcher=failing)
        r2 = ea.fetch_authority("wikidata", "Q2", fetcher=failing)
        self.assertEqual(r1["status"], "unavailable")
        self.assertEqual(len(calls), 2)  # failure not cached -> retried

    def test_aks_id_transform_and_canonical_link(self):
        def fake(url):
            self.assertIn("/18816", url)  # integer id used for the API request
            return [{"KoName": "이규보", "Link": "https://people.aks.ac.kr/front/dirSrv/dirCon.aks?...",
                     "YearBirth": 1168}]

        r = ea.fetch_authority("aks_digerati", "koreanPerson_18816", fetcher=fake)
        self.assertEqual(r["status"], "ok")
        # citable url is the API canonical_link, NOT a raw koreanPerson_ url
        self.assertTrue(r["url"].startswith("https://people.aks.ac.kr/"))
        self.assertNotIn("koreanPerson", r["url"])

    def test_unfetchable_source_rejected(self):
        r = ea.fetch_authority("aks_ency", "12345", fetcher=lambda u: {"x": 1})
        self.assertEqual(r["status"], "error")

    def test_link_only_reference(self):
        ref = ea.link_only_reference("aks_ency", "E0012345")
        self.assertIsNotNone(ref)
        self.assertFalse(ref["fetchable"])
        self.assertIn("E0012345", ref["url"])
        self.assertIsNone(ea.link_only_reference("aks_ency", ""))  # no id -> no link


class TestVectorNormalization(unittest.TestCase):
    def test_verbatim_text_and_entities(self):
        doc = {
            "page_content": "matched",
            "metadata": {
                "entry_id": "E003", "entry_position": 3,
                "source_work_kor": "지봉유설", "source_work_id": "B016",
                "original_chinese": "兩兩佳人弄夕暉。", "korean_translation": "쌍쌍의 가인",
                "creator": "이수광", "creator_id": "P027",
                "creator_external_ids": {"wikidata": "Q1", "aks_digerati": "koreanPerson_1"},
                "mentioned_persons": [{"id": "P99", "nameKor": "허균", "wikidata": "Q9"}],
                "audiences": [{"id": "P50", "nameKor": "권필", "aks_digerati": "koreanPerson_5"}],
                "poetrytalks_link": "https://poetrytalks.org/E003",
            },
        }
        ev = docs_to_evidence([doc])
        d = ev.documents[0]
        self.assertEqual(d["textChi"], "兩兩佳人弄夕暉。")  # verbatim, unaltered
        ids = {(e.node_id, e.wikidata_id, e.aks_digerati_id) for e in ev.entities}
        self.assertIn(("P027", "Q1", "koreanPerson_1"), ids)   # creator
        self.assertIn(("P99", "Q9", None), ids)                # mentioned
        self.assertIn(("P50", None, "koreanPerson_5"), ids)    # audience (both-id support)


class TestGraphNormalization(unittest.TestCase):
    def test_standardized_person_fields_extracted(self):
        rows = [{
            "person_id": "P027", "person_name_kor": "이수광",
            "wikidata_id": "Q1", "aks_digerati_id": "koreanPerson_1",
            "poem_id": "M012", "work_id": "B016",
        }]
        ev = graph_rows_to_evidence(rows, cypher="MATCH ...")
        self.assertEqual(len(ev.entities), 1)
        self.assertEqual(ev.entities[0].wikidata_id, "Q1")
        self.assertTrue(ev.documents)

    def test_graphrag_question_smoke_no_error(self):
        # Criterion 10 — the previously KeyError-prone question completes.
        g = graph_rows_to_evidence(
            [{"person_name_kor": "황진이", "poem_id": "M100", "work_id": "B016"}],
            cypher="MATCH ...",
        )
        f = RecordingFetcher()
        r = gather_graphrag_evidence(
            "황진이는 어떤 시를 썼나요?", "ko",
            graph_retriever=lambda q, l: g,
            vector_retriever=lambda q, l: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        self.assertEqual(f.calls, [])           # poem-list -> no external
        self.assertTrue(r["graph"].provenance)  # provenance preserved


class TestEntityMerge(unittest.TestCase):
    def test_merge_by_shared_authority_id(self):
        a = person(node_id=None, wikidata="Q1")
        b = person(node_id="P1", aks="koreanPerson_1", name="이규보")
        c = person(node_id="P1", wikidata="Q1")  # links a and b via P1/Q1
        merged = merge_entities([a, b, c])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].node_id, "P1")
        self.assertEqual(merged[0].wikidata_id, "Q1")
        self.assertEqual(merged[0].aks_digerati_id, "koreanPerson_1")

    def test_distinct_people_not_merged(self):
        a = person(node_id="P1", name="이규보")
        b = person(node_id="P2", name="이규보")  # same name, different node
        self.assertEqual(len(merge_entities([a, b])), 2)


class TestCypherTemplateBraces(unittest.TestCase):
    def test_no_unintended_template_variables(self):
        # Criterion 9 — literal Cypher/map braces must not become LC variables.
        from langchain_core.prompts import PromptTemplate

        template = _extract_module_string("tools/cypher.py", "CYPHER_GENERATION_TEMPLATE")
        self.assertIsNotNone(template)
        pt = PromptTemplate.from_template(template)
        self.assertEqual(set(pt.input_variables), {"schema", "question"})


def _extract_module_string(rel_path, var_name):
    """Read a module-level string constant via AST, without importing the module
    (which would pull in streamlit/neo4j)."""
    path = os.path.join(os.path.dirname(__file__), "..", rel_path)
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


if __name__ == "__main__":
    unittest.main()

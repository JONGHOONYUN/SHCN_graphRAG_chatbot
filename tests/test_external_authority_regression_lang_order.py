"""work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §5.4 —
Phase 5 regression-only verification: confirms tools/orchestrator.py and
tools/external_authority.py behave exactly as before this work order's
Phase 1-4 changes (which never touched either file). No live HTTP, Gemini,
or Neo4j connection — every retriever/fetcher is injected.
"""

import unittest

from tools.evidence import Entity, Evidence
from tools.orchestrator import authority_intent, gather_graphrag_evidence


def person(node_id, name_kor="", name_eng="", **ids):
    return Entity(node_id=node_id, node_type="Person", name_kor=name_kor,
                  name_eng=name_eng, authority_ids={k: v for k, v in ids.items() if v})


class RecordingFetcher:
    def __init__(self, status="ok"):
        self.calls = []
        self.status = status

    def __call__(self, source, ext_id, language, node_type="Person"):
        self.calls.append((source, ext_id, node_type))
        if self.status != "ok":
            return {"source": source, "id": ext_id, "status": self.status,
                    "node_type": node_type, "error": "unavailable"}
        return {"source": source, "id": ext_id, "status": "ok", "node_type": node_type,
                "url": f"http://example/{ext_id}", "data": {"primary_name": "杜甫"}}


REPRO_QUESTION = "How is Du Fu critiqued in Sihwa Ch'ongnim?"


class TestReproductionQuestionZeroExternalFetch(unittest.TestCase):
    """§5.4 item 1: the exact reproduction question makes zero external
    calls — the critique-relation question is not a biography/location/
    external-source intent, so the authority gate stays off (work order §2.3
    'authority가 꺼짐 — 수정 금지', §5.4 explicit assertion)."""

    def test_repro_question_makes_no_external_call(self):
        f = RecordingFetcher()
        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", wikidata="Q184226"),
            person("P017", name_kor="허균", name_eng="Hŏ Kyun", wikidata="Q123456"),
        ])
        r = gather_graphrag_evidence(
            REPRO_QUESTION, "en",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        self.assertEqual(f.calls, [])
        self.assertFalse(r["authority_attempted"])

    def test_intent_gate_is_off_for_repro_question(self):
        intent = authority_intent(REPRO_QUESTION)
        self.assertFalse(intent["Person"])
        self.assertFalse(intent["Place"])


class TestExplicitBiographyQuestionStillFetchesValidIdsOnly(unittest.TestCase):
    """§5.4 item 2: an explicit biography/location/external question still
    goes through the registry for graph-stored, valid authority ids only —
    unchanged from before this work order."""

    def test_explicit_biography_question_fetches_registered_ids(self):
        f = RecordingFetcher()
        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", wikidata="Q184226"),
        ])
        gather_graphrag_evidence(
            "두보의 생몰년을 알려줘", "ko",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        self.assertEqual(f.calls, [("wikidata", "Q184226", "Person")])

    def test_no_id_no_fetch(self):
        f = RecordingFetcher()
        g = Evidence(kind="graph", entities=[
            person("P999", name_kor="무명씨"),   # no authority id at all
        ])
        gather_graphrag_evidence(
            "이 인물의 생애를 자세히 알려줘", "ko",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        self.assertEqual(f.calls, [])


class TestStatusAndCitationOnlyOkOrLinkOnly(unittest.TestCase):
    """§5.4 items 3-4: only status=ok/link_only + a URL reach the final
    Sources; unavailable/error/unsupported never become citations or facts."""

    def test_ok_status_produces_one_external_citation(self):
        from tools.synthesis import build_citations

        f = RecordingFetcher(status="ok")
        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", wikidata="Q184226"),
        ])
        r = gather_graphrag_evidence(
            "두보의 생몰년을 알려줘", "ko",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        citations = build_citations(r, "ko")
        wikidata_lines = [c for c in citations if "wikidata" in c.lower()
                         and "poetrytalks" not in c]
        self.assertEqual(len(wikidata_lines), 1)

    def test_link_only_status_produces_reference_link_not_a_fact(self):
        from tools.synthesis import format_evidence_for_prompt

        def link_only_fetcher(source, ext_id, language, node_type="Person"):
            return {"source": source, "id": ext_id, "status": "link_only",
                    "node_type": node_type, "url": f"http://example/{ext_id}"}

        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", aks_ency="E0063034"),
        ])
        r = gather_graphrag_evidence(
            "두보의 생몰년을 알려줘", "ko",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=link_only_fetcher,
        )
        out = format_evidence_for_prompt(r, "ko")
        self.assertIn("LINK-ONLY", out)
        self.assertIn("do NOT assert its", out)

    def test_unavailable_status_produces_no_citation_and_no_asserted_fact(self):
        from tools.synthesis import build_citations, format_evidence_for_prompt

        f = RecordingFetcher(status="unavailable")
        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", wikidata="Q184226"),
        ])
        r = gather_graphrag_evidence(
            "두보의 생몰년을 알려줘", "ko",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        citations = build_citations(r, "ko")
        self.assertFalse(any("Q184226" in c for c in citations))
        out = format_evidence_for_prompt(r, "ko")
        self.assertIn("UNAVAILABLE", out)
        self.assertIn("do not use pretraining", out)


if __name__ == "__main__":
    unittest.main()

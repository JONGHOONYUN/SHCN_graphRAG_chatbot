"""graph-ranking-reliability work order §5.3 — orchestrator-level ranking
routing: the deterministic template bypasses free-form Cypher generation,
and authority enrichment stays at 0 fetches unless the user explicitly asks
about the winner (and then only the winner, never the full candidate pool).

Runs with the stdlib unittest runner (no pytest). Neo4j, the LLM, and HTTP
are never touched — every retriever/fetcher is injected.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.evidence import Entity, Evidence  # noqa: E402
from tools.orchestrator import gather_graphrag_evidence  # noqa: E402


def person(node_id, name_kor="", name_eng="", **ids):
    return Entity(node_id=node_id, node_type="Person", name_kor=name_kor,
                  name_eng=name_eng,
                  authority_ids={k: v for k, v in ids.items() if v})


class RecordingFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, source, ext_id, language, node_type="Person"):
        self.calls.append((source, ext_id, node_type))
        return {"source": source, "id": ext_id, "status": "ok",
                "node_type": node_type, "url": f"http://example/{ext_id}",
                "data": {}}


def _king_ranking_evidence():
    """A stand-in for `tools.cypher.retrieve_role_ranking_evidence('king')`
    — 2 candidates, P519 wins outright, matching the live-verified shape."""
    ev = Evidence(kind="graph")
    ev.entities = [
        person("P519", name_kor="선조", name_eng="King Sŏnjo", wikidata="Q484359"),
        person("P513", name_kor="성종", name_eng="King Sŏngjong", wikidata="Q484006"),
    ]
    ev.documents = [
        {"person_id": "P519", "mention_count": 10},
        {"person_id": "P513", "mention_count": 6},
    ]
    ev.claims = [{
        "type": "ranking", "role": "king", "unit": "entry",
        "winner_person_ids": ["P519"], "winner_count": 10,
    }]
    return ev


def _explode_graph_retriever(question, language, history_text=None):
    raise AssertionError(
        "free-form graph_retriever must never be called for a detected "
        "ranking/aggregation question — the deterministic template must "
        "be used instead")


def _irrelevant_vector_evidence():
    # A vector hit surfacing an UNRELATED person — must never be treated as
    # a ranking candidate or reach authority enrichment for a ranking answer.
    ev = Evidence(kind="vector")
    ev.entities = [person("P999", name_kor="무관한인물", wikidata="Q999999")]
    return ev


class TestRankingBypassesFreeFormCypher(unittest.TestCase):
    def test_ranking_question_never_calls_graph_retriever(self):
        r = gather_graphrag_evidence(
            "Who is the most mentioned king in Sihwa ch'ongnim?", "en",
            graph_retriever=_explode_graph_retriever,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            role_ranking_retriever=lambda role: _king_ranking_evidence(),
            authority_fetcher=RecordingFetcher(),
        )
        self.assertEqual(r["ranking_role"], "king")
        self.assertEqual(len(r["graph"].documents), 2)

    def test_korean_equivalent_question_routes_the_same_way(self):
        r = gather_graphrag_evidence(
            "시화총림에서 가장 많이 언급된 왕은 누구인가요?", "ko",
            graph_retriever=_explode_graph_retriever,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            role_ranking_retriever=lambda role: _king_ranking_evidence(),
            authority_fetcher=RecordingFetcher(),
        )
        self.assertEqual(r["ranking_role"], "king")

    def test_non_ranking_question_still_uses_graph_retriever(self):
        called = {"hit": False}

        def graph_retriever(q, l, h=None):
            called["hit"] = True
            return Evidence(kind="graph")

        r = gather_graphrag_evidence(
            "이규보의 생몰년은?", "ko",
            graph_retriever=graph_retriever,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=RecordingFetcher(),
        )
        self.assertIsNone(r["ranking_role"])
        self.assertTrue(called["hit"])


class TestRankingAuthorityGate(unittest.TestCase):
    """Work order §3-P0-D item 3's three named scenarios, verified exactly."""

    def _run(self, question, fetcher):
        return gather_graphrag_evidence(
            question, "en",
            graph_retriever=_explode_graph_retriever,
            vector_retriever=lambda q, l, h=None: _irrelevant_vector_evidence(),
            role_ranking_retriever=lambda role: _king_ranking_evidence(),
            authority_fetcher=fetcher,
        )

    def test_bare_ranking_question_zero_authority_fetch(self):
        f = RecordingFetcher()
        r = self._run("Who is the most mentioned king in Sihwa ch'ongnim?", f)
        self.assertEqual(f.calls, [])
        self.assertFalse(r["authority_attempted"])
        self.assertEqual(r["coverage"], {})

    def test_explicit_wikidata_followup_enriches_winner_only(self):
        f = RecordingFetcher()
        r = self._run(
            "Who is the most mentioned king, and what does Wikidata say "
            "about that person?", f)
        self.assertEqual(f.calls, [("wikidata", "Q484359", "Person")])
        self.assertTrue(r["authority_attempted"])
        # The runner-up (P513) and the irrelevant vector candidate (P999)
        # must NEVER be enriched, even though both are valid Person entities
        # with fetchable ids in `r["persons"]`.
        fetched_ids = {c[1] for c in f.calls}
        self.assertNotIn("Q484006", fetched_ids)
        self.assertNotIn("Q999999", fetched_ids)

    def test_compare_external_sources_enriches_winner_only(self):
        f = RecordingFetcher()
        r = self._run("Compare external sources for the most mentioned king", f)
        self.assertEqual(f.calls, [("wikidata", "Q484359", "Person")])
        self.assertTrue(r["authority_attempted"])

    def test_generic_who_is_alone_does_not_opt_in(self):
        # "who is" is a normal `_PERSON_CUES` biography trigger, but for a
        # detected ranking question it must be ignored — only an
        # unambiguous external-source cue opts in (see
        # tools.orchestrator._ranking_authority_intent).
        f = RecordingFetcher()
        r = self._run("Who is the most mentioned king in Sihwa ch'ongnim?", f)
        self.assertEqual(f.calls, [])

    def test_ranking_failure_yields_zero_fetch_even_with_explicit_cue(self):
        def failed_ranking(role):
            ev = Evidence(kind="graph")
            ev.claims.append({"type": "status", "outcome": "no_results"})
            return ev

        f = RecordingFetcher()
        r = gather_graphrag_evidence(
            "Who is the most mentioned king, and what does Wikidata say "
            "about that person?", "en",
            graph_retriever=_explode_graph_retriever,
            vector_retriever=lambda q, l, h=None: _irrelevant_vector_evidence(),
            role_ranking_retriever=failed_ranking,
            authority_fetcher=f,
        )
        self.assertEqual(f.calls, [])
        self.assertEqual(r["statuses"]["graph"]["outcome"], "no_results")

    def test_want_authority_false_overrides_even_an_explicit_cue(self):
        f = RecordingFetcher()
        r = gather_graphrag_evidence(
            "Who is the most mentioned king, and what does Wikidata say "
            "about that person?", "en",
            graph_retriever=_explode_graph_retriever,
            vector_retriever=lambda q, l, h=None: _irrelevant_vector_evidence(),
            role_ranking_retriever=lambda role: _king_ranking_evidence(),
            authority_fetcher=f,
            want_authority=False,
        )
        self.assertEqual(f.calls, [])


class TestRankingTieYieldsMultipleWinners(unittest.TestCase):
    def test_tied_winners_are_all_eligible_for_enrichment(self):
        def tied_ranking(role):
            ev = Evidence(kind="graph")
            ev.entities = [
                person("P1", name_kor="갑", wikidata="Q1"),
                person("P2", name_kor="을", wikidata="Q2"),
            ]
            ev.documents = [
                {"person_id": "P1", "mention_count": 5},
                {"person_id": "P2", "mention_count": 5},
            ]
            ev.claims = [{
                "type": "ranking", "role": "king", "unit": "entry",
                "winner_person_ids": ["P1", "P2"], "winner_count": 5,
            }]
            return ev

        f = RecordingFetcher()
        r = gather_graphrag_evidence(
            "Who is the most mentioned king, and what does Wikidata say "
            "about that person?", "en",
            graph_retriever=_explode_graph_retriever,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            role_ranking_retriever=tied_ranking,
            authority_fetcher=f,
        )
        fetched = {c[1] for c in f.calls}
        self.assertEqual(fetched, {"Q1", "Q2"})
        self.assertTrue(r["authority_attempted"])


if __name__ == "__main__":
    unittest.main()

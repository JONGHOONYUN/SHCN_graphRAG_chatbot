"""work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §5.2 —
named "poetrytalks wikidata" citations: exact per-language format, legacy
Cypher alias normalization (subject_name_*/critic_name_*/critical_term_*),
identity/safety invariants (never merge distinct internal ids, never treat
an external authority id as a Poetry Talks node id), and ID-only fallback
when no name is available. No live Gemini/Neo4j/network anywhere here.
"""

import unittest

from tools.evidence import (
    Entity,
    Evidence,
    entities_from_graph_row,
    node_references_from_graph_row,
)
from tools.synthesis import _format_citation_name, build_citations

# The exact reproduction fixture from work order §5.2.
REPRO_ROW = {
    "subject_person_id": "P094",
    "subject_name_kor": "두보",
    "subject_name_eng": "Du Fu",
    "critic_person_id": "P017",
    "critic_name_kor": "허균",
    "critic_name_eng": "Hŏ Kyun",
    "critical_term_id": "CT004",
    "critical_term_kor": "고고",
    "critical_term_eng": "lofty and ancient",
}


def _evidence_for_row(row):
    ev = Evidence(kind="graph")
    ev.entities = entities_from_graph_row(row)
    ev.node_references = node_references_from_graph_row(row)
    return {"graph": ev, "vector": Evidence(kind="vector"), "external": Evidence(kind="external")}


class TestLegacyAliasNormalization(unittest.TestCase):
    """work order §2.2/§4 Phase 2: the ACTUAL alias shapes generated Cypher
    produced (subject_name_*/critic_name_*/critical_term_kor|eng, without the
    standardized <role>_name_* infix) must resolve to the CORRECT entity's
    own name — never another role's name, never dropped to ID-only."""

    def test_subject_and_critic_never_cross_contaminate(self):
        entities = {e.node_id: e for e in entities_from_graph_row(REPRO_ROW)}
        self.assertEqual(entities["P094"].name_kor, "두보")
        self.assertEqual(entities["P094"].name_eng, "Du Fu")
        self.assertEqual(entities["P017"].name_kor, "허균")
        self.assertEqual(entities["P017"].name_eng, "Hŏ Kyun")
        # Cross-contamination guard: P094 must NEVER carry P017's name.
        self.assertNotEqual(entities["P094"].name_kor, entities["P017"].name_kor)

    def test_critical_term_legacy_alias_without_name_infix(self):
        refs = {r.node_id: r for r in node_references_from_graph_row(REPRO_ROW)}
        self.assertEqual(refs["CT004"].node_type, "CriticalTerm")
        self.assertEqual(refs["CT004"].name_kor, "고고")
        self.assertEqual(refs["CT004"].name_eng, "lofty and ancient")

    def test_standard_alias_form_still_works(self):
        row = {
            "subject_person_id": "P094",
            "subject_person_name_kor": "두보",
            "subject_person_name_eng": "Du Fu",
        }
        entities = {e.node_id: e for e in entities_from_graph_row(row)}
        self.assertEqual(entities["P094"].name_kor, "두보")
        self.assertEqual(entities["P094"].name_eng, "Du Fu")

    def test_bare_nested_map_camelcase_form_still_works(self):
        row = {"critical_term_id": "CT004", "nameKor": "고고", "nameEng": "lofty and ancient"}
        refs = {r.node_id: r for r in node_references_from_graph_row(row)}
        self.assertEqual(refs["CT004"].name_kor, "고고")
        self.assertEqual(refs["CT004"].name_eng, "lofty and ancient")

    def test_no_broad_fallback_across_unrelated_roles(self):
        # subject_person_id must NEVER read critic_name_eng even if present
        # and subject_name_eng happens to be absent.
        row = {
            "subject_person_id": "P094",
            "critic_person_id": "P017",
            "critic_name_kor": "허균", "critic_name_eng": "Hŏ Kyun",
        }
        entities = {e.node_id: e for e in entities_from_graph_row(row)}
        self.assertIsNone(entities["P094"].name_kor)
        self.assertIsNone(entities["P094"].name_eng)


class TestFormatCitationName(unittest.TestCase):
    def test_en_exact_format(self):
        self.assertEqual(_format_citation_name("두보", "Du Fu", None, "en"),
                         " — Du Fu (두보)")

    def test_ko_exact_format(self):
        self.assertEqual(_format_citation_name("두보", "Du Fu", None, "ko"),
                         " — 두보 (Du Fu)")

    def test_only_eng_present(self):
        self.assertEqual(_format_citation_name(None, "Du Fu", None, "en"), " — Du Fu")

    def test_only_kor_present(self):
        self.assertEqual(_format_citation_name("두보", None, None, "ko"), " — 두보")

    def test_only_kor_present_for_en_response_shows_kor_alone(self):
        self.assertEqual(_format_citation_name("두보", None, None, "en"), " — 두보")

    def test_equal_values_shown_once(self):
        self.assertEqual(_format_citation_name("Du Fu", "Du Fu", None, "en"), " — Du Fu")

    def test_both_missing_returns_empty(self):
        self.assertEqual(_format_citation_name(None, None, None, "en"), "")
        self.assertEqual(_format_citation_name("", "  ", None, "ko"), "")

    def test_name_mr_or_chi_never_substitutes_for_missing_eng_in_en(self):
        # en/ko must never fall back to nameChi — only zh may prioritize it.
        self.assertEqual(_format_citation_name(None, None, "杜甫", "en"), "")
        self.assertEqual(_format_citation_name(None, None, "杜甫", "ko"), "")

    def test_zh_prefers_chi_but_preserves_kor_or_eng(self):
        out = _format_citation_name("두보", "Du Fu", "杜甫", "zh")
        self.assertTrue(out.startswith(" — 杜甫"))
        self.assertTrue("두보" in out or "Du Fu" in out)

    def test_zh_without_chi_falls_back_to_kor_eng_pair(self):
        self.assertEqual(_format_citation_name("두보", "Du Fu", None, "zh"),
                         " — 두보 (Du Fu)")


class TestBuildCitationsNamedBullets(unittest.TestCase):
    def test_english_exact_output(self):
        evidence = _evidence_for_row(REPRO_ROW)
        citations = build_citations(evidence, "en")
        self.assertIn(
            "- poetrytalks wikidata: [P094](https://poetrytalks.org/P094) — Du Fu (두보)",
            citations)

    def test_korean_exact_output(self):
        evidence = _evidence_for_row(REPRO_ROW)
        citations = build_citations(evidence, "ko")
        self.assertIn(
            "- poetrytalks wikidata: [P094](https://poetrytalks.org/P094) — 두보 (Du Fu)",
            citations)

    def test_critical_term_named_in_both_languages(self):
        evidence = _evidence_for_row(REPRO_ROW)
        en = build_citations(evidence, "en")
        ko = build_citations(evidence, "ko")
        self.assertTrue(any("CT004" in c and "lofty and ancient (고고)" in c for c in en))
        self.assertTrue(any("CT004" in c and "고고 (lofty and ancient)" in c for c in ko))

    def test_substring_backward_compatibility_id_link_still_present(self):
        # Existing tests match on the bare "[id](url)" substring — the name
        # suffix must be an ADDITION, never a replacement.
        evidence = _evidence_for_row(REPRO_ROW)
        citations = build_citations(evidence, "en")
        self.assertTrue(any("[P094](https://poetrytalks.org/P094)" in c for c in citations))

    def test_unnamed_node_falls_back_to_id_only(self):
        ev = Evidence(kind="graph")
        ev.node_references = node_references_from_graph_row({"work_id": "B001"})
        evidence = {"graph": ev, "vector": Evidence(kind="vector"),
                   "external": Evidence(kind="external")}
        citations = build_citations(evidence, "en")
        self.assertIn("- poetrytalks wikidata: [B001](https://poetrytalks.org/B001)", citations)

    def test_distinct_internal_ids_never_merge_even_with_shared_external_id(self):
        # P553 and P1227 share an external Wikidata id but are different
        # graph nodes — each keeps its own bullet, never merged/summed.
        e1 = Entity(node_id="P553", node_type="Person", name_kor="허초희",
                   name_eng="Hŏ Ch'ohŭi", authority_ids={"wikidata": "Q464558"})
        e2 = Entity(node_id="P1227", node_type="Person", name_kor="허난설헌",
                   name_eng="Hŏ Nansŏrhŏn", authority_ids={"wikidata": "Q464558"})
        graph_ev = Evidence(kind="graph", entities=[e1, e2])
        from tools.evidence import make_node_reference
        graph_ev.node_references = [
            make_node_reference("P553", source_type="neo4j_graph",
                               name_kor="허초희", name_eng="Hŏ Ch'ohŭi"),
            make_node_reference("P1227", source_type="neo4j_graph",
                               name_kor="허난설헌", name_eng="Hŏ Nansŏrhŏn"),
        ]
        evidence = {"graph": graph_ev, "vector": Evidence(kind="vector"),
                   "external": Evidence(kind="external")}
        citations = build_citations(evidence, "en")
        p553 = [c for c in citations if "P553" in c]
        p1227 = [c for c in citations if "P1227" in c]
        self.assertEqual(len(p553), 1)
        self.assertEqual(len(p1227), 1)
        self.assertIn("Hŏ Ch'ohŭi", p553[0])
        self.assertIn("Hŏ Nansŏrhŏn", p1227[0])
        self.assertNotEqual(p553[0], p1227[0])

    def test_different_internal_ids_sharing_a_name_are_not_merged(self):
        from tools.evidence import make_node_reference
        graph_ev = Evidence(kind="graph")
        graph_ev.node_references = [
            make_node_reference("P100", source_type="neo4j_graph",
                               name_kor="김철수", name_eng="Kim Ch'ŏlsu"),
            make_node_reference("P200", source_type="neo4j_graph",
                               name_kor="김철수", name_eng="Kim Ch'ŏlsu"),
        ]
        evidence = {"graph": graph_ev, "vector": Evidence(kind="vector"),
                   "external": Evidence(kind="external")}
        citations = build_citations(evidence, "en")
        p100 = [c for c in citations if "P100" in c]
        p200 = [c for c in citations if "P200" in c]
        self.assertEqual(len(p100), 1)
        self.assertEqual(len(p200), 1)

    def test_external_authority_ids_never_appear_as_poetrytalks_ids(self):
        e = Entity(node_id="P094", node_type="Person", name_kor="두보",
                  name_eng="Du Fu",
                  authority_ids={"wikidata": "Q464558", "aks_digerati": "koreanPerson_1",
                                "aks_ency": "E0063034"})
        graph_ev = Evidence(kind="graph", entities=[e])
        from tools.evidence import make_node_reference
        graph_ev.node_references = [make_node_reference(
            "P094", source_type="neo4j_graph", name_kor="두보", name_eng="Du Fu")]
        evidence = {"graph": graph_ev, "vector": Evidence(kind="vector"),
                   "external": Evidence(kind="external")}
        citations = build_citations(evidence, "en")
        joined = "\n".join(citations)
        self.assertNotIn("Q464558", joined)
        self.assertNotIn("koreanPerson_1", joined)
        self.assertNotIn("E0063034", joined)

    def test_graph_id_only_merges_with_vector_named_reference(self):
        from tools.evidence import make_node_reference
        graph_ev = Evidence(kind="graph")
        graph_ev.node_references = [
            make_node_reference("P094", source_type="neo4j_graph"),   # id-only
        ]
        vector_ev = Evidence(kind="vector")
        vector_ev.node_references = [
            make_node_reference("P094", source_type="neo4j_vector",
                               name_kor="두보", name_eng="Du Fu"),
        ]
        evidence = {"graph": graph_ev, "vector": vector_ev, "external": Evidence(kind="external")}
        citations = build_citations(evidence, "en")
        p094 = [c for c in citations if "P094" in c]
        self.assertEqual(len(p094), 1)
        self.assertIn("Du Fu", p094[0])

    def test_referenced_node_ids_filtering_still_narrows_group(self):
        evidence = _evidence_for_row(REPRO_ROW)
        citations = build_citations(evidence, "en", referenced_node_ids=["P094"])
        joined = "\n".join(citations)
        self.assertIn("P094", joined)
        self.assertNotIn("P017", joined)
        self.assertNotIn("CT004", joined)

    def test_work_class_named_citation(self):
        from tools.evidence import make_node_reference
        graph_ev = Evidence(kind="graph")
        graph_ev.node_references = [make_node_reference(
            "B009", source_type="neo4j_graph", name_kor="패관잡기", name_eng="Paegwan Chapki")]
        evidence = {"graph": graph_ev, "vector": Evidence(kind="vector"),
                   "external": Evidence(kind="external")}
        citations = build_citations(evidence, "en")
        self.assertIn(
            "- poetrytalks wikidata: [B009](https://poetrytalks.org/B009) — Paegwan Chapki (패관잡기)",
            citations)


if __name__ == "__main__":
    unittest.main()

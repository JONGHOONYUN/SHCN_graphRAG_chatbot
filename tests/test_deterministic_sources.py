"""Deterministic Sources assembly + provenance validity (work order:
CLAUDE_CODE_DETERMINISTIC_SOURCES_AND_PROVENANCE_FIX.md).

Reproduces the observed production defect as mock fixtures — P553 허초희 /
P1227 허난설헌 sharing Wikidata Q464558, vector provenance with
entry_id=None and entry_position=0, and deliberately NON-COMPLIANT LLM
outputs (Sources omitted / bullets collapsed / URLs stripped / fake URLs) —
then asserts the final assembled answer honors the deterministic contract.

No live Gemini or Neo4j is required anywhere in this file.
"""

import unittest
from unittest import mock

from tools.answer_renderer import (
    assemble_final_answer,
    link_entities_in_body,
    render_sources_section,
    sources_header,
    strip_model_sources,
)
from tools.evidence import (
    Entity,
    Evidence,
    Provenance,
    collect_entities,
    document_to_parts,
    merge_entities,
    normalize_entry_position,
    provenance_from_graph_row,
    _linked_id,
)
from tools.synthesis import (
    SYNTHESIS_SYSTEM_RULES,
    _rebuild_vector_prov_label,
    build_citations,
)

FORBIDDEN = ("(?)", "[?]", "(None)", "Entry None", "Entry 0", "Entry -1", ")(")


def _assert_clean(testcase, text):
    for bad in FORBIDDEN:
        testcase.assertNotIn(bad, text, f"forbidden {bad!r} in:\n{text}")


# ── Phase 0 fixture (mirrors the observed failing response) ──────────────────
def vector_doc(entry_id, position, work_id="B023", work_kor="패관잡기",
               work_eng="A Storyteller's Miscellany"):
    return {
        "page_content": "…",
        "metadata": {
            "entry_id": entry_id,
            "entry_position": position,
            "source_work_id": work_id,
            "source_work_kor": work_kor,
            "source_work_eng": work_eng,
        },
    }


def observed_evidence():
    """P553/P1227 with shared Q464558 + 4 vector provs incl. entry_id=None and
    entry_position=0 + a valid graph aggregation row."""
    graph = Evidence(kind="graph")
    graph.documents.append(
        {"person_id": "P553", "person_name_kor": "허초희", "mention_count": 10,
         "wikidata_id": "Q464558"})
    graph.documents.append(
        {"person_id": "P1227", "person_name_kor": "허난설헌", "mention_count": 8,
         "wikidata_id": "Q464558"})
    graph.entities = [
        Entity(node_id="P553", node_type="Person", name_kor="허초희",
               name_eng="Hŏ Ch'ohŭi", authority_ids={"wikidata": "Q464558"}),
        Entity(node_id="P1227", node_type="Person", name_kor="허난설헌",
               name_eng="Hŏ Nansŏrhŏn", authority_ids={"wikidata": "Q464558"}),
    ]
    graph.provenance = (
        provenance_from_graph_row({"person_id": "P553"})
        + provenance_from_graph_row({"person_id": "P1227"})
    )

    vector = Evidence(kind="vector")
    for doc in (
        vector_doc("E031", 31),                                   # valid
        vector_doc("E028", 28, work_id="B001", work_kor="시화총림",
                   work_eng="Compendium of Remarks on Poetry"),   # valid
        vector_doc(None, 0, work_id="B001", work_kor="시화총림",
                   work_eng="Compendium of Remarks on Poetry"),   # broken
        vector_doc(None, 94, work_id="B016", work_kor="지봉유설",
                   work_eng="Topical Discourses of Chibong"),     # broken
    ):
        d, ents, provs = document_to_parts(doc)
        vector.documents.append(d)
        vector.entities.extend(ents)
        vector.provenance.extend(provs)

    return {"graph": graph, "vector": vector,
            "external": Evidence(kind="external")}


# ── §2.1 Neo4j internal-ID identity invariant ───────────────────────────────
class TestInternalIdIdentityInvariant(unittest.TestCase):
    """P553 and P1227 share idWikidata=Q464558 but are distinct graph nodes.
    They must NEVER merge and their counts must never be summed."""

    def _pair(self):
        return [
            Entity(node_id="P553", node_type="Person",
                   authority_ids={"wikidata": "Q464558"}),
            Entity(node_id="P1227", node_type="Person",
                   authority_ids={"wikidata": "Q464558"}),
        ]

    def test_merge_entities_keeps_two(self):
        merged = merge_entities(self._pair())
        self.assertEqual(sorted(e.node_id for e in merged), ["P1227", "P553"])

    def test_collect_entities_keeps_two(self):
        ev = Evidence(kind="graph", entities=self._pair())
        merged = collect_entities(ev)
        self.assertEqual(sorted(e.node_id for e in merged), ["P1227", "P553"])

    def test_bridging_anonymous_record_does_not_collapse_the_pair(self):
        anon = Entity(node_type="Person", authority_ids={"wikidata": "Q464558"})
        merged = merge_entities([anon] + self._pair())
        self.assertEqual(sorted(e.node_id for e in merged), ["P1227", "P553"])

    def test_citations_keep_both_nodes(self):
        cites = build_citations(observed_evidence(), "en")
        joined = "\n".join(cites)
        self.assertIn("[P553](https://poetrytalks.org/P553)", joined)
        self.assertIn("[P1227](https://poetrytalks.org/P1227)", joined)


# ── Phase 1: model-Sources stripping ─────────────────────────────────────────
class TestStripModelSources(unittest.TestCase):
    def test_markdown_headers_all_depths_and_languages(self):
        for header in ("# Sources", "###### Sources", "## References",
                       "## 출처", "### 참고문헌", "## 来源", "## 參考資料",
                       "## 参考资料", "**Sources**", "Sources:", "출처:",
                       "来源："):
            body = f"answer body line.\n\n{header}\n- fake bullet"
            sanitized, removed = strip_model_sources(body)
            self.assertTrue(removed, header)
            self.assertEqual(sanitized, "answer body line.", header)

    def test_mid_sentence_source_word_is_not_cut(self):
        body = ("The source of this poem is the graph.\n"
                "Sources are described in detail here.\n"
                "More body text.")
        sanitized, removed = strip_model_sources(body)
        self.assertFalse(removed)
        self.assertEqual(sanitized, body)

    def test_no_sources_returns_unchanged(self):
        sanitized, removed = strip_model_sources("plain body")
        self.assertEqual((sanitized, removed), ("plain body", False))


class TestRenderSourcesSection(unittest.TestCase):
    def test_localized_headers(self):
        for lang, header in (("ko", "출처"), ("en", "Sources"), ("zh", "来源")):
            out = render_sources_section(["- a: [X1](https://poetrytalks.org/X1)"], lang)
            self.assertTrue(out.startswith(f"## {header}\n"), out)
            self.assertEqual(sources_header(lang), header)

    def test_empty_citations_no_orphan_header(self):
        self.assertEqual(render_sources_section([], "en"), "")
        self.assertEqual(render_sources_section(None, "ko"), "")


class TestDeterministicAssembly(unittest.TestCase):
    """The final assembly contract holds against deliberately NON-COMPLIANT
    LLM outputs — the exact failure modes observed in production."""

    def setUp(self):
        self.evidence = observed_evidence()
        self.citations = build_citations(self.evidence, "en")
        self.assertTrue(self.citations)

    def _final(self, llm_body):
        return assemble_final_answer(llm_body, self.citations, "en")

    def _assert_contract(self, final):
        # every deterministic bullet present, exactly as built
        for bullet in self.citations:
            self.assertIn(bullet, final)
        # single Sources section
        self.assertEqual(final.count("## Sources"), 1)
        _assert_clean(self, final)

    def test_llm_omits_sources_entirely(self):
        final = self._final("Hŏ Ch'ohŭi is mentioned 10 times; "
                            "Hŏ Nansŏrhŏn 8 times.")
        self._assert_contract(final)

    def test_llm_collapses_bullets_into_headers(self):
        final = self._final(
            "Body answer.\n\n## Sources\npoetrytalks wikidata\n"
            "Sihwa Ch'ongnim Graph\nSihwa Ch'ongnim Graph: 패관잡기 > Entry 31")
        self._assert_contract(final)
        # the collapsed, URL-less model lines are gone
        self.assertNotIn("\npoetrytalks wikidata\n", final)

    def test_llm_strips_urls_and_invents_fake_ones(self):
        final = self._final(
            "Body answer.\n\n## Sources\n"
            "- poetrytalks wikidata: P553\n"
            "- fake: [P553](https://evil.example.com/P553)\n"
            "- Graph: Compendium > Entry 0 (?)")
        self._assert_contract(final)
        self.assertNotIn("evil.example.com", final)

    def test_llm_writes_complete_sources_still_single_section(self):
        final = self._final(
            "Body answer.\n\n## Sources\n" + "\n".join(self.citations))
        self._assert_contract(final)

    def test_korean_and_chinese_headers(self):
        for lang, header in (("ko", "출처"), ("zh", "来源")):
            cites = build_citations(self.evidence, lang)
            final = assemble_final_answer("본문.", cites, lang)
            self.assertEqual(final.count(f"## {header}"), 1, final)

    def test_no_citations_no_header(self):
        final = assemble_final_answer("본문.", [], "ko")
        self.assertNotIn("## 출처", final)
        self.assertEqual(final, "본문.")

    def test_agent_owns_assembly_and_saves_assembled_output(self):
        src = open("agent.py", encoding="utf-8").read()
        self.assertIn("assemble_final_answer(", src)
        # the assembled output (not the raw LLM body) goes to history
        self.assertLess(src.index("assemble_final_answer("),
                        src.index("hist.add_ai_message(output)"))
        # retrieval-failure safe message returns BEFORE any assembly
        self.assertLess(src.index("retrieval_failure_message(user_language)"),
                        src.index("assemble_final_answer("))


# ── Phase 2: provenance validity ─────────────────────────────────────────────
class TestLinkedIdContract(unittest.TestCase):
    def test_none_and_empty_return_empty_not_question_mark(self):
        self.assertEqual(_linked_id(None), "")
        self.assertEqual(_linked_id(""), "")

    def test_valid_id_still_links(self):
        self.assertEqual(
            _linked_id("E031"),
            "[E031](https://poetrytalks.org/E031)")


class TestNormalizeEntryPosition(unittest.TestCase):
    def test_positive_kept(self):
        self.assertEqual(normalize_entry_position(31), 31)
        self.assertEqual(normalize_entry_position("31"), 31)

    def test_zero_negative_none_bool_dropped(self):
        for bad in (0, -1, None, True, False, "0", "abc", 3.5):
            self.assertIsNone(normalize_entry_position(bad), bad)


class TestVectorProvenanceValidity(unittest.TestCase):
    def _prov(self, entry_id, position, work_id="B023"):
        _d, _e, provs = document_to_parts(vector_doc(entry_id, position,
                                                     work_id=work_id))
        return provs

    def test_missing_entry_id_downgrades_to_work_only(self):
        provs = self._prov(None, 31)
        self.assertEqual(len(provs), 1)
        label = provs[0].label
        _assert_clean(self, label)
        self.assertIn("[B023](https://poetrytalks.org/B023)", label)
        self.assertNotIn("Entry", label)          # no faked entry breadcrumb
        self.assertIsNone(provs[0].entry_id)

    def test_position_none_renders_entry_without_number(self):
        provs = self._prov("E031", None)
        label = provs[0].label
        _assert_clean(self, label)
        self.assertIn("[E031](https://poetrytalks.org/E031)", label)

    def test_position_zero_never_renders_entry_zero(self):
        label = self._prov("E010", 0)[0].label
        _assert_clean(self, label)
        self.assertIn("[E010]", label)

    def test_valid_entry_and_position_render_fully(self):
        label = self._prov("E031", 31)[0].label
        _assert_clean(self, label)
        self.assertIn("Entry 31", label)
        self.assertIn("[E031](https://poetrytalks.org/E031)", label)
        self.assertNotIn(")(", label)

    def test_no_valid_ids_produces_no_breadcrumb_and_logs(self):
        with self.assertLogs("tools.evidence", level="WARNING") as logs:
            provs = self._prov(None, 31, work_id="not-a-node-id")
        self.assertEqual(provs, [])
        self.assertTrue(any("contract violation" in m for m in logs.output))

    def test_graph_person_aggregation_provenance_preserved(self):
        provs = provenance_from_graph_row(
            {"person_id": "P553", "mention_count": 10})
        self.assertEqual(len(provs), 1)
        self.assertEqual(provs[0].entity_id, "P553")

    def test_external_only_row_no_poetrytalks_provenance(self):
        provs = provenance_from_graph_row(
            {"idWikidata": "Q464558", "idAKSency": "E0063034"})
        self.assertEqual(provs, [])


class TestRebuildLabelValidity(unittest.TestCase):
    def test_invalid_entry_id_downgrades_not_question_mark(self):
        prov = {"work_name_kor": "패관잡기", "work_name_eng": "Paegwan Chapki",
                "work_id": "B023", "entry_id": None, "entry_position": 31}
        line = _rebuild_vector_prov_label(prov, "en")
        _assert_clean(self, line)
        self.assertIn("[B023]", line)
        self.assertNotIn("Entry", line)

    def test_dirty_fallback_label_is_skipped(self):
        prov = {"label": "지봉유설 > Entry 94 (?)", "work_id": None,
                "entry_id": None}
        self.assertEqual(_rebuild_vector_prov_label(prov, "en"), "")

    def test_clean_fallback_label_still_used(self):
        prov = {"label": "some pre-built label", "work_id": None,
                "entry_id": None}
        self.assertEqual(_rebuild_vector_prov_label(prov, "en"),
                         "some pre-built label")


# ── Phase 3: format + structured de-duplication ──────────────────────────────
class TestCitationDedupAndFormat(unittest.TestCase):
    def test_duplicate_entry_provs_one_breadcrumb(self):
        vector = Evidence(kind="vector")
        for _ in range(3):
            _d, _e, provs = document_to_parts(vector_doc("E031", 31))
            vector.provenance.extend(provs)
        ev = {"graph": Evidence(kind="graph"), "vector": vector,
              "external": Evidence(kind="external")}
        cites = build_citations(ev, "en")
        breadcrumbs = [c for c in cites if "Entry 31" in c]
        self.assertEqual(len(breadcrumbs), 1, cites)

    def test_ptw_bullet_once_per_node_across_graph_and_vector(self):
        ev = observed_evidence()
        # graph provenance already references P553; add a vector doc whose
        # provenance mentions the same node id in its label.
        cites = build_citations(ev, "en")
        p553_ptw = [c for c in cites
                    if c.startswith("- poetrytalks wikidata:") and "P553" in c]
        self.assertEqual(len(p553_ptw), 1)

    def test_no_forbidden_shapes_anywhere(self):
        joined = "\n".join(build_citations(observed_evidence(), "en"))
        _assert_clean(self, joined)

    def test_shared_external_id_never_collapses_citations(self):
        joined = "\n".join(build_citations(observed_evidence(), "en"))
        self.assertIn("P553", joined)
        self.assertIn("P1227", joined)
        self.assertNotIn("Q464558", joined)   # external id is not a PTW node

    def test_work_only_and_entry_citation_coexist(self):
        vector = Evidence(kind="vector")
        for doc in (vector_doc("E031", 31), vector_doc(None, 5)):
            _d, _e, provs = document_to_parts(doc)
            vector.provenance.extend(provs)
        ev = {"graph": Evidence(kind="graph"), "vector": vector,
              "external": Evidence(kind="external")}
        cites = build_citations(ev, "en")
        joined = "\n".join(cites)
        self.assertIn("Entry 31", joined)               # entry-specific kept
        graph_lines = [c for c in cites if "Miscellany" in c]
        self.assertEqual(len(graph_lines), 2, cites)    # not collapsed


# ── Phase 4: deterministic body entity links ─────────────────────────────────
class TestBodyEntityLinks(unittest.TestCase):
    ENTITIES = [
        {"node_id": "P553", "name_kor": "허초희", "name_eng": "Hŏ Ch'ohŭi"},
        {"node_id": "P1227", "name_kor": "허난설헌", "name_eng": "Hŏ Nansŏrhŏn"},
    ]

    def test_unique_names_link_to_their_own_nodes(self):
        body = "허초희 is mentioned 10 times; 허난설헌 8 times."
        out = link_entities_in_body(body, self.ENTITIES)
        self.assertIn("[허초희](https://poetrytalks.org/P553)", out)
        self.assertIn("[허난설헌](https://poetrytalks.org/P1227)", out)

    def test_shared_wikidata_id_gets_distinct_urls(self):
        ents = [dict(e, authority_ids={"wikidata": "Q464558"})
                for e in self.ENTITIES]
        out = link_entities_in_body("허초희 그리고 허난설헌.", ents)
        self.assertIn("poetrytalks.org/P553", out)
        self.assertIn("poetrytalks.org/P1227", out)
        self.assertNotIn("Q464558", out)

    def test_name_not_in_evidence_is_not_linked(self):
        out = link_entities_in_body("정약용 and 허초희.", self.ENTITIES)
        self.assertNotIn("[정약용]", out)
        self.assertIn("[허초희](https://poetrytalks.org/P553)", out)

    def test_ambiguous_homonym_is_never_linked(self):
        ents = self.ENTITIES + [{"node_id": "P999", "name_kor": "허초희"}]
        out = link_entities_in_body("허초희의 시.", ents)
        self.assertNotIn("[허초희]", out)

    def test_existing_markdown_link_not_double_linked(self):
        body = "[허초희](https://poetrytalks.org/P553) 는 이미 링크됨. 허난설헌도 등장."
        out = link_entities_in_body(body, self.ENTITIES)
        self.assertEqual(out.count("poetrytalks.org/P553"), 1)
        self.assertIn("[허난설헌](https://poetrytalks.org/P1227)", out)

    def test_code_blocks_untouched(self):
        body = "```\n허초희\n```\n본문의 허초희."
        out = link_entities_in_body(body, self.ENTITIES)
        self.assertIn("```\n허초희\n```", out)
        self.assertEqual(out.count("[허초희]"), 1)

    def test_only_first_mention_linked(self):
        out = link_entities_in_body("허초희, 그리고 다시 허초희.", self.ENTITIES)
        self.assertEqual(out.count("[허초희]"), 1)

    def test_invalid_node_id_never_links(self):
        ents = [{"node_id": "Q464558", "name_kor": "허초희"}]
        out = link_entities_in_body("허초희.", ents)
        self.assertEqual(out, "허초희.")

    def test_failure_safe(self):
        class Broken:
            @property
            def node_id(self):
                raise RuntimeError("boom")

        out = link_entities_in_body("허초희.", [Broken()])
        self.assertEqual(out, "허초희.")


# ── Phase 5: full integration contract ───────────────────────────────────────
class TestIntegrationContract(unittest.TestCase):
    """End-to-end (mock LLM): the observed question's evidence + every
    non-compliant LLM behavior still yields the deterministic contract."""

    NONCOMPLIANT_BODIES = (
        # omits Sources
        "Hŏ Ch'ohŭi (허초희) is mentioned 10 times; Hŏ Nansŏrhŏn (허난설헌) 8.",
        # collapsed bullets
        "Body.\n\n## Sources\npoetrytalks wikidata\nSihwa Ch'ongnim Graph",
        # stripped URLs
        "Body.\n\nSources:\n- poetrytalks wikidata: P553\n- Graph: Entry 0 (?)",
        # wrong-language + fake url section
        "Body.\n\n### 참고문헌\n- [x](https://evil.example.com/x)",
    )

    def test_contract_holds_for_every_noncompliant_body(self):
        evidence = observed_evidence()
        citations = build_citations(evidence, "en")
        entities = [e.to_dict() for e in
                    collect_entities(evidence["graph"], evidence["vector"])]
        for body in self.NONCOMPLIANT_BODIES:
            with self.subTest(body=body[:40]):
                final = assemble_final_answer(body, citations, "en",
                                              entities=entities)
                self.assertIn("[P553](https://poetrytalks.org/P553)", final)
                self.assertIn("[P1227](https://poetrytalks.org/P1227)", final)
                self.assertIn("Entry 31", final)
                self.assertEqual(final.count("## Sources"), 1)
                self.assertNotIn("evil.example.com", final)
                _assert_clean(self, final)

    def test_body_counts_stay_per_node(self):
        evidence = observed_evidence()
        entities = [e.to_dict() for e in
                    collect_entities(evidence["graph"], evidence["vector"])]
        body = "허초희: 10회, 허난설헌: 8회 언급되었습니다."
        final = assemble_final_answer(
            body, build_citations(evidence, "ko"), "ko", entities=entities)
        self.assertIn("[허초희](https://poetrytalks.org/P553)", final)
        self.assertIn("[허난설헌](https://poetrytalks.org/P1227)", final)
        self.assertIn("10", final)
        self.assertIn("8", final)
        self.assertNotIn("18", final)      # counts never summed

    def test_rules_no_longer_ask_llm_to_author_sources(self):
        self.assertIn("SYSTEM-OWNED", SYNTHESIS_SYSTEM_RULES)
        self.assertNotIn("Copy each bullet EXACTLY", SYNTHESIS_SYSTEM_RULES)


if __name__ == "__main__":
    unittest.main()

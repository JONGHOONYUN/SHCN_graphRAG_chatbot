"""graph-ranking-reliability work order §5.5 — McCune-Reischauer (MR)
romanization consistency for Korean-related entities.

`nameMR` is the SOLE authoritative Latin-script field for Korean Person/
Place/Work/Era/Topic entities. `nameRR` (Revised Romanization) must never be
surfaced as a display/name value anywhere in the pipeline. Diacritics
(breve `ŏ`/`ŭ`, apostrophes) must survive verbatim — no ASCII folding, no
re-romanization.

Fixtures use realistic stored values from the live-verified data (see
IMPLEMENTATION_NOTE.md): 이규보 nameMR='Yi Kyubo' vs nameRR='Yi Gyubo' (these
differ — a strong signal if the wrong field ever leaks through), and MR
diacritic examples (Sŏng Hyŏn / Hŏ Ch'ohŭi / Ch'wisŏn) per work order §5.5
item 1.

Only tools.evidence / tools.synthesis / tools.answer_renderer are imported
(pure, no streamlit/neo4j/llm side effects). tools.vector / tools.cypher
source text is checked by reading the file directly (no import), so this
suite runs without live credentials.
"""

import os
import unittest

from tools.evidence import (
    Entity,
    Evidence,
    NodeReference,
    _person_from_flat,
    _place_from_flat,
    make_node_reference,
    merge_entities,
    merge_node_references,
)
from tools.answer_renderer import link_entities_in_body
from tools.synthesis import (
    RETRIEVAL_STATUS_MESSAGES,
    SYNTHESIS_SYSTEM_RULES,
    format_evidence_for_prompt,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


class TestEntityAndNodeReferenceCarryNameMr(unittest.TestCase):
    def test_entity_name_mr_field_and_to_dict(self):
        e = Entity(node_id="P103", node_type="Person",
                   name_kor="성현", name_mr="Sŏng Hyŏn")
        self.assertEqual(e.name_mr, "Sŏng Hyŏn")
        self.assertEqual(e.to_dict()["name_mr"], "Sŏng Hyŏn")

    def test_node_reference_name_mr_via_make_node_reference(self):
        ref = make_node_reference("P1227", source_type="neo4j_vector",
                                   name_kor="허난설헌", name_mr="Hŏ Ch'ohŭi")
        self.assertEqual(ref.name_mr, "Hŏ Ch'ohŭi")

    def test_merge_node_references_preserves_name_mr(self):
        a = make_node_reference("P1227", source_type="neo4j_vector",
                                name_kor="허난설헌")
        b = make_node_reference("P1227", source_type="neo4j_graph",
                                name_mr="Hŏ Ch'ohŭi")
        merged = merge_node_references([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].name_mr, "Hŏ Ch'ohŭi")

    def test_merge_entities_preserves_name_mr(self):
        a = Entity(node_id="P103", node_type="Person", name_kor="성현")
        b = Entity(node_id="P103", node_type="Person", name_mr="Sŏng Hyŏn")
        merged = merge_entities([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].name_mr, "Sŏng Hyŏn")

    def test_diacritics_and_apostrophes_survive_verbatim(self):
        # work order §5.5 item 1 — breve (ŏ, ŭ) and apostrophe must not be
        # ASCII-folded or otherwise altered anywhere along the path.
        e = Entity(node_id="P1", node_type="Person", name_mr="Ch'wisŏn")
        self.assertEqual(e.to_dict()["name_mr"], "Ch'wisŏn")
        self.assertIn("ŏ", e.to_dict()["name_mr"])
        self.assertIn("'", e.to_dict()["name_mr"])


class TestNameRrNeverReadIntoNameField(unittest.TestCase):
    """work order §5.5 item 3/6: `nameRR` is a real import-data field for some
    nodes but must never become a displayed name — reserved-but-dropped."""

    def test_person_from_flat_ignores_name_rr(self):
        # 이규보: nameMR='Yi Kyubo' vs nameRR='Yi Gyubo' — deliberately
        # DIFFERENT strings so a wrong-field read is unambiguous.
        flat = {"id": "P001", "nameKor": "이규보", "nameMR": "Yi Kyubo",
               "nameRR": "Yi Gyubo"}
        e = _person_from_flat(flat)
        self.assertEqual(e.name_mr, "Yi Kyubo")
        self.assertNotIn("Yi Gyubo", (e.name_mr or ""))
        self.assertNotIn("Yi Gyubo", e.authority_ids.values())

    def test_place_from_flat_ignores_name_rr(self):
        flat = {"id": "L001", "nameKor": "북산", "nameMR": "Puksan",
               "nameRR": "Buksan"}
        e = _place_from_flat(flat)
        self.assertEqual(e.name_mr, "Puksan")
        self.assertNotIn("Buksan", e.authority_ids.values())

    def test_person_missing_mr_does_not_fabricate_one(self):
        flat = {"id": "P519", "nameKor": "선조", "nameEng": "King Sŏnjo"}
        e = _person_from_flat(flat)
        self.assertIsNone(e.name_mr)


class TestEntityLineRendersMr(unittest.TestCase):
    """`_entity_line` (via the public format_evidence_for_prompt) surfaces an
    explicit `MR=` field the synthesis LLM can prioritize (rule 12)."""

    def test_mr_field_appears_verbatim_in_evidence_block(self):
        ev = {
            "question": "q", "language": "en",
            "graph": Evidence(kind="graph", entities=[
                Entity(node_id="P519", node_type="Person",
                      name_eng="King Sŏnjo", name_mr=None),
                Entity(node_id="P103", node_type="Person",
                      name_kor="성현", name_mr="Sŏng Hyŏn"),
            ]),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external"),
            "statuses": {"graph": {"source": "graph", "outcome": "ok"},
                        "vector": {"source": "vector", "outcome": "ok"}},
            "coverage": {},
        }
        out = format_evidence_for_prompt(ev, "en")
        self.assertIn("MR=Sŏng Hyŏn", out)
        # No MR on file for P519 -> no fabricated MR= line for that entity,
        # but the stored English name is still shown as-is.
        self.assertIn("King Sŏnjo", out)


class TestSynthesisRulesMentionMrAndRankingAndT1052(unittest.TestCase):
    def test_mr_priority_rule_present(self):
        self.assertIn("McCune-Reischauer", SYNTHESIS_SYSTEM_RULES)
        self.assertIn("MR=", SYNTHESIS_SYSTEM_RULES)

    def test_ranking_rule_present(self):
        self.assertIn("RANKING/AGGREGATION", SYNTHESIS_SYSTEM_RULES)
        self.assertIn("mention_count", SYNTHESIS_SYSTEM_RULES)

    def test_t1052_internal_link_clarification_present(self):
        self.assertIn("NOT a real Wikidata.org record", SYNTHESIS_SYSTEM_RULES)


class TestRetrievalStatusMessagesAreDistinct(unittest.TestCase):
    """work order status contract: no_results / invalid_query /
    temporarily_unavailable must never share wording."""

    def test_graph_three_outcomes_have_distinct_text_per_language(self):
        for lang in ("ko", "en", "zh"):
            no_res = RETRIEVAL_STATUS_MESSAGES[("graph", "no_results")][lang]
            invalid = RETRIEVAL_STATUS_MESSAGES[("graph", "invalid_query")][lang]
            unavail = RETRIEVAL_STATUS_MESSAGES[("graph", "temporarily_unavailable")][lang]
            self.assertNotEqual(no_res, invalid)
            self.assertNotEqual(no_res, unavail)
            self.assertNotEqual(invalid, unavail)


class TestAnswerRendererLinksMrNames(unittest.TestCase):
    def test_mr_only_mention_gets_linked(self):
        entities = [
            {"node_id": "P103", "node_type": "Person", "name_mr": "Sŏng Hyŏn"},
        ]
        body = "According to the entry, Sŏng Hyŏn wrote a critique."
        out = link_entities_in_body(body, entities)
        self.assertIn("[Sŏng Hyŏn](", out)

    def test_name_mr_does_not_override_unambiguous_kor_link(self):
        entities = [
            {"node_id": "P103", "node_type": "Person",
             "name_kor": "성현", "name_mr": "Sŏng Hyŏn"},
        ]
        body = "성현 wrote a critique, and Sŏng Hyŏn is also mentioned."
        out = link_entities_in_body(body, entities)
        self.assertIn("[성현](", out)
        self.assertIn("[Sŏng Hyŏn](", out)


class TestVectorProjectionSourceHasNoDisplayNameRr(unittest.TestCase):
    """work order §5.5 item 6: no `p.nameRR` / `creator_rr` / display
    `nameRR` projection remains in the vector retrieval query, and
    nameMR was added where it was previously missing (places/topics/
    forms_types/critical_terms/era/work).

    The projection moved from `tools/vector.py` to
    `chatbot/retrieval/vector_query.py` (large-module modularization work
    order Phase 5.1); the assertions below are unchanged."""

    def setUp(self):
        self.src = _read(os.path.join("chatbot", "retrieval", "vector_query.py"))

    def test_no_creator_rr_projection(self):
        self.assertNotIn("creator_rr", self.src)

    def test_no_bare_name_rr_projection(self):
        self.assertNotIn("nameRR: p.nameRR", self.src)
        self.assertNotIn("nameRR: a.nameRR", self.src)

    def test_places_topics_work_have_name_mr_projection(self):
        self.assertIn("source_work_mr:", self.src)
        self.assertIn("nameMR: pl.nameMR", self.src)
        self.assertIn("nameMR: t.nameMR", self.src)
        self.assertIn("nameMR: ct.nameMR", self.src)
        self.assertIn("nameMR: e.nameMR", self.src)
        self.assertIn("nameMR: a.nameMR", self.src)


class TestCypherPromptMrGuidance(unittest.TestCase):
    """work order §5.5 item 6: the Cypher-generation prompt no longer claims
    nameRR "does not exist", and documents MR-first priority + standardized
    person_name_mr/place_name_mr aliases.

    The prompt text moved from `tools/cypher.py` to
    `chatbot/retrieval/graph_prompt.py` (large-module modularization work
    order Phase 4.1); the assertions below are unchanged — only the file
    that owns the prompt did."""

    def setUp(self):
        self.src = _read(os.path.join("chatbot", "retrieval", "graph_prompt.py"))

    def test_no_longer_claims_name_rr_does_not_exist(self):
        self.assertNotIn("nameRR does not exist in the data", self.src)

    def test_documents_mr_priority(self):
        self.assertIn("nameMR` is the SOLE authoritative", self.src)

    def test_standardized_aliases_include_name_mr(self):
        self.assertIn("p.nameMR        AS person_name_mr", self.src)
        self.assertIn("pl.nameMR        AS place_name_mr", self.src)


if __name__ == "__main__":
    unittest.main()

"""graph-ranking-reliability work order §5.3 — ranking/aggregation intent
and role-cue detection (tools.graph_intent). Pure functions, no Neo4j/LLM."""

import unittest

from tools.graph_intent import (
    MENTION_COUNT_UNIT,
    ROLE_RELATIONSHIP,
    ROLE_TYPE_CUES,
    build_role_ranking_query,
    detect_role_cue,
    is_graph_aggregation_intent,
    rank_rows_by_mention_count,
)


class TestAggregationIntentDetection(unittest.TestCase):
    def test_exact_reference_question_english(self):
        self.assertTrue(is_graph_aggregation_intent(
            "Who is the most mentioned king in Sihwa ch'ongnim?"))

    def test_exact_reference_question_korean(self):
        self.assertTrue(is_graph_aggregation_intent(
            "시화총림에서 가장 많이 언급된 왕은 누구인가요?"))

    def test_top_n_english(self):
        self.assertTrue(is_graph_aggregation_intent("Top 5 most cited poets"))

    def test_how_many_times_english(self):
        self.assertTrue(is_graph_aggregation_intent(
            "How many times is Sŏnjo mentioned?"))

    def test_korean_rank_cue(self):
        self.assertTrue(is_graph_aggregation_intent("허난설헌은 상위 몇 위인가요?"))

    def test_chinese_cue(self):
        self.assertTrue(is_graph_aggregation_intent("誰是最多提及的王?"))

    def test_plain_biography_question_is_not_aggregation(self):
        self.assertFalse(is_graph_aggregation_intent("Tell me about King Sejong"))
        self.assertFalse(is_graph_aggregation_intent("허난설헌은 누구인가요?"))

    def test_plain_poem_list_question_is_not_aggregation(self):
        self.assertFalse(is_graph_aggregation_intent(
            "List the poems written by 이규보"))

    def test_empty_and_non_string_are_false(self):
        self.assertFalse(is_graph_aggregation_intent(""))
        self.assertFalse(is_graph_aggregation_intent(None))  # type: ignore[arg-type]


class TestRoleCueDetection(unittest.TestCase):
    def test_king_english(self):
        self.assertEqual(
            detect_role_cue("Who is the most mentioned king in Sihwa ch'ongnim?"),
            "king")

    def test_king_korean(self):
        self.assertEqual(
            detect_role_cue("시화총림에서 가장 많이 언급된 왕은 누구인가요?"), "king")

    def test_king_chinese(self):
        self.assertEqual(detect_role_cue("誰是最多提及的王?"), "king")

    def test_no_role_cue_returns_none(self):
        self.assertIsNone(detect_role_cue("Who is the most mentioned poet?"))

    def test_role_cue_alone_without_ranking_language_still_detected(self):
        # detect_role_cue is a pure name-cue matcher — callers are responsible
        # for gating on is_graph_aggregation_intent() first (a bare "king"
        # cue alone, e.g. "Tell me about King Sejong", must NOT be treated as
        # a ranking question by the orchestrator even though the cue matches
        # here).
        self.assertEqual(detect_role_cue("Tell me about King Sejong"), "king")

    def test_empty_and_non_string_are_none(self):
        self.assertIsNone(detect_role_cue(""))
        self.assertIsNone(detect_role_cue(None))  # type: ignore[arg-type]


class TestRoleRegistryShape(unittest.TestCase):
    """Pins the live-verified mapping (see IMPLEMENTATION_NOTE.md) so a
    future edit cannot silently drift back to the CONTAINS-ambiguous or
    HAS_OFFICE-based shape that produced the original bug."""

    def test_king_cues_are_exact_verified_values(self):
        self.assertEqual(ROLE_TYPE_CUES["king"]["en"], ("king",))
        self.assertEqual(ROLE_TYPE_CUES["king"]["ko"], ("왕",))
        self.assertEqual(ROLE_TYPE_CUES["king"]["zh"], ("王",))

    def test_role_relationship_is_has_type_not_has_office(self):
        self.assertEqual(ROLE_RELATIONSHIP, "HAS_TYPE")

    def test_mention_count_unit_is_entry(self):
        self.assertEqual(MENTION_COUNT_UNIT, "entry")


class TestBuildRoleRankingQuery(unittest.TestCase):
    """graph-ranking-reliability work order §5.3 items 1-2: the template
    never produces the malformed backtick shape, matches by EXACT equality
    (never CONTAINS), and returns the standard aliases/params a caller can
    bind safely."""

    def test_no_backtick_multi_label_shape(self):
        cypher, _params = build_role_ranking_query("king")
        self.assertNotIn("`", cypher)

    def test_uses_exact_equality_not_contains(self):
        cypher, _params = build_role_ranking_query("king")
        self.assertIn("topic.nameKor = $topic_ko", cypher)
        self.assertIn("topic.nameEng = $topic_en", cypher)
        self.assertIn("topic.nameChi = $topic_zh", cypher)
        self.assertNotIn("CONTAINS", cypher)

    def test_uses_has_type_relationship(self):
        cypher, _params = build_role_ranking_query("king")
        self.assertIn("[:HAS_TYPE]", cypher)
        self.assertNotIn("HAS_OFFICE", cypher)

    def test_returns_internal_id_and_standard_aliases(self):
        cypher, _params = build_role_ranking_query("king")
        self.assertIn("p.ID AS person_id", cypher)
        self.assertIn("p.nameMR AS person_name_mr", cypher)
        self.assertIn("p.idWikidata AS wikidata_id", cypher)
        self.assertIn("p.idAKSdigerati AS aks_digerati_id", cypher)

    def test_has_order_by_and_limit(self):
        cypher, _params = build_role_ranking_query("king", limit=7)
        self.assertIn("ORDER BY mention_count DESC", cypher)
        self.assertIn("LIMIT 7", cypher)

    def test_params_carry_the_registered_cue_values(self):
        _cypher, params = build_role_ranking_query("king")
        self.assertEqual(params, {
            "topic_ko": "왕", "topic_en": "king", "topic_zh": "王",
        })

    def test_no_string_interpolation_of_natural_language(self):
        # The only user-controllable value baked into the query text at all
        # is the integer limit; the role cue values travel exclusively via
        # `params`, never string-interpolated into the Cypher text itself.
        cypher, params = build_role_ranking_query("king")
        for v in params.values():
            self.assertNotIn(v, cypher)

    def test_unregistered_role_raises_keyerror(self):
        with self.assertRaises(KeyError):
            build_role_ranking_query("queen")


class TestRankRowsByMentionCount(unittest.TestCase):
    def test_single_winner(self):
        rows = [
            {"person_id": "P519", "mention_count": 10},
            {"person_id": "P513", "mention_count": 6},
            {"person_id": "P960", "mention_count": 6},
        ]
        top_count, winners = rank_rows_by_mention_count(rows)
        self.assertEqual(top_count, 10)
        self.assertEqual([w["person_id"] for w in winners], ["P519"])

    def test_tie_returns_all_winners(self):
        rows = [
            {"person_id": "P1", "mention_count": 5},
            {"person_id": "P2", "mention_count": 5},
            {"person_id": "P3", "mention_count": 2},
        ]
        top_count, winners = rank_rows_by_mention_count(rows)
        self.assertEqual(top_count, 5)
        self.assertEqual({w["person_id"] for w in winners}, {"P1", "P2"})

    def test_empty_rows(self):
        top_count, winners = rank_rows_by_mention_count([])
        self.assertIsNone(top_count)
        self.assertEqual(winners, [])


if __name__ == "__main__":
    unittest.main()

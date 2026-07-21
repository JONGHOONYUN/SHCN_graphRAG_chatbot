"""Regression tests for the two user-reported Sources issues:

  (A) URL missing — LLM saw pre-built citations like
        `- poetrytalks wikidata: [P553](https://poetrytalks.org/P553)`
      but only rendered the group label `poetrytalks wikidata` without any
      URL bullets. Fix: rule 7b (verbatim reproduction) + strengthened human
      message. This suite verifies that the pre-built lines DO contain full
      markdown links, that rule 7b is in the prompt, and that the human
      message frame asks for verbatim reproduction.

  (B) Sources shows Korean work names when the answer is English — because
      the retrieval-time `Provenance.label` was Korean-first. Fix: store
      raw `work_name_kor/eng/chi` on Provenance and rebuild the label at
      synthesis time in the locked response language, with bilingual
      "English (Korean)" rendering for non-Korean answers.
"""

import unittest

from tools.evidence import (
    Evidence,
    Provenance,
    document_to_parts,
)
from tools.synthesis import (
    SYNTHESIS_SYSTEM_RULES,
    _format_vector_block,
    _rebuild_vector_prov_label,
    _work_name_bilingual,
    build_citations,
)


# ── (A) Verbatim reproduction ────────────────────────────────────────────────
class TestPrebuiltCitationsCarryFullUrls(unittest.TestCase):
    """The pre-computed citation strings MUST embed full markdown links so
    the LLM has no excuse to output a group name without its URL."""

    def _sample_evidence(self):
        prov = Provenance(
            source_type="neo4j_graph",
            label="Graph: person=[P553](https://poetrytalks.org/P553)",
            source_url="https://poetrytalks.org/P553",
            entity_id="P553",
        )
        return {
            "graph": Evidence(kind="graph", provenance=[prov]),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external"),
        }

    def test_every_ptw_bullet_has_markdown_link(self):
        cites = build_citations(self._sample_evidence(), "en")
        ptw_lines = [c for c in cites if "poetrytalks wikidata" in c]
        self.assertGreater(len(ptw_lines), 0)
        for line in ptw_lines:
            self.assertIn("[P553]", line, line)
            self.assertIn("(https://poetrytalks.org/P553)", line, line)


class TestSystemRulesForbidStrippingUrls(unittest.TestCase):
    def test_rule_7b_present(self):
        self.assertIn("VERBATIM", SYNTHESIS_SYSTEM_RULES)
        self.assertIn("7b", SYNTHESIS_SYSTEM_RULES)

    def test_rule_7b_names_the_failure_mode(self):
        # The rule must call out the exact failure the user reported:
        # stripping URLs / collapsing bullets into headers.
        low = SYNTHESIS_SYSTEM_RULES.lower()
        self.assertIn("strip", low)
        self.assertIn("collapse", low)


class TestAgentHumanMessageDemandsVerbatimReproduction(unittest.TestCase):
    """Regression-guard the agent.py synthesis_prompt human message so a
    future refactor can't quietly loosen the reproduction instruction."""

    def test_message_uses_strong_verb(self):
        import re
        src = open("agent.py", encoding="utf-8").read()
        # The frame must include an imperative telling the model to copy
        # the pre-built citations bullet-for-bullet.
        self.assertRegex(
            src,
            r"(?is)Copy every bullet EXACTLY|reproduce.*verbatim|"
            r"preserve.*markdown link",
        )


# ── (B) Language-aware Sources ──────────────────────────────────────────────
class TestWorkNameBilingualForNonKorean(unittest.TestCase):
    def test_english_answer_picks_english_name_and_appends_korean(self):
        prov = {
            "work_name_kor": "패관잡기",
            "work_name_eng": "Paegwan Chapki",
            "work_name_chi": "稗官雜記",
        }
        self.assertEqual(
            _work_name_bilingual(prov, "en"),
            "Paegwan Chapki (패관잡기)",
        )

    def test_chinese_answer_picks_chinese_name_and_appends_korean(self):
        prov = {
            "work_name_kor": "패관잡기",
            "work_name_eng": "Paegwan Chapki",
            "work_name_chi": "稗官雜記",
        }
        self.assertEqual(
            _work_name_bilingual(prov, "zh"),
            "稗官雜記 (패관잡기)",
        )

    def test_korean_answer_stays_korean_only(self):
        prov = {
            "work_name_kor": "패관잡기",
            "work_name_eng": "Paegwan Chapki",
        }
        self.assertEqual(_work_name_bilingual(prov, "ko"), "패관잡기")

    def test_english_without_english_name_falls_back(self):
        prov = {"work_name_kor": "패관잡기"}
        # Only Korean available — must still return something, not None.
        self.assertEqual(_work_name_bilingual(prov, "en"), "패관잡기")

    def test_empty_returns_none(self):
        self.assertIsNone(_work_name_bilingual({}, "en"))


class TestVectorProvenanceRebuildPerLanguage(unittest.TestCase):
    def _sample_prov(self):
        return {
            "work_name_kor": "패관잡기",
            "work_name_eng": "Paegwan Chapki",
            "work_name_chi": "稗官雜記",
            "work_id": "B023",
            "entry_id": "E031",
            "entry_position": 31,
            "label": "패관잡기 ([B023](https://poetrytalks.org/B023)) > "
                     "Entry 31 ([E031](https://poetrytalks.org/E031))",
        }

    def test_en_uses_english_work_name(self):
        line = _rebuild_vector_prov_label(self._sample_prov(), "en")
        self.assertIn("Paegwan Chapki", line)
        self.assertIn("(패관잡기)", line)   # Korean original in parens
        self.assertIn("[B023](https://poetrytalks.org/B023)", line)
        self.assertIn("Entry 31", line)
        self.assertIn("[E031](https://poetrytalks.org/E031)", line)

    def test_zh_uses_chinese_work_name(self):
        line = _rebuild_vector_prov_label(self._sample_prov(), "zh")
        self.assertIn("稗官雜記", line)
        self.assertIn("(패관잡기)", line)

    def test_ko_stays_korean(self):
        line = _rebuild_vector_prov_label(self._sample_prov(), "ko")
        self.assertIn("패관잡기", line)
        self.assertNotIn("Paegwan Chapki", line)

    def test_falls_back_to_static_label_if_no_raw_components(self):
        prov = {
            "label": "some pre-built label",
            "work_id": None,
            "entry_id": None,
        }
        line = _rebuild_vector_prov_label(prov, "en")
        self.assertEqual(line, "some pre-built label")


class TestBuildCitationsSourcesInAnswerLanguage(unittest.TestCase):
    def _sample_evidence(self):
        # Simulate a vector-search-only result (mirrors the user's failing
        # question "Which woman is mentioned the most…" where graph
        # aggregation was empty and vector supplied the entries).
        doc, _entities, prov = document_to_parts({
            "metadata": {
                "entry_id": "E031",
                "entry_position": 31,
                "source_work_id": "B023",
                "source_work_kor": "패관잡기",
                "source_work_eng": "Paegwan Chapki",
                "poetrytalks_link": "https://poetrytalks.org/E031",
            },
            "page_content": "",
        })
        vector = Evidence(kind="vector", documents=[doc],
                          provenance=list(prov))
        return {"graph": Evidence(kind="graph"),
                "vector": vector,
                "external": Evidence(kind="external")}

    def test_english_answer_no_korean_leak(self):
        cites = build_citations(self._sample_evidence(), "en")
        joined = "\n".join(cites)
        # English name must appear; Korean name is allowed inline only as
        # the bilingual "(패관잡기)" reference, never as the primary name.
        self.assertIn("Paegwan Chapki", joined)
        # The bare "패관잡기 > Entry ..." pattern (Korean-primary) must NOT
        # appear on its own — only inside "Paegwan Chapki (패관잡기)".
        self.assertNotIn("패관잡기 >", joined)

    def test_korean_answer_stays_korean(self):
        cites = build_citations(self._sample_evidence(), "ko")
        joined = "\n".join(cites)
        self.assertIn("패관잡기", joined)
        # Korean answer should NOT append the English name — keep it clean.
        self.assertNotIn("(Paegwan Chapki)", joined)


class TestVectorBlockUsesLanguage(unittest.TestCase):
    def test_english_evidence_block_uses_english_work_name(self):
        vector = {"documents": [{
            "work_name_kor": "패관잡기",
            "work_name_eng": "Paegwan Chapki",
            "entry_id": "E031",
            "entry_position": 31,
        }]}
        out = _format_vector_block(vector, "ok", "en")
        self.assertIn("Paegwan Chapki", out)


if __name__ == "__main__":
    unittest.main()

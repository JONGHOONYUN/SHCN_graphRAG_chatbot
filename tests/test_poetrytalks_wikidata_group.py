"""User-requested behavior:

    (1) EVERY graph node class — Person, Entry, Poem, Critique, Work, Place,
        Topic, Era, CriticalTerm, ... — must yield a
        `https://poetrytalks.org/<id>` URL for its `id` value.
    (2) The group of these URLs is named **"poetrytalks wikidata"** (a proper
        noun, kept literally in every language — never translated).
    (3) The group MUST appear in the Sources of every response that cites any
        graph node. Missing this group is a regression.
"""

import unittest

from tools.evidence import (
    Evidence,
    Provenance,
    is_valid_node_id,
    poetrytalks_url,
    provenance_from_graph_row,
)
from tools.synthesis import (
    CITATION_LABELS,
    SYNTHESIS_SYSTEM_RULES,
    build_citations,
)


# ── (1) All node classes generate URLs ───────────────────────────────────────
class TestAllNodeClassesGetUrl(unittest.TestCase):
    """The user's requirement: any node id (regardless of class) gets a URL.
    We validate the enumerated 8 classes AND a hypothetical unknown-prefix
    class (K123, R042) as proxies for CriticalTerm / other future kinds."""

    def test_known_prefixes(self):
        cases = {
            "B016": "work",
            "E003": "entry",
            "M012": "poem",
            "C001": "critique",
            "P553": "person",
            "L100": "place",
            "T042": "topic",
            "H044": "era",
        }
        for node_id, kind in cases.items():
            with self.subTest(node_id=node_id):
                self.assertTrue(is_valid_node_id(node_id))
                self.assertEqual(
                    poetrytalks_url(node_id),
                    f"https://poetrytalks.org/{node_id}",
                )

    def test_unknown_prefix_still_gets_url(self):
        """CriticalTerm and any hypothetical future node class must NOT be
        dropped just because we haven't hard-coded the letter."""
        for node_id in ("K123", "R042", "V007", "Y1234"):
            with self.subTest(node_id=node_id):
                self.assertTrue(is_valid_node_id(node_id))
                self.assertEqual(
                    poetrytalks_url(node_id),
                    f"https://poetrytalks.org/{node_id}",
                )


# ── (2) The group's name is "poetrytalks wikidata" in every language ─────────
class TestGroupNameIsProperNounEverywhere(unittest.TestCase):
    def test_ko(self):
        self.assertEqual(
            CITATION_LABELS["ko"]["poetrytalks_wikidata_prefix"],
            "poetrytalks wikidata",
        )

    def test_en(self):
        self.assertEqual(
            CITATION_LABELS["en"]["poetrytalks_wikidata_prefix"],
            "poetrytalks wikidata",
        )

    def test_zh(self):
        self.assertEqual(
            CITATION_LABELS["zh"]["poetrytalks_wikidata_prefix"],
            "poetrytalks wikidata",
        )


# ── (3) Every response with any cited node includes the mandatory group ──────
class TestGroupIsAlwaysPresent(unittest.TestCase):
    def _run(self, provs, language):
        ev = {
            "graph": Evidence(kind="graph", provenance=provs),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external"),
        }
        return build_citations(ev, language)

    def test_person_only(self):
        provs = provenance_from_graph_row({"person_id": "P553"})
        cites = self._run(provs, "en")
        self.assertTrue(any(
            "poetrytalks wikidata" in c and "P553" in c and
            "https://poetrytalks.org/P553" in c for c in cites
        ), cites)

    def test_multiple_node_classes(self):
        provs = provenance_from_graph_row({
            "work_id": "B016",
            "entry_id": "E003",
            "poem_id": "M012",
            "person_id": "P027",
        })
        cites = self._run(provs, "ko")
        joined = "\n".join(cites)
        # Every one of the 4 nodes must have its own "poetrytalks wikidata"
        # bullet with the correct URL.
        for node_id in ("B016", "E003", "M012", "P027"):
            self.assertIn(f"poetrytalks wikidata: [{node_id}]", joined)
            self.assertIn(f"https://poetrytalks.org/{node_id}", joined)

    def test_unknown_prefix_still_grouped(self):
        # Simulate a row containing a CriticalTerm-like id (letter not in the
        # prefix map). It MUST still surface under the mandatory group.
        provs = provenance_from_graph_row({
            "person_id": "P027",
            "critical_term_id": "K123",
        })
        cites = self._run(provs, "en")
        joined = "\n".join(cites)
        self.assertIn("poetrytalks wikidata: [K123]", joined)
        self.assertIn("https://poetrytalks.org/K123", joined)

    def test_chinese_language_keeps_proper_name(self):
        provs = provenance_from_graph_row({"person_id": "P553"})
        cites = self._run(provs, "zh")
        # 'poetrytalks wikidata' is a proper name — not translated to Chinese.
        self.assertTrue(any("poetrytalks wikidata" in c for c in cites))

    def test_external_id_values_do_not_leak_into_group(self):
        # idAKSency 'E0063034' must NEVER appear in the poetrytalks wikidata
        # group — it's not a node id.
        provs = provenance_from_graph_row({
            "person_id": "P553",
            "idAKSency": "E0063034",
            "idWikidata": "Q464558",
        })
        cites = self._run(provs, "en")
        joined = "\n".join(cites)
        self.assertNotIn("E0063034", joined)
        self.assertNotIn("Q464558", joined)
        self.assertIn("poetrytalks wikidata: [P553]", joined)


# ── System-rule text carries the mandatory instruction ───────────────────────
class TestSystemRulesEnforceGroup(unittest.TestCase):
    def test_rules_mention_mandatory_group(self):
        # The LLM prompt must include the mandatory-group instruction so the
        # synthesis obeys the policy even if the pre-built citation lines
        # were somehow empty on a downstream refactor.
        self.assertIn("poetrytalks wikidata", SYNTHESIS_SYSTEM_RULES)
        self.assertIn("MANDATORY", SYNTHESIS_SYSTEM_RULES)


if __name__ == "__main__":
    unittest.main()

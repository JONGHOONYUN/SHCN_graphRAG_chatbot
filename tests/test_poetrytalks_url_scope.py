"""Poetry Talks URL scope — only the node's OWN `id` may become a wiki URL.

Regression tests for the failure mode reported by users where the graph
returned a row like

    { person_id: "P553",
      person_name_kor: "허초희",
      idAKSency: "E0063034",   ← Encyclopedia of Korean Culture ID
      idWikidata: "Q464558",   ← Wikidata Q-id
      ... }

and the answer's Sources section rendered `Graph: entry=E0063034` with the
URL `https://poetrytalks.org/E0063034` — a broken link, because E0063034 is
NOT an Entry node ID (Entry IDs are 3-digit: E001…E921).

The correct URL for the Person entity Hŏ Ch'ohŭi is
`https://poetrytalks.org/P553`, derived from her OWN `id` field.
"""

import unittest

from tools.evidence import (
    _is_external_id_key,
    _looks_like_node_id,
    poetrytalks_url,
    provenance_from_graph_row,
)


class TestNodeIdShapeGuard(unittest.TestCase):
    """`_looks_like_node_id` must ACCEPT real node IDs (≤4 digit suffix)
    and REJECT the 7-digit external-authority format."""

    def test_accepts_real_person_ids(self):
        self.assertEqual(_looks_like_node_id("P001"), "person")
        self.assertEqual(_looks_like_node_id("P553"), "person")
        self.assertEqual(_looks_like_node_id("P1249"), "person")

    def test_accepts_real_entry_ids(self):
        self.assertEqual(_looks_like_node_id("E001"), "entry")
        self.assertEqual(_looks_like_node_id("E921"), "entry")

    def test_accepts_other_node_kinds(self):
        self.assertEqual(_looks_like_node_id("B016"), "work")
        self.assertEqual(_looks_like_node_id("M012"), "poem")
        self.assertEqual(_looks_like_node_id("C001"), "critique")
        self.assertEqual(_looks_like_node_id("L100"), "place")
        self.assertEqual(_looks_like_node_id("T042"), "topic")
        self.assertEqual(_looks_like_node_id("H044"), "era")

    def test_rejects_idAKSency_format(self):
        # These are real idAKSency values pulled from node_Person.csv.
        for external_id in ("E0063034", "E0043772", "E0009722",
                            "E0045877", "E0035446"):
            self.assertIsNone(
                _looks_like_node_id(external_id),
                f"{external_id!r} was incorrectly classified as a node ID"
            )

    def test_rejects_5plus_digit_suffix(self):
        # Defensive band: any 5+ digit suffix under a node-prefix letter is
        # rejected regardless of the actual source column.
        for value in ("P12345", "E12345", "H12345"):
            self.assertIsNone(_looks_like_node_id(value))


class TestExternalIdKeyDetection(unittest.TestCase):
    def test_flags_external_authority_columns(self):
        for key in ("idAKSency", "idAKSdigerati", "idAKSmap",
                    "idAKSkdp", "idAKSsillok",
                    "idWikidata", "idLOC", "idOpenLibrary",
                    "idCBDB", "idYaleLux", "idBritannica",
                    "idBNF", "idBnF", "idNLK", "idBritishMuseum",
                    "idWorldHistory", "idAcademiaSinica", "idEncyChina"):
            self.assertTrue(
                _is_external_id_key(key),
                f"{key!r} should be flagged as an external-authority key",
            )

    def test_does_not_flag_node_id_columns(self):
        for key in ("id", "person_id", "entry_id", "work_id", "poem_id",
                    "critique_id", "place_id", "topic_id", "era_id",
                    "creator_person_id", "subject_person_id",
                    "place_id"):
            self.assertFalse(
                _is_external_id_key(key),
                f"{key!r} must NOT be flagged as external",
            )

    def test_ignores_lowercase_id_prefix(self):
        # 'identifier' happens to start with 'id' but the next char is 'e'
        # (lowercase) — never an external-authority column.
        self.assertFalse(_is_external_id_key("identifier"))
        self.assertFalse(_is_external_id_key("identity"))


class TestPoetryTalksUrlOnlyForNodeIds(unittest.TestCase):
    def test_url_for_real_node_id(self):
        self.assertEqual(
            poetrytalks_url("P553"),
            "https://poetrytalks.org/P553",
        )

    def test_no_url_for_idAKSency_value(self):
        # This is the exact value that produced the broken link in
        # production. It MUST NOT resolve to a URL.
        self.assertIsNone(poetrytalks_url("E0063034"))

    def test_no_url_for_wikidata_qid(self):
        self.assertIsNone(poetrytalks_url("Q464558"))


class TestProvenanceIgnoresExternalIdColumns(unittest.TestCase):
    """`provenance_from_graph_row` must derive its ids solely from node-own
    id columns, NEVER from external-authority columns."""

    def test_row_with_person_and_external_ids(self):
        # Modeled after the row that caused the reported broken URL.
        row = {
            "person_id": "P553",
            "person_name_kor": "허초희",
            "person_name_chi": "許楚姬",
            "idAKSency": "E0063034",
            "idWikidata": "Q464558",
            "idLOC": "n81055101",
        }
        provs = provenance_from_graph_row(row)
        self.assertEqual(len(provs), 1)

        label = provs[0].label
        # The correct Person URL must appear.
        self.assertIn("P553", label)
        self.assertIn("https://poetrytalks.org/P553", label)
        # The idAKSency value must NEVER appear as a URL — it's not a node.
        self.assertNotIn("E0063034", label)
        self.assertNotIn("entry=", label)
        # source_url must point at the Person, not the AKS Ency page.
        self.assertEqual(
            provs[0].source_url,
            "https://poetrytalks.org/P553",
        )

    def test_row_with_only_external_ids_produces_no_provenance(self):
        # A degenerate row that has only external references and no node id
        # must return an empty provenance list — no fake URL is fabricated.
        row = {
            "idAKSency": "E0063034",
            "idWikidata": "Q464558",
        }
        self.assertEqual(provenance_from_graph_row(row), [])

    def test_mixed_row_still_picks_correct_ids(self):
        row = {
            "work_id": "B016",
            "entry_id": "E003",
            "poem_id": "M012",
            "creator_person_id": "P027",
            "idAKSency": "E0043772",   # Yi Kyubo's AKS Ency ID — irrelevant here
            "idWikidata": "Q2913717",
        }
        provs = provenance_from_graph_row(row)
        self.assertEqual(len(provs), 1)
        label = provs[0].label
        for real_id in ("B016", "E003", "M012", "P027"):
            self.assertIn(real_id, label)
        self.assertNotIn("E0043772", label)


if __name__ == "__main__":
    unittest.main()

"""work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §5.1 —
language-locked presentation order for parallel source-text fields
(textEng/textKor/textChi and role-prefixed <role>_text_eng|kor|chi), in
BOTH the graphRAG Graph/Vector evidence formatters and the shared pure
helper. No live Gemini/Neo4j/network is used anywhere in this file.
"""

import unittest

from tools.evidence import Evidence
from tools.synthesis import (
    format_evidence_for_prompt,
    reorder_source_text_fields,
    source_text_priority,
)


class TestSourceTextPriority(unittest.TestCase):
    def test_en_order(self):
        self.assertEqual(source_text_priority("en"),
                         ("textEng", "textKor", "textChi"))

    def test_ko_order(self):
        self.assertEqual(source_text_priority("ko"),
                         ("textKor", "textEng", "textChi"))

    def test_zh_order(self):
        self.assertEqual(source_text_priority("zh"),
                         ("textChi", "textKor", "textEng"))

    def test_unsupported_language_falls_back_to_ko(self):
        self.assertEqual(source_text_priority("fr"), source_text_priority("ko"))
        self.assertEqual(source_text_priority(""), source_text_priority("ko"))
        self.assertEqual(source_text_priority(None), source_text_priority("ko"))


class TestReorderSourceTextFields(unittest.TestCase):
    def _row(self):
        return {
            "critique_id": "C001",
            "critique_text_kor": "우리나라 시로는…",
            "critique_text_chi": "我國詩「」\n第二行",
            "critique_text_eng": "Our country's poetry…",
            "entry_id": "E001",
        }

    def test_en_puts_eng_before_kor_before_chi(self):
        row = self._row()
        ordered = reorder_source_text_fields(row, "en")
        keys = list(ordered.keys())
        self.assertLess(keys.index("critique_text_eng"), keys.index("critique_text_kor"))
        self.assertLess(keys.index("critique_text_kor"), keys.index("critique_text_chi"))

    def test_ko_puts_kor_first(self):
        row = self._row()
        ordered = reorder_source_text_fields(row, "ko")
        keys = list(ordered.keys())
        self.assertLess(keys.index("critique_text_kor"), keys.index("critique_text_eng"))
        self.assertLess(keys.index("critique_text_eng"), keys.index("critique_text_chi"))

    def test_zh_puts_chi_first(self):
        row = self._row()
        ordered = reorder_source_text_fields(row, "zh")
        keys = list(ordered.keys())
        self.assertLess(keys.index("critique_text_chi"), keys.index("critique_text_kor"))
        self.assertLess(keys.index("critique_text_kor"), keys.index("critique_text_eng"))

    def test_bare_text_fields_also_reordered(self):
        row = {"id": "M001", "textKor": "k", "textEng": "e", "textChi": "c"}
        ordered = reorder_source_text_fields(row, "en")
        keys = list(ordered.keys())
        self.assertLess(keys.index("textEng"), keys.index("textKor"))
        self.assertLess(keys.index("textKor"), keys.index("textChi"))

    def test_missing_field_is_silently_omitted_relative_order_preserved(self):
        row = {"poem_text_kor": "k", "poem_text_eng": "e"}   # no chi at all
        ordered = reorder_source_text_fields(row, "en")
        self.assertNotIn("poem_text_chi", ordered)
        keys = list(ordered.keys())
        self.assertLess(keys.index("poem_text_eng"), keys.index("poem_text_kor"))

    def test_nested_collect_list_reordered_same_rule(self):
        row = {
            "entry_id": "E001",
            "contained_critiques": [
                {"id": "C001", "critique_text_kor": "k1", "critique_text_eng": "e1"},
                {"id": "C002", "critique_text_kor": "k2", "critique_text_chi": "c2",
                 "critique_text_eng": "e2"},
            ],
        }
        ordered = reorder_source_text_fields(row, "en")
        for item in ordered["contained_critiques"]:
            keys = list(item.keys())
            eng_idx = keys.index("critique_text_eng")
            kor_idx = keys.index("critique_text_kor")
            self.assertLess(eng_idx, kor_idx)

    def test_values_are_byte_for_byte_unchanged(self):
        row = {
            "textEng": "quote with \"quotes\" and\nnewline",
            "textChi": "「引用」。標點符號、換行\n第二行",
            "textKor": "따옴표 \"인용\" 및\n줄바꿈",
        }
        ordered = reorder_source_text_fields(row, "ko")
        self.assertEqual(ordered["textEng"], row["textEng"])
        self.assertEqual(ordered["textChi"], row["textChi"])
        self.assertEqual(ordered["textKor"], row["textKor"])

    def test_original_object_never_mutated(self):
        row = self._row()
        import copy
        snapshot = copy.deepcopy(row)
        reorder_source_text_fields(row, "en")
        self.assertEqual(row, snapshot)

    def test_non_text_fields_untouched_and_unrelated_keys_preserved(self):
        row = {"critique_id": "C001", "note": "irrelevant", "critique_text_kor": "k"}
        ordered = reorder_source_text_fields(row, "en")
        self.assertEqual(ordered["critique_id"], "C001")
        self.assertEqual(ordered["note"], "irrelevant")

    def test_scalar_and_list_passthrough(self):
        self.assertEqual(reorder_source_text_fields("plain string", "en"), "plain string")
        self.assertEqual(reorder_source_text_fields(42, "en"), 42)
        self.assertEqual(reorder_source_text_fields(None, "en"), None)
        self.assertEqual(reorder_source_text_fields([1, "x", None], "en"), [1, "x", None])


class TestGraphBlockUsesLanguageOrder(unittest.TestCase):
    """§5.1: 'Graph top-level critique_text_kor/chi/eng가 영어에서 eng -> kor ->
    chi로 정렬됨' via the actual formatter, not just the pure helper."""

    def test_graph_block_orders_by_language(self):
        graph = {
            "documents": [{
                "critique_id": "C001",
                "critique_text_kor": "한국어 본문",
                "critique_text_chi": "中文本文",
                "critique_text_eng": "English body",
            }],
        }
        evidence = {
            "question": "q", "language": "en",
            "graph": Evidence(kind="graph", documents=graph["documents"]),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external"),
            "statuses": {"graph": {"source": "graph", "outcome": "ok"},
                        "vector": {"source": "vector", "outcome": "ok"}},
            "coverage": {},
        }
        out = format_evidence_for_prompt(evidence, "en")
        eng_idx = out.index("critique_text_eng")
        kor_idx = out.index("critique_text_kor")
        chi_idx = out.index("critique_text_chi")
        self.assertLess(eng_idx, kor_idx)
        self.assertLess(kor_idx, chi_idx)

    def test_graph_block_korean_order(self):
        graph_docs = [{
            "critique_id": "C001",
            "critique_text_kor": "한국어 본문",
            "critique_text_chi": "中文本文",
            "critique_text_eng": "English body",
        }]
        evidence = {
            "question": "q", "language": "ko",
            "graph": Evidence(kind="graph", documents=graph_docs),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external"),
            "statuses": {"graph": {"source": "graph", "outcome": "ok"},
                        "vector": {"source": "vector", "outcome": "ok"}},
            "coverage": {},
        }
        out = format_evidence_for_prompt(evidence, "ko")
        self.assertLess(out.index("critique_text_kor"), out.index("critique_text_eng"))
        self.assertLess(out.index("critique_text_eng"), out.index("critique_text_chi"))

    def test_graph_block_does_not_mutate_original_documents(self):
        docs = [{"critique_text_kor": "k", "critique_text_eng": "e"}]
        evidence = {
            "graph": Evidence(kind="graph", documents=docs),
            "vector": Evidence(kind="vector"), "external": Evidence(kind="external"),
            "statuses": {}, "coverage": {},
        }
        import copy
        snapshot = copy.deepcopy(docs)
        format_evidence_for_prompt(evidence, "en")
        self.assertEqual(docs, snapshot)


class TestVectorBlockUsesLanguageOrder(unittest.TestCase):
    def _evidence(self, language):
        vector_docs = [{
            "entry_id": "E001", "source_work_kor": "지봉유설",
            "textEng": "English quote", "textKor": "한국어 인용",
            "textChi": "中文引用", "descEng": "a description",
        }]
        return {
            "graph": Evidence(kind="graph"),
            "vector": Evidence(kind="vector", documents=vector_docs),
            "external": Evidence(kind="external"),
            "statuses": {"graph": {"source": "graph", "outcome": "ok"},
                        "vector": {"source": "vector", "outcome": "ok"}},
            "coverage": {},
        }

    def test_en_order_eng_kor_chi_then_desc(self):
        out = format_evidence_for_prompt(self._evidence("en"), "en")
        eng_idx = out.index("textEng: English quote")
        kor_idx = out.index("textKor: 한국어 인용")
        chi_idx = out.index("textChi: 中文引用")
        desc_idx = out.index("descEng: a description")
        self.assertLess(eng_idx, kor_idx)
        self.assertLess(kor_idx, chi_idx)
        self.assertLess(chi_idx, desc_idx)     # descEng always after the three

    def test_ko_order_kor_first(self):
        out = format_evidence_for_prompt(self._evidence("ko"), "ko")
        self.assertLess(out.index("textKor: 한국어 인용"), out.index("textEng: English quote"))
        self.assertLess(out.index("textEng: English quote"), out.index("textChi: 中文引用"))

    def test_zh_order_chi_first(self):
        out = format_evidence_for_prompt(self._evidence("zh"), "zh")
        self.assertLess(out.index("textChi: 中文引用"), out.index("textKor: 한국어 인용"))
        self.assertLess(out.index("textKor: 한국어 인용"), out.index("textEng: English quote"))

    def test_missing_field_omitted_relative_order_kept(self):
        docs = [{"entry_id": "E001", "textEng": "only english"}]
        evidence = {
            "graph": Evidence(kind="graph"),
            "vector": Evidence(kind="vector", documents=docs),
            "external": Evidence(kind="external"), "statuses": {}, "coverage": {},
        }
        out = format_evidence_for_prompt(evidence, "en")
        self.assertIn("textEng: only english", out)
        self.assertNotIn("textKor:", out)
        self.assertNotIn("textChi:", out)


if __name__ == "__main__":
    unittest.main()

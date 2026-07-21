"""Phase 6 — `INDEX_BY_LANG` is a single source of truth.

Both `tools.vector` and `text_rag` must reference the same object owned by
`rag_config`. Regression here means the two modules could diverge again.
"""

import unittest


class TestRagConfigSingleSource(unittest.TestCase):
    def test_rag_config_defines_index_by_lang(self):
        import rag_config
        self.assertIn("ko", rag_config.INDEX_BY_LANG)
        self.assertIn("en", rag_config.INDEX_BY_LANG)
        self.assertIn("zh", rag_config.INDEX_BY_LANG)

    def test_ko_config(self):
        import rag_config
        cfg = rag_config.INDEX_BY_LANG["ko"]
        self.assertEqual(cfg["index_name"], "EntryTextsKor")
        self.assertEqual(cfg["text_property"], "textKor")
        self.assertEqual(cfg["embedding_property"], "textEmbedding_Kor")

    def test_en_config(self):
        import rag_config
        cfg = rag_config.INDEX_BY_LANG["en"]
        self.assertEqual(cfg["index_name"], "EntryTextsEng")
        self.assertEqual(cfg["text_property"], "textEng")
        self.assertEqual(cfg["embedding_property"], "textEmbedding_Eng")

    def test_zh_config(self):
        import rag_config
        cfg = rag_config.INDEX_BY_LANG["zh"]
        self.assertEqual(cfg["index_name"], "EntryTextsChi")
        self.assertEqual(cfg["text_property"], "textChi")
        self.assertEqual(cfg["embedding_property"], "textEmbedding_Chi")

    def test_index_config_for_unknown_falls_back_to_ko(self):
        import rag_config
        cfg = rag_config.index_config_for("fr")
        self.assertEqual(cfg, rag_config.INDEX_BY_LANG["ko"])

    def test_index_config_for_empty_falls_back_to_ko(self):
        import rag_config
        self.assertEqual(rag_config.index_config_for(""),
                         rag_config.INDEX_BY_LANG["ko"])


class TestNoDuplicateInVectorPy(unittest.TestCase):
    """`tools/vector.py` used to define its own INDEX_BY_LANG dict. Phase 6
    removes that: only the re-export from `rag_config` may remain."""

    def test_vector_py_does_not_redefine_index_by_lang(self):
        src = open("tools/vector.py", encoding="utf-8").read()
        # Textual mentions in prompt strings and comments are fine; a literal
        # dict assignment is not.
        self.assertNotIn(
            "INDEX_BY_LANG = {", src,
            "tools/vector.py must not redefine INDEX_BY_LANG as a literal.",
        )
        # And a from-import from rag_config must be present.
        self.assertIn("from rag_config import INDEX_BY_LANG", src)


class TestNoDuplicateInTextRagPy(unittest.TestCase):
    def test_text_rag_py_does_not_redefine_index_by_lang(self):
        src = open("text_rag.py", encoding="utf-8").read()
        self.assertNotIn(
            "INDEX_BY_LANG = {", src,
            "text_rag.py must not redefine INDEX_BY_LANG as a literal.",
        )


if __name__ == "__main__":
    unittest.main()

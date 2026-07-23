"""Phase 6 — `INDEX_BY_LANG` is a single source of truth.

Both `tools.vector` and `text_rag` must reference the same object owned by
`rag_config`. Regression here means the two modules could diverge again.
"""

import unittest
from pathlib import Path


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
        src = Path("tools/vector.py").read_text(encoding="utf-8")
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
        src = Path("text_rag.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "INDEX_BY_LANG = {", src,
            "text_rag.py must not redefine INDEX_BY_LANG as a literal.",
        )


class TestTextRagRetrievalMetadataContract(unittest.TestCase):
    """textRAG must project the public domain ID, not an export/runtime id."""

    @staticmethod
    def _builder_source():
        src = Path("text_rag.py").read_text(encoding="utf-8")
        start = src.index("def _build_light_retrieval_query")
        end = src.index("def _get_text_retriever_for_lang", start)
        return src[start:end]

    def test_entry_and_work_use_uppercase_domain_id(self):
        builder = self._builder_source()
        self.assertIn("entry_id: node.ID", builder)
        self.assertIn("| w.ID][0]", builder)
        self.assertNotIn("node.id", builder)
        self.assertNotIn("| w.id][0]", builder)

    def test_poetrytalks_link_uses_shared_base_url_and_domain_id(self):
        src = Path("text_rag.py").read_text(encoding="utf-8")
        builder = self._builder_source()
        self.assertIn(
            "from tools.evidence import POETRYTALKS_BASE_URL", src
        )
        self.assertIn(
            "poetrytalks_link: '{POETRYTALKS_BASE_URL}' + node.ID",
            builder,
        )


if __name__ == "__main__":
    unittest.main()

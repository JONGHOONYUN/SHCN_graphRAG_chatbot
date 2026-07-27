"""work order CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md §5.3 —
independent vectorRAG (text_rag.py) document_prompt contract: the custom
per-document prompt actually carries the three parallel-language bodies (in
this session's response-language priority) and Entry/Work provenance, and
`result["context"]` normalizes through the SAME deterministic
build_citations()/assemble_final_answer() boundary graphRAG uses.

Tests `tools.vectorrag_prompt` directly (pure, no streamlit/neo4j/llm import)
rather than importing `text_rag.py`, which constructs live Gemini/Neo4j
clients at module import time — matching the existing project convention of
reading text_rag.py's SOURCE as text for identifier/wording checks instead.
"""

import os
import unittest

from langchain_core.documents import Document

from tools.answer_renderer import assemble_final_answer
from tools.evidence import collect_node_references, docs_to_evidence
from tools.synthesis import build_citations
from tools.vectorrag_prompt import (
    document_prompt_for_lang,
    prepare_documents_for_prompt,
    provenance_block,
    quoted_text_block,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


FULL_META = {
    "entry_id": "E003", "entry_position": 3,
    "entry_name_kor": "월야 항목", "entry_name_eng": None,
    "original_chinese": "月夜漢文",
    "korean_translation": "달밤 한국어 번역",
    "english_translation": "Moonlit night English translation",
    "source_work_kor": "지봉유설", "source_work_eng": "Jibong yusol",
    "source_work_id": "B023",
    "poetrytalks_link": "https://poetrytalks.org/E003",
}


class TestQuotedTextBlockOrder(unittest.TestCase):
    def test_en_order(self):
        block = quoted_text_block(FULL_META, "en")
        self.assertLess(block.index("[English]"), block.index("[Korean]"))
        self.assertLess(block.index("[Korean]"), block.index("[Chinese]"))

    def test_ko_order(self):
        block = quoted_text_block(FULL_META, "ko")
        self.assertLess(block.index("[Korean]"), block.index("[English]"))
        self.assertLess(block.index("[English]"), block.index("[Chinese]"))

    def test_zh_order(self):
        block = quoted_text_block(FULL_META, "zh")
        self.assertLess(block.index("[Chinese]"), block.index("[Korean]"))
        self.assertLess(block.index("[Korean]"), block.index("[English]"))

    def test_values_verbatim(self):
        block = quoted_text_block(FULL_META, "en")
        self.assertIn("Moonlit night English translation", block)
        self.assertIn("달밤 한국어 번역", block)
        self.assertIn("月夜漢文", block)

    def test_missing_field_silently_omitted(self):
        meta = dict(FULL_META)
        meta["english_translation"] = None
        block = quoted_text_block(meta, "en")
        self.assertNotIn("[English]", block)
        self.assertIn("[Korean]", block)

    def test_empty_meta_returns_empty_string(self):
        self.assertEqual(quoted_text_block({}, "en"), "")
        self.assertEqual(quoted_text_block(None, "en"), "")


class TestProvenanceBlock(unittest.TestCase):
    def test_includes_work_entry_and_link(self):
        block = provenance_block(FULL_META)
        self.assertIn("지봉유설", block)
        self.assertIn("B023", block)
        self.assertIn("E003", block)
        self.assertIn("https://poetrytalks.org/E003", block)

    def test_no_none_placeholder_when_fields_missing(self):
        meta = {"entry_id": "E003"}
        block = provenance_block(meta)
        self.assertNotIn("None", block)
        self.assertIn("E003", block)

    def test_empty_meta_no_crash(self):
        self.assertIsInstance(provenance_block({}), str)
        self.assertIsInstance(provenance_block(None), str)


class TestPrepareDocumentsForPrompt(unittest.TestCase):
    def test_adds_computed_keys_preserves_originals(self):
        doc = Document(page_content="raw", metadata=dict(FULL_META))
        prepared = prepare_documents_for_prompt([doc], "en")
        meta = prepared[0].metadata
        self.assertIn("quoted_text_block", meta)
        self.assertIn("provenance_block", meta)
        for k, v in FULL_META.items():
            self.assertEqual(meta.get(k), v)

    def test_original_document_not_mutated(self):
        doc = Document(page_content="raw", metadata=dict(FULL_META))
        original_keys = set(doc.metadata.keys())
        prepare_documents_for_prompt([doc], "en")
        self.assertEqual(set(doc.metadata.keys()), original_keys)

    def test_accepts_plain_dict_documents(self):
        prepared = prepare_documents_for_prompt(
            [{"page_content": "raw", "metadata": dict(FULL_META)}], "ko")
        self.assertEqual(len(prepared), 1)
        self.assertTrue(prepared[0].metadata["quoted_text_block"].startswith("[Korean]"))

    def test_document_prompt_template_renders_both_blocks(self):
        doc = Document(page_content="raw", metadata=dict(FULL_META))
        prepared = prepare_documents_for_prompt([doc], "en")
        template = document_prompt_for_lang("en")
        rendered = template.format(**prepared[0].metadata)
        self.assertIn("Moonlit night English translation", rendered)
        self.assertIn("지봉유설", rendered)

    def test_empty_docs_list(self):
        self.assertEqual(prepare_documents_for_prompt([], "en"), [])
        self.assertEqual(prepare_documents_for_prompt(None, "en"), [])


class TestVectorRagDeterministicAssembly(unittest.TestCase):
    """§5.3: result['context'] normalizes through docs_to_evidence() and the
    SAME build_citations()/assemble_final_answer() boundary graphRAG uses —
    a fake model Sources section is discarded, and the real Sources header
    appears exactly once with correct name/ID fallback behavior."""

    def _prepared_context(self, language):
        doc = Document(page_content="raw", metadata=dict(FULL_META))
        return prepare_documents_for_prompt([doc], language)

    def test_fake_model_sources_discarded_and_appears_once(self):
        context = self._prepared_context("en")
        evidence = docs_to_evidence(context)
        citations = build_citations({"graph": None, "vector": evidence, "external": None}, "en")
        node_refs = [r.to_dict() for r in collect_node_references(evidence)]
        link_targets = [e.to_dict() for e in evidence.entities] + node_refs

        fake_model_body = (
            "The moon is a recurring image.\n\n"
            "## Sources\n- I made this up: [X999](https://evil.example/X999)"
        )
        out = assemble_final_answer(fake_model_body, citations, "en", entities=link_targets)
        self.assertEqual(out.count("## Sources"), 1)
        self.assertNotIn("evil.example", out)
        self.assertNotIn("I made this up", out)

    def test_named_work_and_id_only_entry_both_present(self):
        context = self._prepared_context("en")
        evidence = docs_to_evidence(context)
        citations = build_citations({"graph": None, "vector": evidence, "external": None}, "en")
        joined = "\n".join(citations)
        # Work has a name -> bilingual citation.
        self.assertIn("[B023](https://poetrytalks.org/B023) — Jibong yusol", joined)
        # Entry has no nameEng and this fixture's nameKor is generic filler
        # text, not a real title, but the ID link itself must still be
        # present regardless (never silently dropped).
        self.assertIn("[E003](https://poetrytalks.org/E003)", joined)

    def test_entry_without_any_name_falls_back_to_id_only(self):
        meta = dict(FULL_META)
        meta["entry_name_kor"] = None
        meta["entry_name_eng"] = None
        doc = Document(page_content="raw", metadata=meta)
        evidence = docs_to_evidence(prepare_documents_for_prompt([doc], "en"))
        citations = build_citations({"graph": None, "vector": evidence, "external": None}, "en")
        ptw_entry_lines = [c for c in citations
                          if "poetrytalks wikidata" in c and "E003" in c]
        self.assertEqual(len(ptw_entry_lines), 1)
        self.assertNotIn("—", ptw_entry_lines[0])   # no name suffix at all

    def test_korean_response_language_order_reaches_prompt_text(self):
        context = self._prepared_context("ko")
        rendered = document_prompt_for_lang("ko").format(**context[0].metadata)
        self.assertLess(rendered.index("[Korean]"), rendered.index("[English]"))


class TestTextRagSourceContract(unittest.TestCase):
    """Static checks on text_rag.py's actual source (never imported — see
    module docstring) confirming Phase 4's wiring is really in place."""

    def setUp(self):
        self.src = _read("text_rag.py")

    def test_entry_name_fields_added_to_retrieval_query(self):
        self.assertIn("entry_name_kor: node.nameKor", self.src)
        self.assertIn("entry_name_eng: node.nameEng", self.src)

    def test_document_prompt_passed_to_stuff_chain(self):
        self.assertIn("document_prompt=document_prompt_for_lang(response_language)", self.src)

    def test_model_instructed_not_to_write_sources(self):
        self.assertIn("Sources", self.src)
        self.assertIn("직접 작성하지 마세요", self.src)

    def test_context_normalized_via_docs_to_evidence_and_shared_assembly(self):
        self.assertIn("docs_to_evidence(result.get(\"context\")", self.src)
        self.assertIn("build_citations(evidence_dict, response_language)", self.src)
        self.assertIn("assemble_final_answer(body, citations, response_language", self.src)

    def test_protected_identifiers_unchanged(self):
        # These names/suffixes must survive untouched (work order §9 /
        # the prior UI-label work order's own non-goal list).
        self.assertIn("def generate_text_rag_response(", self.src)
        self.assertIn("def _get_text_retriever_for_lang(", self.src)
        self.assertIn('f"{get_session_id()}::textRAG"', self.src)

    def test_no_hardcoded_chinese_first_instruction_remains(self):
        self.assertNotIn("한자 원문(original_chinese)을 먼저", self.src)


if __name__ == "__main__":
    unittest.main()

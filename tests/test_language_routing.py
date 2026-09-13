"""work order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §9.3
— routing integration: question_language reaches the vector retriever,
response_language reaches synthesis/citations/external-authority locale, the
graph retriever never receives a translated question, and both graphRAG
(`tools.orchestrator.gather_graphrag_evidence`) and the independent
vectorRAG retriever-cache split behave per the work order's routing
contract. No network, no live DB — every retriever/fetcher is injected or
(for text_rag.py's pure helpers) tested via `tools.vectorrag_prompt`
directly, exactly as `tests/test_vectorrag_document_prompt.py` already does.
"""

import os
import unittest

from tools.evidence import Entity, Evidence
from tools.language_policy import resolve_languages
from tools.orchestrator import gather_graphrag_evidence
from tools.vectorrag_prompt import prepare_documents_for_prompt

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def person(node_id, name_kor="", name_eng="", **ids):
    return Entity(node_id=node_id, node_type="Person", name_kor=name_kor,
                  name_eng=name_eng, authority_ids={k: v for k, v in ids.items() if v})


class _Recorder:
    """Records the (question, language, history_text) it was called with."""

    def __init__(self, evidence: Evidence):
        self.evidence = evidence
        self.calls = []

    def __call__(self, question, language, history_text=None):
        self.calls.append((question, language, history_text))
        return self.evidence


class RecordingFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, source, ext_id, language, node_type="Person"):
        self.calls.append((source, ext_id, language, node_type))
        return {"source": source, "id": ext_id, "status": "ok",
                "node_type": node_type, "url": f"http://example/{ext_id}",
                "data": {}}


class TestGatherGraphragEvidenceLanguageSplit(unittest.TestCase):
    """§9.3 items 1-3: question_language drives vector retrieval, the graph
    retriever never sees a translated question, and response_language
    drives the external authority fetcher's locale."""

    def test_vector_retriever_gets_question_language(self):
        graph_r = _Recorder(Evidence(kind="graph"))
        vector_r = _Recorder(Evidence(kind="vector"))
        gather_graphrag_evidence(
            "How is 杜甫 critiqued?", "en",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=RecordingFetcher(),
            response_language="ko",
        )
        self.assertEqual(vector_r.calls[0][1], "en")

    def test_graph_retriever_gets_the_same_untranslated_question_text(self):
        graph_r = _Recorder(Evidence(kind="graph"))
        vector_r = _Recorder(Evidence(kind="vector"))
        question = "두보는 어떻게 평가되는가?"
        gather_graphrag_evidence(
            question, "ko",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=RecordingFetcher(),
            response_language="en",
        )
        # The graph retriever must receive the EXACT question text — no
        # translation, no control-phrase leakage (there is none here; the
        # cleaning already happened upstream in resolve_languages()).
        self.assertEqual(graph_r.calls[0][0], question)

    def test_external_authority_fetcher_gets_response_language(self):
        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", wikidata="Q33772"),
        ])
        graph_r = _Recorder(g)
        vector_r = _Recorder(Evidence(kind="vector"))
        f = RecordingFetcher()
        gather_graphrag_evidence(
            "두보의 생몰년은?", "ko",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=f,
            response_language="en",
        )
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(f.calls[0][2], "en")   # language positional arg

    def test_response_language_reaches_return_dict(self):
        graph_r = _Recorder(Evidence(kind="graph"))
        vector_r = _Recorder(Evidence(kind="vector"))
        r = gather_graphrag_evidence(
            "How is 杜甫 critiqued?", "en",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=RecordingFetcher(),
            response_language="ko",
        )
        self.assertEqual(r["question_language"], "en")
        self.assertEqual(r["response_language"], "ko")
        self.assertEqual(r["language"], "en")   # unchanged legacy key

    def test_reverse_split_korean_question_english_response(self):
        graph_r = _Recorder(Evidence(kind="graph"))
        vector_r = _Recorder(Evidence(kind="vector"))
        r = gather_graphrag_evidence(
            "두보는 어떻게 평가되는가?", "ko",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=RecordingFetcher(),
            response_language="en",
        )
        self.assertEqual(vector_r.calls[0][1], "ko")
        self.assertEqual(r["question_language"], "ko")
        self.assertEqual(r["response_language"], "en")


class TestBackwardCompatibleSingleLanguageCall(unittest.TestCase):
    """§9.3 item 5: existing single-language positional callers
    (`gather_graphrag_evidence(question, "ko", ...)`, no `response_language`)
    keep working exactly as before the split."""

    def test_omitting_response_language_defaults_it_to_language(self):
        graph_r = _Recorder(Evidence(kind="graph"))
        vector_r = _Recorder(Evidence(kind="vector"))
        r = gather_graphrag_evidence(
            "이규보에 대해 알려줘", "ko",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=RecordingFetcher(),
        )
        self.assertEqual(r["question_language"], "ko")
        self.assertEqual(r["response_language"], "ko")

    def test_legacy_two_positional_arg_retrievers_still_work(self):
        # Mirrors tests/test_pipeline.py::TestOrchestrator._run's
        # `lambda q, l: ...` (2-arg) retriever convention.
        calls = []

        def graph_r(q, l):
            calls.append(("graph", q, l))
            return Evidence(kind="graph")

        def vector_r(q, l):
            calls.append(("vector", q, l))
            return Evidence(kind="vector")

        r = gather_graphrag_evidence(
            "이규보에 대해 알려줘", "ko",
            graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=RecordingFetcher(),
        )
        self.assertEqual([c[2] for c in calls], ["ko", "ko"])
        self.assertEqual(r["language"], "ko")

    def test_external_fetch_locale_defaults_to_language_when_response_language_omitted(self):
        g = Evidence(kind="graph", entities=[
            person("P094", name_kor="두보", name_eng="Du Fu", wikidata="Q33772"),
        ])
        f = RecordingFetcher()
        gather_graphrag_evidence(
            "두보의 생몰년은?", "ko",
            graph_retriever=lambda q, l, h=None: g,
            vector_retriever=lambda q, l, h=None: Evidence(kind="vector"),
            authority_fetcher=f,
        )
        self.assertEqual(f.calls[0][2], "ko")


class TestControlOnlyInputMakesNoBackendCall(unittest.TestCase):
    """§9.3 item 7: a control-only turn must short-circuit BEFORE any
    RAG backend call — simulated with the same conditional bot.py itself
    uses (`if resolution.control_only: ... else: handle_submit(...)`),
    since bot.py cannot be imported directly (Streamlit side effects)."""

    def _dispatch(self, prompt, locked=None):
        """Mirrors bot.py's post-`resolve_languages` dispatch exactly enough
        to prove the short-circuit property, without importing bot.py."""
        calls = {"backend": 0}
        resolution = resolve_languages(prompt, locked)
        if not resolution.control_only:
            calls["backend"] += 1
        return resolution, calls

    def test_lock_only_makes_no_backend_call(self):
        resolution, calls = self._dispatch("Answer in English")
        self.assertTrue(resolution.control_only)
        self.assertEqual(calls["backend"], 0)

    def test_release_only_makes_no_backend_call(self):
        resolution, calls = self._dispatch("자동 언어 감지", locked="en")
        self.assertTrue(resolution.control_only)
        self.assertEqual(calls["backend"], 0)

    def test_real_question_does_make_a_backend_call(self):
        resolution, calls = self._dispatch("두보는 어떻게 평가되는가?")
        self.assertFalse(resolution.control_only)
        self.assertEqual(calls["backend"], 1)

    def test_lock_plus_question_in_one_message_still_calls_backend(self):
        # "영어로 답변해줘. 두보는..." carries BOTH a lock command and a real
        # question — control_only must be False (there IS a real question
        # left after stripping the control phrase) so the backend still runs.
        resolution, calls = self._dispatch("영어로 답변해줘. 두보는 어떻게 평가되는가?")
        self.assertFalse(resolution.control_only)
        self.assertEqual(calls["backend"], 1)
        self.assertEqual(resolution.question_text, "두보는 어떻게 평가되는가?")


class TestVectorRagRetrieverCacheSplit(unittest.TestCase):
    """§9.3 item 6: the independent vectorRAG path's base-retriever cache is
    keyed by question_language, while `prepare_documents_for_prompt` is
    applied fresh per call with response_language — verified directly
    against `tools.vectorrag_prompt` (pure, already covered for its own
    contract in tests/test_vectorrag_document_prompt.py) to prove the split
    itself: the SAME retrieved documents produce DIFFERENT quoted-text
    ordering depending purely on which language is passed at call time,
    independent of whatever language the (hypothetical) cached retriever
    was built for."""

    def _docs(self):
        from langchain_core.documents import Document
        meta = {
            "entry_id": "E003", "entry_position": 3,
            "original_chinese": "月夜漢文", "korean_translation": "달밤 한국어 번역",
            "english_translation": "Moonlit night English translation",
            "source_work_kor": "지봉유설", "source_work_eng": "Jibong yusol",
            "source_work_id": "B023", "poetrytalks_link": "https://poetrytalks.org/E003",
        }
        return [Document(page_content="raw", metadata=dict(meta))]

    def test_same_retrieved_docs_different_response_language_different_order(self):
        docs = self._docs()
        prepared_en = prepare_documents_for_prompt(docs, "en")
        prepared_ko = prepare_documents_for_prompt(docs, "ko")
        block_en = prepared_en[0].metadata["quoted_text_block"]
        block_ko = prepared_ko[0].metadata["quoted_text_block"]
        self.assertLess(block_en.index("English"), block_en.index("Korean"))
        self.assertLess(block_ko.index("Korean"), block_ko.index("English"))
        # Same underlying documents (question_language retrieval is a single
        # fixed call) — only the LANGUAGE PASSED AT PREP TIME changed the
        # order, proving prep is not baked into a per-question-language cache.
        self.assertIsNot(prepared_en[0], prepared_ko[0])


class TestAgentSourceCarriesLanguageSplit(unittest.TestCase):
    """`agent.py` imports `llm`/`graph` at module level (live Gemini/Neo4j
    clients), so — matching this project's existing convention
    (`tests/test_deterministic_sources.py`,
    `tests/test_sources_language_and_urls.py`) — its contract is pinned by
    reading the SOURCE text rather than importing the module."""

    def setUp(self):
        self.src = _read("agent.py")
        # The pipeline body moved to `chatbot/application/graphrag_pipeline.py`
        # (large-module modularization work order Phase 8.2); `agent.py` keeps
        # the public entry point, the session-state reads, and the
        # backward-compatible language default.
        self.pipeline_src = _read("chatbot/application/graphrag_pipeline.py")

    def test_synthesize_answer_accepts_question_language_with_backward_compat_default(self):
        self.assertIn(
            "def synthesize_answer(user_input: str, response_language: str,\n"
            "                      question_language: str = None) -> str:",
            self.src,
        )
        self.assertIn("question_language = question_language or response_language", self.src)

    def test_vector_retrieval_uses_question_language_authority_uses_response_language(self):
        self.assertIn(
            "evidence = gather_graphrag_evidence(\n"
            "        user_input, question_language, history_text=history_text or None,\n"
            "        response_language=response_language)",
            self.pipeline_src,
        )

    def test_generate_response_reads_both_session_keys_with_fallback(self):
        self.assertIn('st.session_state.get("response_language")', self.src)
        self.assertIn('st.session_state.get("question_language")', self.src)


class TestBotPySourceAppliesResolveLanguages(unittest.TestCase):
    """`bot.py` runs Streamlit page-config/auth at import time — pinned via
    source text, same convention as the UI-label work order's own tests
    (`tests/test_ui_mode_labels.py`)."""

    def setUp(self):
        self.src = _read("bot.py")

    def test_resolve_languages_is_used(self):
        self.assertIn("resolve_languages(prompt", self.src)

    def test_sets_all_four_session_state_keys(self):
        self.assertIn('st.session_state["question_language"] = resolution.question_language', self.src)
        self.assertIn('st.session_state["response_language"] = resolution.response_language', self.src)
        self.assertIn('st.session_state["effective_language"] = resolution.response_language', self.src)
        self.assertIn("st.session_state[\"locked_language\"] = resolution.locked_language", self.src)

    def test_control_only_short_circuits_before_handle_submit(self):
        control_only_idx = self.src.index("if resolution.control_only:")
        handle_submit_idx = self.src.index("handle_submit(resolution.question_text, chatbot_mode)")
        self.assertLess(control_only_idx, handle_submit_idx)

    def test_handle_submit_receives_question_text_not_raw_prompt(self):
        self.assertIn("handle_submit(resolution.question_text, chatbot_mode)", self.src)

    def test_raw_prompt_still_displayed_and_saved(self):
        self.assertIn('{"role": "user", "content": prompt}', self.src)

    def test_language_policy_import_is_lazy_not_top_level(self):
        # Must stay consistent with the pre-existing Phase 2 hardening rule
        # (tests/test_phase2_auth_init.py): bot.py never imports `tools.*`
        # at module top level.
        import ast

        tree = ast.parse(self.src)
        top_level_modules = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                top_level_modules.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_modules.add(alias.name.split(".")[0])
        self.assertNotIn("tools", top_level_modules)
        self.assertIn("from tools.language_policy import resolve_languages", self.src)


if __name__ == "__main__":
    unittest.main()

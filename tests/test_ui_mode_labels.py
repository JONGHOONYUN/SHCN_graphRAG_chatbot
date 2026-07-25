"""CLAUDE_CODE_TEXTRAG_UI_LABEL_TO_VECTORRAG.md — frontend "textRAG" label
renamed to "vectorRAG" for the user-facing UI only. Internal mode keys,
`messages_by_mode` keys, and the Neo4j history suffix stay "textRAG".

`bot.py` runs `st.set_page_config(...)` and password-auth code (including
`st.stop()`) at import time, so — matching the existing pattern elsewhere in
this suite (`open("agent.py", encoding="utf-8").read()`) — its user-facing
strings and internal-identifier contracts are checked by reading the source
as UTF-8 text, never by importing the module. `mode_labels.py` has no
Streamlit/Neo4j/LLM side effects, so it is imported and called directly.
"""

import os
import unittest

from mode_labels import MODE_DISPLAY_LABELS, mode_display_label

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


class TestModeDisplayLabelMapping(unittest.TestCase):
    def test_graphrag_label_unchanged(self):
        self.assertEqual(mode_display_label("graphRAG"), "graphRAG")

    def test_textrag_displays_as_vectorrag(self):
        self.assertEqual(mode_display_label("textRAG"), "vectorRAG")

    def test_unknown_mode_passes_through(self):
        self.assertEqual(mode_display_label("unknown"), "unknown")

    def test_registry_has_exactly_the_two_known_modes(self):
        self.assertEqual(MODE_DISPLAY_LABELS,
                         {"graphRAG": "graphRAG", "textRAG": "vectorRAG"})


class TestBotPyUserFacingStringsUseVectorRag(unittest.TestCase):
    def setUp(self):
        self.src = _read("bot.py")

    def test_sidebar_off_help_text_says_vectorrag(self):
        self.assertIn("꺼짐 (vectorRAG)", self.src)

    def test_sidebar_off_help_text_no_longer_says_textrag(self):
        self.assertNotIn("꺼짐 (textRAG)", self.src)

    def test_greeting_says_vectorrag_mode(self):
        self.assertIn("— vectorRAG 모드", self.src)

    def test_greeting_no_longer_says_textrag_mode(self):
        self.assertNotIn("— textRAG 모드", self.src)

    def test_current_mode_caption_uses_display_label_function(self):
        self.assertIn("mode_display_label(chatbot_mode)", self.src)
        # The caption must not interpolate the raw internal value directly.
        self.assertNotIn('f"**현재 모드**: `{chatbot_mode}`', self.src)

    def test_graphrag_user_facing_label_unchanged(self):
        self.assertIn('"graphRAG 모드"', self.src)
        self.assertIn("— graphRAG 모드**입니다", self.src)
        self.assertIn("켜짐 (graphRAG)", self.src)


class TestBotPyInternalIdentifiersUnchanged(unittest.TestCase):
    """Work order §4 — a plain global find/replace must NOT touch these."""

    def setUp(self):
        self.src = _read("bot.py")

    def test_internal_mode_key_still_textrag(self):
        self.assertIn(
            'chatbot_mode = "graphRAG" if is_graphrag else "textRAG"',
            self.src)

    def test_messages_by_mode_key_still_textrag(self):
        self.assertIn('"textRAG": [GREETING_TEXTRAG]', self.src)
        self.assertIn('"graphRAG": [GREETING_GRAPHRAG]', self.src)

    def test_greeting_variable_name_unchanged(self):
        self.assertIn("GREETING_TEXTRAG = {", self.src)

    def test_graphrag_routes_to_agent(self):
        self.assertIn('if mode == "graphRAG":', self.src)
        self.assertIn("from agent import generate_response", self.src)

    def test_non_graphrag_routes_to_text_rag_backend(self):
        self.assertIn("from text_rag import generate_text_rag_response",
                      self.src)

    def test_mode_display_label_imported_from_dedicated_module(self):
        self.assertIn("from mode_labels import mode_display_label", self.src)


class TestTextRagModuleUnchangedByThisWorkOrder(unittest.TestCase):
    """Work order §4.3/§4.4 — backend file/function names and the Neo4j
    history suffix must survive this UI-only relabeling untouched."""

    def setUp(self):
        self.src = _read("text_rag.py")

    def test_session_suffix_still_textrag(self):
        self.assertIn('f"{get_session_id()}::textRAG"', self.src)

    def test_entry_point_function_name_unchanged(self):
        self.assertIn("def generate_text_rag_response(", self.src)

    def test_no_vectorrag_leaked_into_backend_module(self):
        # This work order is UI-label-only; the backend module itself should
        # not have been touched at all.
        self.assertNotIn("vectorRAG", self.src)


if __name__ == "__main__":
    unittest.main()

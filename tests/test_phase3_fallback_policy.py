"""Phase 3 — Exception taxonomy and fallback policy.

Verifies:
  * ReAct fallback fires ONLY for `TransientProviderError`.
  * Configuration errors, unsafe query errors, coding-defect exceptions do
    NOT trigger fallback (they would otherwise be silently retried and mask
    the real cause).
  * `no_results` (empty synthesis output) never falls back.
  * Every error path emits exactly one correlation id in the log.
"""

import logging
import unittest
from unittest.mock import patch

from errors import (
    ChatbotError,
    ConfigurationError,
    ModelResponseError,
    RetrievalError,
    TransientProviderError,
    UnsafeQueryError,
    is_fallback_eligible,
)


class TestFallbackEligibility(unittest.TestCase):
    def test_transient_only(self):
        self.assertTrue(is_fallback_eligible(TransientProviderError("x")))

    def test_configuration_never(self):
        self.assertFalse(is_fallback_eligible(ConfigurationError("x")))

    def test_unsafe_query_never(self):
        self.assertFalse(is_fallback_eligible(UnsafeQueryError("x")))

    def test_retrieval_never(self):
        self.assertFalse(is_fallback_eligible(RetrievalError("x")))

    def test_model_response_never(self):
        # Retry policy for ModelResponseError is caller-owned; it must never
        # implicitly trigger the ReAct fallback.
        self.assertFalse(is_fallback_eligible(ModelResponseError("x")))

    def test_generic_defects_never(self):
        self.assertFalse(is_fallback_eligible(TypeError("bad call")))
        self.assertFalse(is_fallback_eligible(AttributeError("nope")))
        self.assertFalse(is_fallback_eligible(KeyError("missing")))

    def test_interrupts_never(self):
        self.assertFalse(is_fallback_eligible(KeyboardInterrupt()))
        self.assertFalse(is_fallback_eligible(SystemExit()))


class TestChatbotErrorCarriesCorrelationId(unittest.TestCase):
    def test_correlation_id_defaults_to_hex_slice(self):
        e = ChatbotError("hello")
        self.assertIsInstance(e.correlation_id, str)
        self.assertEqual(len(e.correlation_id), 8)

    def test_correlation_id_can_be_supplied(self):
        e = ChatbotError("x", correlation_id="12345678")
        self.assertEqual(e.correlation_id, "12345678")


class TestAgentGenerateResponseFallbackPolicy(unittest.TestCase):
    """Fallback policy is enforced in `agent.generate_response`. The test
    stubs the streamlit session_state so we can drive the function directly
    without a live browser."""

    def _import_agent(self):
        # Patch `st.session_state` on the imported streamlit module because
        # `agent.generate_response` reads `st.session_state.get(...)`.
        import streamlit as st
        import agent
        return st, agent

    def test_transient_triggers_react_fallback(self):
        st, agent = self._import_agent()
        with patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer",
                          side_effect=TransientProviderError("provider down")) as syn, \
             patch.object(agent, "_generate_response_react",
                          return_value="react-answer") as react:
            out = agent.generate_response("hi")
        self.assertEqual(out, "react-answer")
        syn.assert_called_once()
        react.assert_called_once()

    def test_configuration_error_no_fallback(self):
        st, agent = self._import_agent()
        with patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer",
                          side_effect=ConfigurationError("secret missing")), \
             patch.object(agent, "_generate_response_react") as react:
            out = agent.generate_response("hi")
        react.assert_not_called()
        self.assertIn("search", out.lower())  # localized safe message

    def test_unsafe_query_error_no_fallback(self):
        st, agent = self._import_agent()
        from tools.cypher_safety import UnsafeCypherError

        with patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer",
                          side_effect=UnsafeCypherError("forbidden keyword")), \
             patch.object(agent, "_generate_response_react") as react:
            out = agent.generate_response("hi")
        react.assert_not_called()
        self.assertTrue(out)

    def test_typeerror_no_fallback(self):
        """A TypeError inside the pipeline is a coding defect. The Phase 3
        rule is: do NOT fall back to ReAct — that would hide the bug forever
        and double external calls."""
        st, agent = self._import_agent()
        with patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer",
                          side_effect=TypeError("bad call")), \
             patch.object(agent, "_generate_response_react") as react:
            out = agent.generate_response("hi")
        react.assert_not_called()
        self.assertTrue(out)

    def test_empty_output_no_fallback(self):
        """Empty output from synthesis is `no_results`, not a failure. It
        must not trigger the ReAct fallback (Phase 3 §4)."""
        st, agent = self._import_agent()
        with patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer", return_value="   "), \
             patch.object(agent, "_generate_response_react") as react:
            out = agent.generate_response("hi")
        react.assert_not_called()
        self.assertTrue(out)

    def test_gemini_empty_stream_no_fallback(self):
        st, agent = self._import_agent()
        with patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer",
                          side_effect=ValueError("No generation chunks were returned")), \
             patch.object(agent, "_generate_response_react") as react:
            out = agent.generate_response("hi")
        react.assert_not_called()
        self.assertTrue(out)


class TestErrorLoggingHasCorrelationId(unittest.TestCase):
    def test_config_error_path_logs_correlation_id(self):
        import streamlit as st
        import agent

        with self.assertLogs("agent", level="ERROR") as cap, \
             patch.dict(st.session_state, {"effective_language": "en"}, clear=False), \
             patch.object(agent, "synthesize_answer",
                          side_effect=ConfigurationError("no key",
                                                        correlation_id="cfg12345")):
            agent.generate_response("hi")
        combined = "\n".join(cap.output)
        self.assertIn("cfg12345", combined)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()

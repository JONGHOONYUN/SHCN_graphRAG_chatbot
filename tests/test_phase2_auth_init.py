"""Phase 2 — Post-auth lazy init and safe session_id.

Verifies the work-order guarantees that unauthenticated / test / CLI contexts
never trigger Gemini or Neo4j client creation.
"""

import ast
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent


class TestBotDefersBackendImports(unittest.TestCase):
    """`bot.py` must NOT import `agent`, `text_rag`, `llm`, `graph`, or any
    `tools.*` module at top level. Those modules open network resources at
    import time, and Phase 2 forbids pre-auth network I/O."""

    FORBIDDEN_ROOTS = {"agent", "text_rag", "llm", "graph", "tools"}

    def test_bot_top_level_imports(self):
        source = (REPO / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_imports = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_level_imports.add(node.module.split(".")[0])
        offenders = top_level_imports & self.FORBIDDEN_ROOTS
        self.assertFalse(
            offenders,
            f"bot.py top-level imports {offenders} — these must be deferred "
            "into handle_submit() so unauthenticated users trigger no "
            "backend init.",
        )


class TestSessionIdFallback(unittest.TestCase):
    """`utils.get_session_id()` must never raise, even when
    `get_script_run_ctx()` returns None (tests / CLI)."""

    def test_returns_fallback_when_ctx_missing(self):
        # Force reload to reset the fallback UUID captured at import.
        if "utils" in sys.modules:
            importlib.reload(sys.modules["utils"])
        import utils

        with patch("utils.get_script_run_ctx", return_value=None):
            sid = utils.get_session_id()
        self.assertIsInstance(sid, str)
        self.assertGreater(len(sid), 4)
        self.assertTrue(sid.startswith("fallback-"))

    def test_returns_real_session_id_when_present(self):
        import utils

        class _Ctx:
            session_id = "abc-123-real"

        with patch("utils.get_script_run_ctx", return_value=_Ctx()):
            self.assertEqual(utils.get_session_id(), "abc-123-real")

    def test_returns_fallback_when_ctx_missing_attribute(self):
        import utils

        class _Ctx:  # no session_id attribute
            pass

        with patch("utils.get_script_run_ctx", return_value=_Ctx()):
            sid = utils.get_session_id()
        self.assertTrue(sid.startswith("fallback-"))


class TestPasswordCheckUsesConstantTime(unittest.TestCase):
    """Sanity: bot.py's check_password must use `hmac.compare_digest`, not
    Python `==` on the raw secret. AST scan catches drift without importing
    the streamlit-dependent module."""

    def test_uses_hmac_compare_digest(self):
        source = (REPO / "bot.py").read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest", source)
        self.assertNotIn(
            "st.secrets[\"APP_PASSWORD\"]) == ",  # naive == on raw secret
            source,
            "bot.py should not compare APP_PASSWORD with a bare `==` — use "
            "hmac.compare_digest to avoid timing side-channels.",
        )


if __name__ == "__main__":
    unittest.main()

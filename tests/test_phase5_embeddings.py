"""Phase 5 — Embedding retry policy, validation, and dimension checks.

Because `llm.py` reads Streamlit secrets at import time and constructs a live
embedding client, these tests import the `GoogleEmbeddings` class *directly*
from a fresh submodule and never invoke module top-level.
"""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _load_class():
    """Import the class without triggering module-level st.secrets access."""
    # Provide fake secrets so import-time client construction succeeds.
    import streamlit as st
    try:
        st.secrets  # touch to ensure attribute exists
    except Exception:
        pass
    fakes = {
        "GOOGLE_API_KEY": "test-key",
        "GOOGLE_MODEL": "models/gemini-2.5-flash",
        "GOOGLE_EMBEDDING_MODEL": "models/gemini-embedding-001",
    }
    with patch.object(st, "secrets", MagicMock(**{
        "__getitem__.side_effect": fakes.get,
        "get.side_effect": lambda k, d=None: fakes.get(k, d),
    })):
        # ChatGoogleGenerativeAI would also validate the API key; patch it out
        # so import doesn't reach the network.
        with patch("langchain_google_genai.ChatGoogleGenerativeAI",
                   return_value=MagicMock()):
            if "llm" in importlib.sys.modules:
                importlib.reload(importlib.sys.modules["llm"])
            else:
                import llm  # noqa
    import llm as llm_module
    return llm_module.GoogleEmbeddings


class TestModelPinning(unittest.TestCase):
    def test_explicit_model_overrides_secrets(self):
        GoogleEmbeddings = _load_class()
        client = GoogleEmbeddings(api_key="x", model="models/custom")
        self.assertEqual(client.model, "models/custom")
        self.assertIn("models/custom:embedContent", client.embed_url)

    def test_missing_api_key_is_configuration_error(self):
        GoogleEmbeddings = _load_class()
        from errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            GoogleEmbeddings(api_key="", model="models/x")


class TestEmbedDocumentsEmptyInput(unittest.TestCase):
    def test_empty_returns_empty_no_network(self):
        GoogleEmbeddings = _load_class()
        client = GoogleEmbeddings(api_key="k", model="models/x",
                                  session=MagicMock())
        result = client.embed_documents([])
        self.assertEqual(result, [])
        client._session.post.assert_not_called()


class TestResponseValidation(unittest.TestCase):
    def _client(self, response):
        GoogleEmbeddings = _load_class()
        session = MagicMock()
        session.post.return_value = response
        return GoogleEmbeddings(api_key="k", model="models/x", session=session)

    @staticmethod
    def _resp(status=200, ctype="application/json", body=None,
              headers=None):
        r = SimpleNamespace()
        r.status_code = status
        r.headers = {"content-type": ctype}
        if headers:
            r.headers.update(headers)
        r.json = MagicMock(return_value=body if body is not None else {})
        r.raise_for_status = MagicMock()
        return r

    def test_non_json_content_type_rejected(self):
        client = self._client(self._resp(ctype="text/html", body={}))
        from errors import ModelResponseError
        with self.assertRaises(ModelResponseError):
            client.embed_query("hi")

    def test_missing_embedding_key_rejected(self):
        client = self._client(self._resp(body={"not_embedding": {}}))
        from errors import ModelResponseError
        with self.assertRaises(ModelResponseError):
            client.embed_query("hi")

    def test_empty_vector_rejected(self):
        client = self._client(self._resp(body={"embedding": {"values": []}}))
        from errors import ModelResponseError
        with self.assertRaises(ModelResponseError):
            client.embed_query("hi")

    def test_non_numeric_vector_rejected(self):
        client = self._client(self._resp(
            body={"embedding": {"values": [1.0, "not-a-number", 2.0]}}))
        from errors import ModelResponseError
        with self.assertRaises(ModelResponseError):
            client.embed_query("hi")

    def test_batch_count_mismatch_rejected(self):
        client = self._client(self._resp(body={"embeddings": [
            {"values": [1.0, 2.0]},
        ]}))
        # sending 3 texts but server returned 1 → mismatch
        from errors import ModelResponseError
        with self.assertRaises(ModelResponseError):
            client.embed_documents(["a", "b", "c"])


class TestRetryPolicy(unittest.TestCase):
    """429 and 5xx retry with Retry-After honored; 4xx never retried."""

    def _client(self, responses, sleeps=None):
        GoogleEmbeddings = _load_class()
        session = MagicMock()
        session.post.side_effect = responses
        client = GoogleEmbeddings(api_key="k", model="models/x", session=session)
        if sleeps is not None:
            sleeps.clear()
            def _sleep(d): sleeps.append(d)
            client._sleeper = _sleep  # unused but keeps API tidy
        return client, session

    @staticmethod
    def _resp(status, body=None, headers=None):
        r = SimpleNamespace()
        r.status_code = status
        r.headers = {"content-type": "application/json"}
        if headers:
            r.headers.update(headers)
        r.json = MagicMock(return_value=body if body is not None else {})
        r.raise_for_status = MagicMock()
        return r

    def test_400_is_configuration_error_no_retry(self):
        client, session = self._client([self._resp(400)])
        from errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            client.embed_query("hi")
        self.assertEqual(session.post.call_count, 1)

    def test_401_is_configuration_error_no_retry(self):
        client, session = self._client([self._resp(401)])
        from errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            client.embed_query("hi")
        self.assertEqual(session.post.call_count, 1)

    def test_403_is_configuration_error_no_retry(self):
        client, session = self._client([self._resp(403)])
        from errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            client.embed_query("hi")
        self.assertEqual(session.post.call_count, 1)

    def test_429_retries_and_eventually_succeeds(self):
        good = self._resp(200, body={
            "embedding": {"values": [0.1, 0.2, 0.3]}})
        client, session = self._client([
            self._resp(429, headers={"Retry-After": "0"}),
            self._resp(429, headers={"Retry-After": "0"}),
            good,
        ])
        with patch("time.sleep") as ts:
            v = client.embed_query("hi")
        self.assertEqual(len(v), 3)
        self.assertEqual(session.post.call_count, 3)
        # Sleeps invoked between attempts (2 retries → 2 sleeps).
        self.assertEqual(ts.call_count, 2)

    def test_retry_after_header_honored(self):
        good = self._resp(200, body={
            "embedding": {"values": [0.1]}})
        client, session = self._client([
            self._resp(429, headers={"Retry-After": "0.05"}),
            good,
        ])
        recorded = []
        with patch("time.sleep", side_effect=lambda d: recorded.append(d)):
            client.embed_query("hi")
        self.assertEqual(recorded, [0.05])   # exact value from header

    def test_retry_budget_exhausted(self):
        client, session = self._client([
            self._resp(503) for _ in range(5)
        ])
        from errors import TransientProviderError
        with patch("time.sleep"):
            with self.assertRaises(TransientProviderError):
                client.embed_query("hi")
        # Retries capped at _MAX_RETRIES.
        import llm as llm_module
        self.assertEqual(session.post.call_count, llm_module._MAX_RETRIES)


class TestDimensionCheck(unittest.TestCase):
    def test_explicit_dim_mismatch_raises(self):
        GoogleEmbeddings = _load_class()
        session = MagicMock()
        session.post.return_value = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            json=MagicMock(return_value={"embedding": {"values": [1, 2, 3]}}),
            raise_for_status=MagicMock(),
        )
        client = GoogleEmbeddings(api_key="k", model="models/x",
                                  expected_dim=768, session=session)
        from errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            client.embed_query("hi")

    def test_first_response_pins_dimension(self):
        GoogleEmbeddings = _load_class()
        session = MagicMock()

        def responses():
            r1 = SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                json=MagicMock(return_value={"embedding": {"values": [1, 2, 3]}}),
                raise_for_status=MagicMock(),
            )
            r2 = SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                json=MagicMock(return_value={"embedding": {"values": [4, 5]}}),
                raise_for_status=MagicMock(),
            )
            yield r1
            yield r2
        session.post.side_effect = responses()
        client = GoogleEmbeddings(api_key="k", model="models/x", session=session)
        client.embed_query("hi")
        self.assertEqual(client.expected_dim, 3)
        from errors import ConfigurationError
        with self.assertRaises(ConfigurationError):
            client.embed_query("hi again")   # dim=2 now, must reject


if __name__ == "__main__":
    unittest.main()

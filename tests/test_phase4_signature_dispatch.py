"""Phase 4 — Introspection-based arity dispatch.

Guarantees:
  1. Canonical 3-arg retriever is called exactly once.
  2. Legacy 2-arg retriever is called exactly once (no arity retry).
  3. A `TypeError` raised INSIDE the retriever body is not swallowed and
     retried — it becomes a retrieval failure (correlation id logged).
  4. Fetcher with 4-arg canonical signature invoked exactly once.
  5. Fetcher with 3-arg legacy signature invoked exactly once.
  6. Side-effectful fetcher whose body raises TypeError is invoked exactly
     once (no duplicate external call).
"""

import unittest

from tools.evidence import Evidence
from tools.orchestrator import (
    _call_fetcher,
    _fn_accepts_arity,
    _safe_retrieve,
    gather_graphrag_evidence,
)


class TestAccessArity(unittest.TestCase):
    def test_3arg_fn(self):
        def fn(a, b, c=None): return None
        self.assertTrue(_fn_accepts_arity(fn, 3))
        self.assertTrue(_fn_accepts_arity(fn, 2))
        self.assertFalse(_fn_accepts_arity(fn, 4))

    def test_2arg_fn(self):
        def fn(a, b): return None
        self.assertTrue(_fn_accepts_arity(fn, 2))
        self.assertFalse(_fn_accepts_arity(fn, 3))

    def test_var_positional_accepts_all(self):
        def fn(*args, **kw): return None
        self.assertTrue(_fn_accepts_arity(fn, 0))
        self.assertTrue(_fn_accepts_arity(fn, 5))


class TestSafeRetrieveDispatch(unittest.TestCase):
    def test_canonical_3arg_called_once(self):
        calls = []

        def r(q, l, history_text=None):
            calls.append((q, l, history_text))
            return Evidence(kind="graph")

        ev, status = _safe_retrieve(r, "q", "en", "hist", "graph")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("q", "en", "hist"))
        self.assertEqual(status["outcome"], "no_results")

    def test_legacy_2arg_called_once_no_retry(self):
        calls = []

        def r(q, l):
            calls.append((q, l))
            return Evidence(kind="vector")

        ev, status = _safe_retrieve(r, "q", "en", "hist", "vector")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("q", "en"))

    def test_typeerror_from_body_is_not_retried(self):
        """A TypeError raised inside the retriever must NOT trigger a 2-arg
        retry — that would silently double external side effects and mask
        real bugs. It becomes a retrieval failure instead."""
        calls = []

        def r(q, l, history_text=None):
            calls.append(1)
            raise TypeError("something inside the retriever broke")

        ev, status = _safe_retrieve(r, "q", "en", "hist", "graph")
        self.assertEqual(len(calls), 1)  # exactly one call, no retry
        self.assertEqual(status["outcome"], "temporarily_unavailable")

    def test_incompatible_signature_reports_unavailable(self):
        def r(a): return Evidence(kind="graph")   # arity 1 only

        ev, status = _safe_retrieve(r, "q", "en", "hist", "graph")
        self.assertEqual(status["outcome"], "temporarily_unavailable")


class TestCallFetcherDispatch(unittest.TestCase):
    def test_canonical_4arg(self):
        calls = []

        def f(s, i, lang, node_type):
            calls.append((s, i, lang, node_type))
            return {"ok": True}

        result = _call_fetcher(f, "wikidata", "Q1", "en", "Person")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3], "Person")

    def test_legacy_3arg(self):
        calls = []

        def f(s, i, lang):
            calls.append((s, i, lang))
            return {"ok": True}

        result = _call_fetcher(f, "wikidata", "Q1", "en", "Person")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)

    def test_typeerror_from_body_not_retried(self):
        """Under the OLD code a TypeError raised inside the body triggered a
        second, 3-arg call — doubling network side effects. Phase 4 ends
        that: the exception propagates untouched, and side-effect counters
        stay at exactly 1."""
        calls = []

        def f(s, i, lang, node_type="Person"):
            calls.append(1)
            raise TypeError("body raised")

        with self.assertRaises(TypeError):
            _call_fetcher(f, "wikidata", "Q1", "en", "Person")
        self.assertEqual(len(calls), 1)

    def test_incompatible_signature_raises_typeerror(self):
        def f(a): return {}

        with self.assertRaises(TypeError):
            _call_fetcher(f, "wikidata", "Q1", "en", "Person")


class TestNoDoubleCallFromOrchestrator(unittest.TestCase):
    """End-to-end: `gather_graphrag_evidence` never invokes an injected
    retriever twice, even when the retriever raises inside its body."""

    def test_retriever_typeerror_not_doubled(self):
        graph_calls = []
        vector_calls = []

        def graph_r(q, l, history_text=None):
            graph_calls.append(1)
            raise TypeError("intentional")

        def vector_r(q, l):
            vector_calls.append(1)
            return Evidence(kind="vector")

        result = gather_graphrag_evidence(
            "q", "en", graph_retriever=graph_r, vector_retriever=vector_r,
            authority_fetcher=lambda *a, **kw: {"status": "ok"},
        )
        self.assertEqual(len(graph_calls), 1)
        self.assertEqual(len(vector_calls), 1)
        self.assertEqual(result["statuses"]["graph"]["outcome"],
                         "temporarily_unavailable")


if __name__ == "__main__":
    unittest.main()

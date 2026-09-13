"""Large-module modularization — structural contracts.

Guards the boundaries the modularization work order introduced, so a later
change cannot quietly undo them:

  * every legacy `tools.*` import path still resolves, and resolves to the
    SAME object as the new implementation (no duplicated logic/registries);
  * the pure layers (domain / synthesis / authority registry+parsers) import
    with ZERO streamlit / neo4j / langchain / requests / gemini imports, so
    they stay testable without secrets or a network;
  * declared layer dependency directions hold (AST-checked);
  * no import cycles across the new package;
  * call parity for the normal graphRAG pipeline: one synthesis LLM call,
    graph → vector → authority order, and history written exactly once on
    success / never on the short-circuit path.
"""

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _run_probe(code: str) -> str:
    """Execute `code` in a FRESH interpreter rooted at the repo, returning
    stdout. Used for import-side-effect checks, which are meaningless in a
    process where the test suite already imported everything."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                          capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        raise AssertionError(f"probe failed:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


# ── (a) Facade compatibility: same object, not a copy ───────────────────────
class TestFacadesReExportSingleImplementation(unittest.TestCase):
    """`tools/*.py` must be thin facades: every re-exported symbol IS the
    implementation object. A copy would mean two sources of truth."""

    def test_evidence_facade_identity(self):
        import tools.evidence as facade
        from chatbot.domain import evidence_merge, evidence_models, node_identity
        from chatbot.retrieval import graph_rows, vector_documents

        for name, mod in (
            ("Evidence", evidence_models), ("Entity", evidence_models),
            ("Provenance", evidence_models), ("NodeReference", evidence_models),
            ("make_node_reference", evidence_models),
            ("merge_node_references", evidence_models),
            ("POETRYTALKS_BASE_URL", node_identity),
            ("NODE_ID_PREFIXES", node_identity),
            ("is_valid_node_id", node_identity), ("poetrytalks_url", node_identity),
            ("merge_entities", evidence_merge), ("collect_entities", evidence_merge),
            ("collect_node_references", evidence_merge),
            ("docs_to_evidence", vector_documents),
            ("document_to_parts", vector_documents),
            ("graph_rows_to_evidence", graph_rows),
            ("entities_from_graph_row", graph_rows),
        ):
            self.assertIs(getattr(facade, name), getattr(mod, name), name)

    def test_synthesis_facade_identity(self):
        import tools.synthesis as facade
        from chatbot.synthesis import (
            citations, evidence_format, history_format, language_fields,
            source_policy,
        )

        for name, mod in (
            ("format_evidence_for_prompt", evidence_format),
            ("both_retrievals_failed", evidence_format),
            ("retrieval_failure_message", evidence_format),
            ("RETRIEVAL_STATUS_MESSAGES", evidence_format),
            ("build_citations", citations), ("CITATION_LABELS", citations),
            ("_format_citation_name", citations),
            ("serialize_chat_history", history_format),
            ("HISTORY_RULES", history_format),
            ("SYNTHESIS_SYSTEM_RULES", source_policy),
            ("source_text_priority", language_fields),
            ("reorder_source_text_fields", language_fields),
        ):
            self.assertIs(getattr(facade, name), getattr(mod, name), name)

    def test_authority_facade_identity_and_single_registry(self):
        import tools.external_authority as facade
        from chatbot.authority import cache, client, parsers, registry, service

        self.assertIs(facade.AUTHORITY_REGISTRY, registry.AUTHORITY_REGISTRY)
        self.assertIs(facade._authority_cache, cache._authority_cache)
        self.assertIs(facade.fetch_authority, service.fetch_authority)
        self.assertIs(facade.parse_wikidata, parsers.parse_wikidata)
        self.assertIs(facade._fetch, client._fetch)
        self.assertIs(facade.sources_for_node_type, registry.sources_for_node_type)

    def test_orchestrator_facade_identity(self):
        import tools.orchestrator as facade
        from chatbot.application import (
            authority_enrichment, evidence_orchestrator, retrieval_policy,
            retriever_invocation,
        )

        self.assertIs(facade.gather_graphrag_evidence,
                      evidence_orchestrator.gather_graphrag_evidence)
        self.assertIs(facade.authority_intent, retrieval_policy.authority_intent)
        self.assertIs(facade.needs_authority, retrieval_policy.needs_authority)
        self.assertIs(facade._safe_retrieve, retriever_invocation._safe_retrieve)
        self.assertIs(facade._fn_accepts_arity,
                      retriever_invocation._fn_accepts_arity)
        self.assertIs(facade._enrich_entity, authority_enrichment._enrich_entity)

    def test_legacy_public_import_paths_still_resolve(self):
        """Every symbol the work order's compatibility list names must still be
        importable from its original module (the streamlit/neo4j-bound ones are
        checked by source, since importing them needs live secrets)."""
        for mod, names in (
            ("tools.evidence", ("Evidence", "Entity", "Provenance", "NodeReference")),
            ("tools.synthesis", ("format_evidence_for_prompt", "build_citations")),
            ("tools.orchestrator", ("gather_graphrag_evidence",)),
            ("tools.answer_renderer", ("assemble_final_answer",)),
            ("tools.external_authority", ("fetch_authority", "external_authority_lookup")),
        ):
            imported = __import__(mod, fromlist=list(names))
            for n in names:
                self.assertTrue(hasattr(imported, n), f"{mod}.{n}")

    def test_secret_bound_facades_define_their_public_entry_points(self):
        for rel, names in (
            ("tools/cypher.py", ("def cypher_qa_safe(", "def retrieve_graph_evidence(",
                                 "def retrieve_role_ranking_evidence(")),
            ("tools/vector.py", ("def get_poetry_plot(", "def retrieve_sihwa_evidence(")),
            ("agent.py", ("def generate_response(", "def synthesize_answer(")),
            ("text_rag.py", ("def generate_text_rag_response(",)),
        ):
            src = (REPO / rel).read_text(encoding="utf-8")
            for n in names:
                self.assertIn(n, src, f"{rel}: {n}")


# ── (b) Import side effects ─────────────────────────────────────────────────
class TestPureLayersImportWithoutInfrastructure(unittest.TestCase):
    """domain / synthesis / authority-registry must import with no streamlit,
    neo4j, langchain, requests or gemini module loaded — i.e. no secrets, no
    connections, no client construction."""

    HEAVY = ("streamlit", "neo4j", "langchain", "langchain_core",
             "langchain_neo4j", "langchain_google_genai", "requests")

    def _loaded_after(self, modules):
        code = (
            "import sys\n"
            + "".join(f"import {m}\n" for m in modules)
            + "print(','.join(sorted(m for m in sys.modules "
              "if m.split('.')[0] in " + repr([h.split('.')[0] for h in self.HEAVY]) + ")))"
        )
        return [m for m in _run_probe(code).split(",") if m]

    def test_domain_layer_is_pure(self):
        self.assertEqual(self._loaded_after([
            "chatbot.domain.node_identity",
            "chatbot.domain.evidence_models",
            "chatbot.domain.evidence_merge",
            "chatbot.domain.retrieval_models",
        ]), [])

    def test_evidence_mappers_are_pure(self):
        self.assertEqual(self._loaded_after([
            "chatbot.retrieval.vector_documents",
            "chatbot.retrieval.graph_rows",
            "chatbot.retrieval.vector_query",
            "chatbot.retrieval.graph_prompt",
        ]), [])

    def test_synthesis_layer_is_pure(self):
        self.assertEqual(self._loaded_after([
            "chatbot.synthesis.language_fields",
            "chatbot.synthesis.source_policy",
            "chatbot.synthesis.history_format",
            "chatbot.synthesis.evidence_format",
            "chatbot.synthesis.citations",
        ]), [])

    def test_authority_registry_and_parsers_are_pure(self):
        """Only `chatbot.authority.client` may pull in `requests`."""
        self.assertEqual(self._loaded_after([
            "chatbot.authority.validators",
            "chatbot.authority.parsers",
            "chatbot.authority.registry",
            "chatbot.authority.cache",
        ]), [])

    def test_legacy_tools_facades_stay_importable_without_secrets(self):
        self.assertEqual(self._loaded_after([
            "tools.evidence", "tools.synthesis",
        ]), [])

    def test_orchestrator_facade_imports_no_client(self):
        """`tools.orchestrator` may import langchain-free helpers only; the
        graph/vector retrievers stay lazily imported so mocked orchestration
        never touches Neo4j/Gemini."""
        loaded = self._loaded_after(["tools.orchestrator"])
        self.assertEqual([m for m in loaded
                          if m.split(".")[0] in ("streamlit", "neo4j",
                                                 "langchain_neo4j",
                                                 "langchain_google_genai")], [])

    def test_no_import_cycles_across_the_package(self):
        """Importing every chatbot submodule in one fresh interpreter must
        succeed — a cycle surfaces as ImportError/AttributeError here."""
        mods = []
        for path in sorted((REPO / "chatbot").rglob("*.py")):
            rel = path.relative_to(REPO).with_suffix("")
            name = ".".join(rel.parts)
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            # vector_retriever / react_* / *_pipeline pull langchain only.
            mods.append(name)
        out = _run_probe(
            "import importlib\n"
            f"mods = {mods!r}\n"
            "for m in mods: importlib.import_module(m)\n"
            "print(len(mods))"
        )
        self.assertEqual(int(out), len(mods))


# ── (c) Declared layer dependency rules (AST) ───────────────────────────────
class TestLayerDependencyDirections(unittest.TestCase):
    FORBIDDEN_ROOTS = ("streamlit", "neo4j", "langchain", "langchain_core",
                       "langchain_neo4j", "langchain_classic",
                       "langchain_google_genai", "requests", "llm", "graph",
                       "bot")

    @staticmethod
    def _imported_roots(path: Path):
        roots = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for a in node.names:
                    roots.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
        return roots

    def test_domain_imports_nothing_infrastructural(self):
        for path in (REPO / "chatbot" / "domain").glob("*.py"):
            roots = self._imported_roots(path)
            for bad in self.FORBIDDEN_ROOTS:
                self.assertNotIn(bad, roots, f"{path.name} imports {bad}")

    def test_synthesis_imports_no_client_or_ui(self):
        for path in (REPO / "chatbot" / "synthesis").glob("*.py"):
            roots = self._imported_roots(path)
            for bad in ("streamlit", "neo4j", "langchain_neo4j",
                        "langchain_google_genai", "requests", "llm", "graph"):
                self.assertNotIn(bad, roots, f"{path.name} imports {bad}")

    def test_only_the_authority_client_imports_requests(self):
        for path in (REPO / "chatbot" / "authority").glob("*.py"):
            roots = self._imported_roots(path)
            if path.name == "client.py":
                self.assertIn("requests", roots)
            else:
                self.assertNotIn("requests", roots, path.name)

    def test_normal_pipeline_never_imports_legacy(self):
        for path in (REPO / "chatbot" / "application").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("chatbot.legacy", src, path.name)
        for path in (REPO / "chatbot" / "retrieval").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("chatbot.legacy", src, path.name)

    def test_retrieval_and_application_do_not_read_session_state(self):
        for sub in ("retrieval", "application"):
            for path in (REPO / "chatbot" / sub).glob("*.py"):
                src = path.read_text(encoding="utf-8")
                self.assertNotIn("st.session_state", src, f"{sub}/{path.name}")

    def test_new_execution_modules_stay_small(self):
        """§11.1 signal: a new execution-logic module creeping back over 500
        lines means a responsibility was not actually split. Static
        prompt/registry modules are exempt (long text, single source)."""
        exempt = {"graph_prompt.py", "react_prompt.py", "source_policy.py",
                  "registry.py", "parsers.py", "vector_query.py",
                  "react_vector_tool.py"}
        for path in sorted((REPO / "chatbot").rglob("*.py")):
            if path.name in exempt:
                continue
            lines = len(path.read_text(encoding="utf-8").splitlines())
            self.assertLess(lines, 500, f"{path.name} is {lines} lines")


# ── (d) Extension seam (§9) ─────────────────────────────────────────────────
class TestExtensionSeam(unittest.TestCase):
    def test_retrieval_request_carries_only_existing_information(self):
        from chatbot.domain.retrieval_models import RetrievalRequest

        req = RetrievalRequest(question="q", question_language="ko",
                               response_language="en")
        self.assertEqual(
            sorted(req.__dataclass_fields__),
            ["history_text", "question", "question_language", "response_language"])
        self.assertIsNone(req.history_text)

    def test_a_new_retriever_needs_no_change_to_graph_or_vector_code(self):
        """Registering an extra source is an application-layer argument, not an
        edit to the existing retrievers."""
        from tools.evidence import Evidence
        from tools.orchestrator import gather_graphrag_evidence

        seen = []

        def extra(question, language, history_text=None):
            seen.append(question)
            return Evidence(kind="graph")

        result = gather_graphrag_evidence(
            "테스트 질문", "ko", graph_retriever=extra,
            vector_retriever=lambda q, l: Evidence(kind="vector"),
            authority_fetcher=lambda *a: {})
        self.assertEqual(seen, ["테스트 질문"])
        self.assertEqual(result["statuses"]["graph"]["outcome"], "no_results")

    def test_block_formatter_dispatch_registers_only_current_sources(self):
        from chatbot.synthesis.evidence_format import (
            EVIDENCE_BLOCK_FORMATTERS, EVIDENCE_BLOCK_ORDER,
        )

        self.assertEqual(sorted(EVIDENCE_BLOCK_FORMATTERS), ["external", "graph", "vector"])
        self.assertEqual(EVIDENCE_BLOCK_ORDER, ("graph", "vector", "external"))

    def test_no_speculative_glossary_or_research_scaffolding(self):
        from tools.evidence import EVIDENCE_KINDS, NODE_ID_PREFIXES

        self.assertEqual(EVIDENCE_KINDS, ("graph", "vector", "external"))
        for speculative in ("Glossary", "ResearchAuthor", "ResearchWork",
                            "ResearchPassage"):
            self.assertNotIn(speculative, NODE_ID_PREFIXES.values())
        names = {p.name for p in (REPO / "chatbot").rglob("*.py")}
        self.assertNotIn("glossary.py", names)
        self.assertNotIn("research.py", names)


# ── (e) Call parity + history ownership for the normal pipeline ─────────────
class _FakeHistory:
    def __init__(self):
        self.messages = []
        self.user_writes = []
        self.ai_writes = []

    def add_user_message(self, m):
        self.user_writes.append(m)

    def add_ai_message(self, m):
        self.ai_writes.append(m)


class _FakeChain:
    def __init__(self, body="본문입니다."):
        self.calls = []
        self._body = body

    def invoke(self, payload):
        self.calls.append(payload)
        return self._body


class TestNormalPipelineCallAndHistoryParity(unittest.TestCase):
    """`chatbot.application.graphrag_pipeline` owns the normal path: exactly
    one synthesis LLM call, and exactly one history write on success."""

    def setUp(self):
        from tools.evidence import Evidence

        self.hist = _FakeHistory()
        self.chain = _FakeChain()
        self.Evidence = Evidence

    def _bundle(self, statuses=None, external_claims=()):
        ev = self.Evidence
        external = ev(kind="external")
        external.claims.extend(external_claims)
        return {
            "graph": ev(kind="graph", documents=[{"person_id": "P553"}]),
            "vector": ev(kind="vector"),
            "external": external,
            "entities": [],
            "statuses": statuses or {"graph": {"outcome": "ok"},
                                     "vector": {"outcome": "ok"}},
            "coverage": {},
        }

    def _run(self, bundle):
        from chatbot.application import graphrag_pipeline as pipeline

        with patch.object(pipeline, "gather_graphrag_evidence",
                          return_value=bundle) as gather:
            out = pipeline.synthesize_answer(
                "질문", "ko", "ko",
                synthesis_chain=self.chain,
                history_factory=lambda _sid: self.hist,
                session_id="sid")
        return out, gather

    def test_success_calls_llm_once_and_writes_history_once(self):
        out, gather = self._run(self._bundle())
        self.assertEqual(len(self.chain.calls), 1)
        gather.assert_called_once()
        self.assertEqual(len(self.hist.user_writes), 1)
        self.assertEqual(len(self.hist.ai_writes), 1)
        # the ASSEMBLED output is persisted, not the raw model body
        self.assertEqual(self.hist.ai_writes[0], out)

    def test_total_retrieval_failure_short_circuits_before_any_llm_call(self):
        bundle = self._bundle(statuses={
            "graph": {"outcome": "temporarily_unavailable"},
            "vector": {"outcome": "temporarily_unavailable"}})
        out, _ = self._run(bundle)
        self.assertEqual(self.chain.calls, [])          # no LLM call
        self.assertEqual(self.hist.user_writes, [])     # no history write
        self.assertIn("일시적으로 사용할 수 없습니다", out)

    def test_external_evidence_keeps_the_answer_alive_when_retrieval_fails(self):
        bundle = self._bundle(
            statuses={"graph": {"outcome": "temporarily_unavailable"},
                      "vector": {"outcome": "temporarily_unavailable"}},
            external_claims=[{"entity": "X", "source": "wikidata",
                              "status": "ok", "url": "https://x",
                              "data": {"primary_name": "X"}}])
        self._run(bundle)
        self.assertEqual(len(self.chain.calls), 1)

    def test_history_is_loaded_bounded_and_passed_to_graph_retrieval_only(self):
        from chatbot.application import graphrag_pipeline as pipeline

        self.hist.messages = [{"role": "user", "content": "황진이에 대해 알려줘"},
                              {"role": "assistant", "content": "황진이는…"}]
        with patch.object(pipeline, "gather_graphrag_evidence",
                          return_value=self._bundle()) as gather:
            pipeline.synthesize_answer(
                "그 인물의 시는?", "ko", "ko",
                synthesis_chain=self.chain,
                history_factory=lambda _sid: self.hist,
                session_id="sid")
        kwargs = gather.call_args.kwargs
        self.assertIn("황진이", kwargs["history_text"])
        self.assertEqual(gather.call_args.args[1], "ko")  # question_language
        self.assertEqual(kwargs["response_language"], "ko")

    def test_history_read_failure_never_blocks_an_answer(self):
        from chatbot.application import graphrag_pipeline as pipeline

        class Boom:
            @property
            def messages(self):
                raise RuntimeError("neo4j down")

            def add_user_message(self, m):
                pass

            def add_ai_message(self, m):
                pass

        with patch.object(pipeline, "gather_graphrag_evidence",
                          return_value=self._bundle()):
            out = pipeline.synthesize_answer(
                "질문", "ko", "ko", synthesis_chain=self.chain,
                history_factory=lambda _sid: Boom(), session_id="sid")
        self.assertTrue(out.strip())


class TestOrchestrationCallOrderParity(unittest.TestCase):
    def test_graph_then_vector_then_authority(self):
        from tools.evidence import Entity, Evidence
        from tools.orchestrator import gather_graphrag_evidence

        order = []

        def graph_r(q, l, h=None):
            order.append("graph")
            ev = Evidence(kind="graph", documents=[{"person_id": "P553"}])
            ev.entities.append(Entity(node_id="P553", node_type="Person",
                                      authority_ids={"wikidata": "Q1"}))
            return ev

        def vector_r(q, l, h=None):
            order.append("vector")
            return Evidence(kind="vector")

        def fetcher(source, ext_id, language, node_type="Person"):
            order.append(f"authority:{source}")
            return {"source": source, "status": "ok", "url": "https://x",
                    "data": {}}

        gather_graphrag_evidence("이규보의 생몰년은?", "ko",
                                 graph_retriever=graph_r,
                                 vector_retriever=vector_r,
                                 authority_fetcher=fetcher)
        self.assertEqual(order[0], "graph")
        self.assertEqual(order[1], "vector")
        self.assertTrue(all(s.startswith("authority:") for s in order[2:]))
        self.assertTrue(order[2:], "authority enrichment should have run")

    def test_retrieval_failure_does_not_remove_other_evidence(self):
        from tools.evidence import Evidence
        from tools.orchestrator import gather_graphrag_evidence

        def boom(q, l, h=None):
            raise RuntimeError("provider down")

        with self.assertLogs("tools.orchestrator", level="WARNING"):
            r = gather_graphrag_evidence(
                "이규보의 시는?", "ko", graph_retriever=boom,
                vector_retriever=lambda q, l: Evidence(
                    kind="vector", documents=[{"entry_id": "E001"}]),
                authority_fetcher=lambda *a: {})
        self.assertEqual(r["statuses"]["graph"]["outcome"], "temporarily_unavailable")
        self.assertEqual(r["statuses"]["vector"]["outcome"], "ok")
        self.assertTrue(r["vector"].documents)


if __name__ == "__main__":
    unittest.main()

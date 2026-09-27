"""Deterministic test doubles for the observability suites.

Everything here runs the REAL production code paths — GraphCypherQAChain
(built by the production builder), the read-only Cypher safety wrapper,
`Neo4jVector.similarity_search_with_score_by_vector`, the `GoogleEmbeddings`
retry client, the evidence orchestrator and both pipelines — with only the
outermost I/O replaced: a scripted chat model, an in-memory Neo4j driver/row
source, and a fake HTTP session. No real API, internet, or Neo4j is touched.

Not collected as a test module (name does not match `test_*.py`).
"""

from __future__ import annotations

import functools
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from langchain_core.chat_history import InMemoryChatMessageHistory  # noqa: E402
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.prompts import PromptTemplate  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402
from langchain_neo4j.graphs.graph_store import GraphStore  # noqa: E402
from langchain_neo4j.vectorstores.neo4j_vector import IndexType, SearchType  # noqa: E402

from chatbot.observability import telemetry  # noqa: E402
from chatbot.observability.sinks import MemorySink  # noqa: E402
from tools.cypher_safety import SafeNeo4jGraph  # noqa: E402

GRAPHRAG_CYPHER = ("MATCH (p:Person) RETURN p.ID AS person_id, "
                   "p.nameKor AS person_name_kor LIMIT 5")
GRAPH_ROWS = [
    {"person_id": "P553", "person_name_kor": "허초희"},
    {"person_id": "P100", "person_name_kor": "어숙권"},
]
VECTOR_ROWS = [
    {"text": "본문 A", "score": 0.91, "metadata": {
        "entry_id": "E031", "entry_position": 31, "source_work_id": "B023",
        "source_work_kor": "패관잡기", "source_work_eng": "Paegwan Chapki",
        "korean_translation": "번역 A", "original_chinese": "原文A",
        "english_translation": "translation A",
        "entry_name_kor": "항목", "poetrytalks_link": "https://poetrytalks.org/E031"}},
    {"text": "본문 B", "score": 0.80, "metadata": {
        "entry_id": "E032", "entry_position": 32, "source_work_id": "B023",
        "source_work_kor": "패관잡기", "korean_translation": "번역 B"}},
]


# ── scripted chat model (one object serves every purpose, as in production) ─
def usage(inp, out, total=None):
    return {"input_tokens": inp, "output_tokens": out,
            "total_tokens": total if total is not None else inp + out}


class UsageStreamingChat(GenericFakeChatModel):
    """Like GenericFakeChatModel, but a STREAMED reply carries its usage
    metadata on the chunk (as Gemini does) instead of dropping it — so tests
    can check that streamed usage is recorded exactly once."""

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        message = next(self.messages)
        yield ChatGenerationChunk(message=AIMessageChunk(
            content=message.content,
            usage_metadata=getattr(message, "usage_metadata", None)))


def scripted_llm(*replies):
    """`replies` are (text, usage_dict_or_None) tuples, consumed in order."""
    messages = [AIMessage(content=text, usage_metadata=u) if u else AIMessage(content=text)
                for text, u in replies]
    return UsageStreamingChat(messages=iter(messages))


# ── graph store double behind the REAL safety wrapper ────────────────────────
class RowSource:
    """Innermost 'database': returns fixed rows (or raises) and counts calls."""

    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.calls = 0

    def query(self, query, params=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [dict(r) for r in self.rows]


class SafeGraphStore(GraphStore):
    """A pydantic-acceptable GraphStore whose `.query` goes through the real
    `SafeNeo4jGraph` validator + observed execution boundary."""

    def __init__(self, source: RowSource):
        self._safe = SafeNeo4jGraph(source)
        self._enhanced_schema = False

    @property
    def get_structured_schema(self):
        return {"node_props": {}, "rel_props": {}, "relationships": []}

    def get_schema(self):
        return ""

    def query(self, query, params={}):  # noqa: B006 - mirrors GraphStore API
        return self._safe.query(query, params or None)

    def refresh_schema(self):
        return None

    def add_graph_documents(self, *args, **kwargs):
        return None


def structured_graph_chain(llm, source: RowSource):
    from chatbot.retrieval.graph_chain import build_graph_cypher_chain
    from chatbot.retrieval.graph_prompt import CYPHER_GENERATION_TEMPLATE

    return build_graph_cypher_chain(
        llm, graph=SafeGraphStore(source),
        cypher_prompt=PromptTemplate.from_template(CYPHER_GENERATION_TEMPLATE),
        return_intermediate_steps=True, verbose=False)


# ── GoogleEmbeddings (real retry client) with a fake HTTP session ───────────
def load_google_embeddings_class():
    """Import `llm.GoogleEmbeddings` without real secrets or network (same
    technique as tests/test_phase5_embeddings.py)."""
    import streamlit as st

    fakes = {"GOOGLE_API_KEY": "test-key", "GOOGLE_MODEL": "models/fake",
             "GOOGLE_EMBEDDING_MODEL": "models/gemini-embedding-001"}
    with patch.object(st, "secrets", MagicMock(**{
        "__getitem__.side_effect": fakes.get,
        "get.side_effect": lambda k, d=None: fakes.get(k, d),
    })):
        with patch("langchain_google_genai.ChatGoogleGenerativeAI",
                   return_value=MagicMock()):
            if "llm" in sys.modules:
                importlib.reload(sys.modules["llm"])
            else:
                import llm  # noqa: F401
    return sys.modules["llm"].GoogleEmbeddings


def embedding_response(dim=3, status=200):
    r = SimpleNamespace()
    r.status_code = status
    r.headers = {"content-type": "application/json"}
    r.json = MagicMock(return_value={"embedding": {"values": [0.1] * dim}})
    r.raise_for_status = MagicMock()
    return r


def fake_embeddings(dim=3, responses=None):
    GoogleEmbeddings = load_google_embeddings_class()
    session = MagicMock()
    if responses is not None:
        session.post.side_effect = responses
    else:
        session.post.side_effect = lambda *a, **k: embedding_response(dim)
    return GoogleEmbeddings(api_key="k", model="models/gemini-embedding-001",
                            session=session), session


# ── Neo4jVector double: real similarity code, in-memory `.query` ────────────
def vector_store(embeddings, rows=None, *, light=False):
    """An `InstrumentedNeo4jVector` whose only replaced piece is `.query` (the
    DB round-trip). Built without `__init__` so no driver is opened."""
    from chatbot.retrieval.vector_query import (
        _build_light_retrieval_query,
        _build_retrieval_query,
    )
    from chatbot.retrieval.vector_retriever import InstrumentedNeo4jVector

    class _InMemoryVector(InstrumentedNeo4jVector):
        def query(self, query, *, params=None, **kwargs):
            self.db_calls += 1
            return [json.loads(json.dumps(r)) for r in self.rows]

    store = object.__new__(_InMemoryVector)
    attrs = {
        "rows": list(VECTOR_ROWS if rows is None else rows), "db_calls": 0,
        "embedding": embeddings, "search_type": SearchType.VECTOR,
        "_index_type": IndexType.NODE,
        "retrieval_query": (_build_light_retrieval_query if light
                            else _build_retrieval_query)("textKor"),
        "index_name": "EntryTextsKor", "keyword_index_name": None,
        "embedding_dimension": 3, "embedding_node_property": "textEmbedding_Kor",
        "node_label": "Entry", "text_node_property": "textKor",
        "neo4j_version_is_5_23_or_above": True, "support_metadata_filter": True,
        "_is_enterprise": False, "override_relevance_score_fn": None,
    }
    for key, value in attrs.items():
        object.__setattr__(store, key, value)
    return store


# ── end-to-end runners ───────────────────────────────────────────────────────
class History(InMemoryChatMessageHistory):
    pass


def run_graphrag(question="허초희는 누구인가?", *, sink=None, llm=None,
                 graph_source=None, vector_rows=None, graph_retriever=None,
                 vector_retriever=None, authority_fetcher=None, history=None,
                 open_root=True):
    """Drive `graphrag_pipeline.synthesize_answer` through the real
    orchestrator with injected retrievers. Returns (output, sink, parts)."""
    from chatbot.application import graphrag_pipeline
    from chatbot.retrieval import graph_query
    from chatbot.retrieval import vector_retriever as vector_mod
    from chatbot.synthesis.prompt import build_synthesis_chain
    from tools.orchestrator import gather_graphrag_evidence

    sink = sink if sink is not None else MemorySink()
    llm = llm or scripted_llm((GRAPHRAG_CYPHER, usage(120, 20)),
                              ("graph qa prose", usage(300, 40)),
                              ("최종 답변 [P553](https://poetrytalks.org/P553).",
                               usage(900, 80)))
    graph_source = graph_source or RowSource(GRAPH_ROWS)
    chain = structured_graph_chain(llm, graph_source)
    embeddings, _session = fake_embeddings()
    store = vector_store(embeddings, vector_rows)
    retriever = store.as_retriever()

    graph_retriever = graph_retriever or (
        lambda q, lang, h=None: graph_query.retrieve_graph_evidence(q, h, chain=chain))
    vector_retriever = vector_retriever or (
        lambda q, lang, h=None: vector_mod.retrieve_sihwa_evidence(q, retriever=retriever))
    gather = functools.partial(
        gather_graphrag_evidence, graph_retriever=graph_retriever,
        vector_retriever=vector_retriever,
        authority_fetcher=authority_fetcher or (lambda *a: {"status": "unavailable"}))
    history = history if history is not None else History()

    def _call():
        return graphrag_pipeline.synthesize_answer(
            question, "ko", "ko", synthesis_chain=build_synthesis_chain(llm),
            history_factory=lambda sid: history, session_id="sid")

    with telemetry.use_sink(sink), \
            patch.object(graphrag_pipeline, "gather_graphrag_evidence", gather):
        if open_root:
            with telemetry.request_scope(mode="graphRAG", question_language="ko",
                                         response_language="ko"):
                output = _call()
        else:
            output = _call()
    return output, sink, SimpleNamespace(graph_source=graph_source, store=store,
                                         history=history)


def run_vectorrag(question="달을 노래한 시화는?", *, sink=None, llm=None, rows=None):
    from chatbot.application.vectorrag_pipeline import run_vectorrag_pipeline
    from chatbot.observability import events as ev
    from chatbot.observability.callbacks import with_llm_purpose

    sink = sink if sink is not None else MemorySink()
    llm = llm or scripted_llm(("본문 [E031](https://poetrytalks.org/E031) 입니다.",
                               usage(700, 60)))
    embeddings, session = fake_embeddings()
    store = vector_store(embeddings, rows, light=True)
    base = RunnableLambda(lambda x: x["input"]) | store.as_retriever(search_kwargs={"k": 10})
    with telemetry.use_sink(sink):
        with telemetry.request_scope(mode="textRAG", question_language="ko",
                                     response_language="ko"):
            output = run_vectorrag_pipeline(
                question, "ko", base_retriever=base,
                llm=with_llm_purpose(llm, ev.LLM_TEXT_RAG_ANSWER),
                memory_factory=lambda sid: History(), session_id="sid::textRAG")
    return output, sink, SimpleNamespace(store=store, session=session)


def purposes(sink):
    from collections import Counter

    return Counter(e["purpose"] for e in sink.of("llm.completed"))

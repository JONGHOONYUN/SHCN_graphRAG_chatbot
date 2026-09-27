"""Observability dry run — reproduce the Phase 1 baseline scenarios offline.

    python tests/observability_baseline.py            # summary table
    python tests/observability_baseline.py --jsonl    # every event, one per line

Runs the same deterministic doubles as the automated tests (scripted chat
model, in-memory Neo4j rows, fake embedding HTTP session) through the REAL
production code paths. No API key, internet, or Neo4j is needed, and no live
usage numbers are fabricated: token figures below are the scripted fixture
values, useful only to verify that the accounting is correct.

Not a test module (its name does not match `test_*.py`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import _observability_fixtures as fx  # noqa: E402
from chatbot.observability import events as ev, telemetry  # noqa: E402
from chatbot.observability.sinks import MemorySink, NullSink  # noqa: E402


class _ExplodingSink:
    def emit(self, event):
        raise RuntimeError("sink down")


def _summary(name, sink, expected, extra=""):
    done = (sink.of(ev.REQUEST_COMPLETED) or [{}])[0]
    by_purpose = done.get("llm_calls_by_purpose") or {}
    used = {k: v for k, v in by_purpose.items() if v}
    return {
        "scenario": name, "expected": expected,
        "outcome": done.get("outcome"), "llm_calls": done.get("llm_call_count"),
        "by_purpose": used, "embedding_calls": done.get("embedding_call_count"),
        "fallback_used": done.get("fallback_used"),
        "tokens_total": done.get("total_tokens"),
        "citations": done.get("citation_count"), "answer_chars": done.get("answer_chars"),
        "note": extra,
    }


def _authority_cache_hit():
    from chatbot.authority.cache import clear_authority_cache
    from chatbot.authority.service import fetch_authority

    clear_authority_cache()
    calls = []
    sink = MemorySink()
    with telemetry.use_sink(sink):
        with telemetry.request_scope(mode="graphRAG"):
            for _ in range(2):
                fetch_authority("wikidata", "Q1", fetcher=lambda u: calls.append(u) or {
                    "entities": {"Q1": {"labels": {"en": {"value": "x"}}}}})
    clear_authority_cache()
    miss, hit = sink.of(ev.RETRIEVAL_AUTHORITY_SOURCE)
    return sink, (f"miss: http_fetched={miss['http_fetched']} attempts={miss['attempt_count']}; "
                  f"hit: cache_hit={hit['cache_hit']} http_fetched={hit['http_fetched']} "
                  f"attempts={hit['attempt_count']}; real HTTP calls={len(calls)}")


def _fallback():
    from test_observability_pipelines import _AGENT_DRIVER

    proc = subprocess.run(
        [sys.executable, "-c", _AGENT_DRIVER.format(repo=str(TESTS.parent),
                                                    tests=str(TESTS))],
        cwd=str(TESTS.parent), capture_output=True, text=True, encoding="utf-8")
    line = [l for l in proc.stdout.splitlines() if l.startswith("REPORT")][-1]
    events = json.loads(line[len("REPORT"):])["transient"]["events"]
    sink = MemorySink()
    sink.extend(events)
    ids = {e["request_id"] for e in events}
    return sink, f"distinct request_ids={len(ids)}"


def main(argv) -> int:
    rows, all_events = [], []

    _, sink, _ = fx.run_graphrag()
    rows.append(_summary("정상 GraphRAG", sink, "cypher 1 · graph_qa 1 · synthesis 1"))
    all_events += sink.events

    _, sink, _ = fx.run_graphrag(graph_source=fx.RowSource([]))
    graph = sink.of(ev.RETRIEVAL_GRAPH)[0]["status"]
    vector = sink.of(ev.RETRIEVAL_VECTOR)[0]["status"]
    rows.append(_summary("graph empty + vector success", sink,
                         "status 분리, synthesis 실행", f"graph={graph} vector={vector}"))
    all_events += sink.events

    def boom(q, lang, h=None):
        raise RuntimeError("down")

    with patch("chatbot.application.retriever_invocation.logger"):
        _, sink, _ = fx.run_graphrag(graph_retriever=boom, vector_retriever=boom)
    rows.append(_summary("모든 검색 실패", sink, "short_circuit · synthesis 0"))
    all_events += sink.events

    _, sink, _ = fx.run_vectorrag()
    rows.append(_summary("정상 VectorRAG", sink, "embedding · vector query · text_rag_answer",
                         "neo4j vector_query=%d" % len(sink.where(ev.NEO4J_QUERY,
                                                                  operation="vector_query"))))
    all_events += sink.events

    sink, note = _fallback()
    rows.append(_summary("transient 오류 → ReAct 성공", sink,
                         "동일 request_id · fallback_success", note))
    all_events += sink.events

    llm = fx.scripted_llm((fx.GRAPHRAG_CYPHER, None), ("qa", None), ("답변", None))
    _, sink, _ = fx.run_graphrag(llm=llm)
    rows.append(_summary("provider usage 없음", sink, "token null · usage unavailable",
                         "usage_available_calls=%d" %
                         sink.of(ev.REQUEST_COMPLETED)[0]["usage_available_call_count"]))
    all_events += sink.events

    sink, note = _authority_cache_hit()
    rows.append(_summary("authority cache hit", sink, "외부 HTTP 0 · cache_hit true", note))
    all_events += sink.events

    reference, _, _ = fx.run_graphrag(sink=NullSink())
    with patch("chatbot.observability.emitter._internal_logger", MagicMock()):
        failing, _, _ = fx.run_graphrag(sink=_ExplodingSink())
    rows.append({"scenario": "관측 sink 실패", "expected": "사용자 결과 불변",
                 "note": f"answer identical to disabled run: {failing == reference}"})

    if "--jsonl" in argv:
        for event in all_events:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True))
        return 0
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

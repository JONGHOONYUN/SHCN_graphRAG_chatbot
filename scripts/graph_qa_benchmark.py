"""Repeatable live Graph QA benchmark; uses the real app, isolated RAM history.

Run BEFORE editing production code, then AFTER, in separate processes:
  python -B scripts/graph_qa_benchmark.py --stage before --output artifacts/graph_qa_run/before
  python -B scripts/graph_qa_benchmark.py --stage after --output artifacts/graph_qa_run/after

Uses configured credentials and consumes real API tokens. No graph/history writes
are requested. Events contain no raw content; answers.jsonl is a separate local
evaluation artifact for the fixed, public-domain benchmark questions.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = [
    {"id": "person", "route": "generated", "question": "허초희는 누구인가?"},
    {"id": "work", "route": "generated", "question": "패관잡기의 저자는 누구이며, 어떤 작품인가?"},
    {"id": "entries", "route": "generated", "question": "허초희가 언급된 시화 항목을 찾아 근거와 함께 설명해줘."},
    {"id": "ranking", "route": "deterministic", "question": "시화총림에서 가장 많이 언급된 왕은 누구인가?"},
]
CONTROL_FILES = [
    "llm.py", "rag_config.py", "chatbot/retrieval/graph_prompt.py",
    "chatbot/retrieval/vector_query.py", "chatbot/synthesis/prompt.py",
    "chatbot/application/evidence_orchestrator.py",
]
CHANGE_FILES = [
    "tools/cypher.py", "chatbot/retrieval/graph_chain.py",
    "chatbot/retrieval/graph_query.py",
]


def _write_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def _json_line(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    stream.flush()


def source_hashes(paths):
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}


def evidence_summary(evidence):
    # IDs are evaluation metadata only; never injected into telemetry events.
    from chatbot.domain.evidence_merge import collect_node_references
    result = {"statuses": evidence.get("statuses", {}),
              "ranking_role": evidence.get("ranking_role")}
    for key in ("graph", "vector"):
        item = evidence[key]
        result[key + "_node_ids"] = sorted({
            r.node_id for r in collect_node_references(item) if r.node_id})
        result[key + "_document_count"] = len(item.documents)
    return result


def _run_request(agent, case, repetition):
    from langchain_core.chat_history import InMemoryChatMessageHistory
    from chatbot.application import graphrag_pipeline
    from chatbot.authority.cache import clear_authority_cache
    from chatbot.legacy.react_agent import build_chat_agent
    from chatbot.observability import telemetry
    from chatbot.observability.sinks import MemorySink
    import streamlit as st

    history = InMemoryChatMessageHistory()
    sink = MemorySink()
    captured = {}
    gather = graphrag_pipeline.gather_graphrag_evidence

    def capture(*args, **kwargs):
        evidence = gather(*args, **kwargs)
        captured.update(evidence_summary(evidence))
        return evidence

    # Same cache policy on both sides. Vector initialization happens once in
    # setup, while authority cache starts empty for every measured request.
    clear_authority_cache()
    state = {"question_language": "ko", "response_language": "ko",
             "effective_language": "ko"}
    session = f"benchmark-{case['id']}-{repetition}"
    ram_agent = build_chat_agent(agent.agent_executor, lambda sid: history)
    error_type = None
    answer = ""
    with telemetry.use_sink(sink), \
            patch.object(st, "session_state", state), \
            patch.object(agent, "get_session_id", return_value=session), \
            patch.object(agent, "_graphrag_history", lambda sid: history), \
            patch.object(agent, "chat_agent", ram_agent), \
            patch.object(graphrag_pipeline, "gather_graphrag_evidence", capture), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            answer = agent.generate_response(case["question"])
        except Exception as exc:
            error_type = type(exc).__name__
    completed = sink.of("request.completed")
    if len(completed) != 1:
        raise RuntimeError("Expected exactly one completed request")
    done = completed[0]
    llms = sink.of("llm.completed")
    graph_spans = sink.of("retrieval.graph.completed")
    qa = [e for e in llms if e.get("purpose") == "graph_qa"]
    record = {
        "case_id": case["id"], "expected_route": case["route"],
        "repetition": repetition, **done,
        "usage_complete": bool(llms) and all(
            e.get(k) is not None for e in llms
            for k in ("input_tokens", "output_tokens", "total_tokens")),
        "graph_duration_ms": sum(e["duration_ms"] for e in graph_spans),
        "graph_qa_duration_ms": sum(e["duration_ms"] for e in qa),
        "graph_qa_tokens": (sum(e["total_tokens"] for e in qa)
                            if all(e.get("total_tokens") is not None for e in qa) else None),
        "history_messages": len(history.messages),
        "evidence": captured,
        "answer_links": sorted(set(re.findall(r"https?://[^\s)\]>]+", answer))),
        "unhandled_error_type": error_type,
    }
    return record, sink.events, answer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    args = parser.parse_args(argv)
    # Refuse to overwrite previous measurements.
    if args.output.exists():
        parser.error("Output directory already exists; choose a new run directory")
    logging.disable(logging.CRITICAL)  # legacy verbose/error payloads are not artifacts
    print("Initializing live clients; credentials are never printed.", flush=True)
    with contextlib.redirect_stdout(io.StringIO()):
        import agent
        from graph import graph
        from tools.cypher import cypher_qa, cypher_qa_structured
        from tools.vector import _get_retriever_for_lang
        _get_retriever_for_lang("ko")
    expected_direct = args.stage == "after"
    if cypher_qa_structured.return_direct != expected_direct or cypher_qa.return_direct:
        raise RuntimeError("Chain configuration does not match the benchmark stage")
    graph_count = graph.query("MATCH (n) RETURN count(n) AS node_count")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "stage": args.stage, "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": CASES, "repeats": args.repeats, "live": True,
        "model": agent.llm.model, "temperature": agent.llm.temperature,
        "versions": {k: importlib.metadata.version(k) for k in
                     ("langchain-core", "langchain-neo4j", "langchain-google-genai")},
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "control_hashes": source_hashes(CONTROL_FILES),
        "change_hashes": source_hashes(CHANGE_FILES), "graph_counts": graph_count,
        "structured_return_direct": cypher_qa_structured.return_direct,
        "legacy_return_direct": cypher_qa.return_direct,
        "history": "fresh in-memory history per request; no Neo4j chat writes",
        "cache": "vector initialized before timing; authority cleared per request",
        "scope": "agent.generate_response backend; excludes UI and Neo4j history I/O",
        "limitations": "Small sequential sample, not a production load test; SDK retries invisible",
    }
    _write_json(args.output / "manifest.json", manifest)
    records = []
    with (args.output / "events.jsonl").open("x", encoding="utf-8") as events_file, \
            (args.output / "runs.jsonl").open("x", encoding="utf-8") as runs_file, \
            (args.output / "answers.jsonl").open("x", encoding="utf-8") as answers_file:
        for repetition in range(1, args.repeats + 1):
            for case in CASES:
                print(f"START {args.stage} {case['id']} repeat={repetition}", flush=True)
                record, events, answer = _run_request(agent, case, repetition)
                for event in events:
                    _json_line(events_file, event)
                _json_line(runs_file, record)
                _json_line(answers_file, {"case_id": case["id"], "repetition": repetition,
                                          "request_id": record["request_id"], "answer": answer})
                records.append(record)
                print(json.dumps({k: record[k] for k in (
                    "case_id", "repetition", "outcome", "llm_call_count", "total_tokens",
                    "duration_ms", "usage_complete")}), flush=True)
                if len(records) >= 2 and all(r["outcome"] not in
                        ("success", "fallback_success") for r in records[-2:]):
                    print("Stopped after two consecutive failed requests; partial logs retained.", flush=True)
                    return 2
    print(f"Saved {len(records)} requests to {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print credential-bearing provider exceptions or connection URIs.
        print(f"Benchmark failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)

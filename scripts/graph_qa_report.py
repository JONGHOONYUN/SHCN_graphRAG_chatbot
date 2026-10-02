"""Compare two graph_qa_benchmark runs without calling any provider."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics


def load_run(directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (directory / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line]
    keys = [(r["case_id"], r["repetition"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate benchmark case/repetition")
    expected = {(c["id"], n) for c in manifest["cases"]
                for n in range(1, manifest["repeats"] + 1)}
    if set(keys) != expected:
        raise ValueError("Incomplete run: keep partial logs, do not report a complete comparison")
    return manifest, rows


def percentile(values, fraction):
    """Nearest-rank percentile; small-sample p95 can simply be the maximum."""
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def average(rows, key):
    values = [r[key] for r in rows if r.get(key) is not None]
    return statistics.mean(values) if values else None


def summarize(rows):
    return {
        "requests": len(rows),
        "success_count": sum(r["outcome"] == "success" for r in rows),
        "fallback_count": sum(bool(r.get("fallback_used")) for r in rows),
        "graph_qa_calls": sum(r["llm_calls_by_purpose"].get("graph_qa", 0) for r in rows),
        "mean_llm_calls": average(rows, "llm_call_count"),
        "complete_usage_requests": sum(bool(r["usage_complete"]) for r in rows),
        # Missing usage is never treated as zero, nor silently averaged away.
        **{f"mean_{k}": average(rows, k) if rows and all(
            r["usage_complete"] and r.get(k) is not None for r in rows) else None
           for k in ("input_tokens", "output_tokens", "total_tokens")},
        "median_ms": statistics.median([r["duration_ms"] for r in rows]) if rows else None,
        "p95_ms": percentile([r["duration_ms"] for r in rows], 0.95),
        "median_graph_ms": statistics.median([r["graph_duration_ms"] for r in rows]) if rows else None,
        "mean_graph_qa_ms": average(rows, "graph_qa_duration_ms"),
        "mean_graph_qa_tokens": average(rows, "graph_qa_tokens"),
    }


def reduction(before, after):
    if before is None or after is None or before == 0:
        return None
    return (before - after) / before * 100


def compare(before_manifest, before, after_manifest, after):
    for key in ("cases", "repeats", "model", "temperature", "versions", "control_hashes",
                "history", "cache", "scope", "graph_counts"):
        if before_manifest.get(key) != after_manifest.get(key):
            raise ValueError(f"Comparison controls differ: {key}")
    before_map = {(r["case_id"], r["repetition"]): r for r in before}
    after_map = {(r["case_id"], r["repetition"]): r for r in after}
    if before_map.keys() != after_map.keys():
        raise ValueError("Comparison cases differ")
    generated = [key for key, b in before_map.items()
                 if b["expected_route"] == "generated"
                 and b["outcome"] == after_map[key]["outcome"] == "success"
                 and not b.get("fallback_used") and not after_map[key].get("fallback_used")]
    pairs = []
    for key, b in before_map.items():
        a = after_map[key]
        pairs.append({
            "case_id": key[0], "repetition": key[1],
            "before_calls": b["llm_call_count"], "after_calls": a["llm_call_count"],
            "before_tokens": b["total_tokens"], "after_tokens": a["total_tokens"],
            "before_ms": b["duration_ms"], "after_ms": a["duration_ms"],
            "before_outcome": b["outcome"], "after_outcome": a["outcome"],
            "graph_ids_equal": b["evidence"].get("graph_node_ids") == a["evidence"].get("graph_node_ids"),
            "vector_ids_equal": b["evidence"].get("vector_node_ids") == a["evidence"].get("vector_node_ids"),
            "answer_links_equal": b["answer_links"] == a["answer_links"],
            "before_citations": b.get("citation_count"), "after_citations": a.get("citation_count"),
        })
    b_summary = summarize([before_map[key] for key in generated])
    a_summary = summarize([after_map[key] for key in generated])
    return {
        "all_before": summarize(before), "all_after": summarize(after),
        "generated_successful_pairs": len(generated),
        "before": b_summary, "after": a_summary,
        "reductions_percent": {key: reduction(b_summary[key], a_summary[key]) for key in (
            "mean_llm_calls", "mean_total_tokens", "median_ms", "p95_ms", "median_graph_ms")},
        "pairs": pairs,
        "quality_note": "ID/link equality is a structural check, not semantic answer grading.",
    }


def render(report):
    def fmt(value):
        return "N/A" if value is None else f"{value:,.2f}"
    b, a = report["before"], report["after"]
    lines = [
        "# Graph QA live comparison", "",
        "Same fixed questions, two separate processes, fresh RAM history per request.",
        "Configured real model, Neo4j retrieval, embedding and authority calls are used.",
        "UI and production Neo4j history read/write latency are excluded.",
        "Small sample: descriptive results only; p95 may be the maximum observation.", "",
        f"Paired successful generated-GraphRAG requests: {report['generated_successful_pairs']}", "",
        "| Metric | Before | After | Reduction (%) |", "|---|---:|---:|---:|",
    ]
    for key in ("mean_llm_calls", "mean_input_tokens", "mean_output_tokens", "mean_total_tokens",
                "median_ms", "p95_ms", "median_graph_ms", "graph_qa_calls"):
        lines.append(f"| {key} | {fmt(b[key])} | {fmt(a[key])} | {fmt(reduction(b[key], a[key]))} |")
    lines += ["", "All requests (including deterministic ranking and failures):", "",
              "```json", json.dumps({"before": report["all_before"], "after": report["all_after"]},
                                    ensure_ascii=False, indent=2), "```", "",
              "| Case / repeat | LLM calls | Tokens | Latency ms | Graph IDs equal | Vector IDs equal | Answer links equal |",
              "|---|---|---|---|---|---|---|"]
    for p in report["pairs"]:
        lines.append(f"| {p['case_id']} / {p['repetition']} | {p['before_calls']} → {p['after_calls']} | "
                     f"{p['before_tokens']} → {p['after_tokens']} | {fmt(p['before_ms'])} → {fmt(p['after_ms'])} | "
                     f"{p['graph_ids_equal']} | {p['vector_ids_equal']} | {p['answer_links_equal']} |")
    lines += ["", report["quality_note"],
              "Review answers.jsonl separately; observability events contain no question or answer text.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bm, before = load_run(args.before)
    am, after = load_run(args.after)
    report = compare(bm, before, am, after)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "comparison.md").write_text(render(report), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "pairs"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

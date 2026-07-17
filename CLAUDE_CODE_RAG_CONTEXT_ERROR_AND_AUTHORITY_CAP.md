# Claude Code Work Order: Conversation Context, User-Safe Retrieval Errors, and Authority Enrichment Scale

## Objective

Improve the current chatbot without regressing the evidence-first graphRAG architecture or the Person/Place authority safety controls.

This task has four required outcomes:

1. graphRAG normal-path answers must use prior conversation context safely.
2. textRAG user-facing wording must accurately describe its use of Neo4j metadata relationships.
3. graph/vector retrieval failures must be normalized into user-safe status information rather than technical exception text visible to the final LLM/user.
4. broad authority-enrichment queries must support more than the current maximum of three people, while transparently reporting any cap/truncation.

## Files in Scope

- `agent.py`
- `tools/orchestrator.py`
- `tools/evidence.py`
- `tools/synthesis.py`
- `tools/external_authority.py`
- `text_rag.py`
- `bot.py` (only if required for messaging/state integration)
- `tests/test_pipeline.py` and any new focused tests
- `IMPLEMENTATION_NOTE.md`

Do not redesign the Streamlit UI. Do not weaken the existing external-authority registry, response validation, node-type safety, or citation rules.

---

## 1. Add Conversation Context to the graphRAG Normal Path

### Problem

The normal graphRAG path is:

```text
generate_response()
  -> synthesize_answer()
  -> gather_graphrag_evidence(current_user_input)
  -> one final synthesis LLM call
```

Unlike the legacy ReAct fallback and textRAG, this normal path does not currently use `Neo4jChatMessageHistory`. Consequently, follow-up questions such as “앞서 말한 그 인물은?”, “그 작품의 다음 시는?” can lose referents.

### Required Design

Use graphRAG-specific Neo4j chat history with the existing session namespace:

```text
<Streamlit session ID>::graphRAG
```

The normal graphRAG path must read recent relevant history and provide it to both:

1. the evidence gathering step, for query resolution/retrieval; and
2. the final synthesis step, for interpreting follow-up references.

### Implementation Requirements

1. Reuse `Neo4jChatMessageHistory` or a clearly equivalent history adapter. Do not mix graphRAG and textRAG histories.
2. Add a bounded history serializer, e.g. the last 6–10 messages or a token/character budget. Do not pass unlimited historical content to Gemini.
3. Preserve speaker roles (`user`, `assistant`) and omit tool traces, raw external payloads, and internal errors from the serialized history.
4. Pass history to graph retrieval in an explicit, bounded form. The retrieval prompt may use it only to resolve references; it must not treat prior assistant assertions as evidence.
5. Pass the same bounded history to final synthesis with an explicit rule:

```text
Conversation history may resolve pronouns or ellipsis only.
It is not evidence. Corpus and external facts must come only from the current evidence blocks.
```

6. After a successful normal graphRAG answer, append the current user message and final assistant answer to the graphRAG history.
7. Do not double-save messages when the legacy ReAct fallback is invoked; define one clear owner for persistence per code path.
8. If history is unavailable or corrupt, continue with an empty history and do not fail the answer.
9. Add a clear-session/expiry strategy if the existing Neo4j history retention is unbounded.

### Follow-up Retrieval Behavior

For a follow-up query such as “그 인물은 어떤 시를 썼나요?”:

- resolve “그 인물” only from the bounded prior conversation;
- convert it to a resolved name/node-ID query for graph/vector retrieval where possible;
- if multiple plausible prior entities exist, ask a concise clarification rather than arbitrarily selecting one;
- do not infer a missing entity from model pretraining.

### Tests

Add mocked tests for:

1. graphRAG history uses `::graphRAG`, never `::textRAG`.
2. a follow-up reference resolves using an immediately preceding graphRAG turn.
3. multiple possible antecedents produce a clarification path rather than a guessed entity.
4. textRAG history remains isolated.
5. historical assistant text alone is not emitted as a corpus/external fact without current evidence.
6. history is bounded and does not include raw authority data or technical failures.

---

## 2. Correct the textRAG Mode Description

### Problem

textRAG does not perform graph relationship reasoning, but its lightweight Neo4j vector retrieval query uses `[:HAS_PART]` to retrieve the containing Work name and citation metadata for an Entry.

Saying “textRAG does not use graph relationships” is technically inaccurate.

### Required Changes

Update all user-facing and prompt-facing descriptions in `bot.py`, `text_rag.py`, and any relevant greeting/help text to state the following accurately in Korean and other supported languages:

```text
textRAG performs semantic vector search over Entry texts.
It does not perform graph relationship reasoning or structured relationship queries.
It may use the Entry–Work containment relation only to attach source/citation metadata.
```

Requirements:

- Do not describe textRAG as graph-free or as never reading a relationship.
- Do not imply textRAG can answer authorship, office, era, critique, or multi-hop relationship questions reliably.
- Keep the graphRAG-switch recommendation for structural questions.
- Ensure labels, sidebar help, greeting text, and system prompt are consistent.

### Tests

- Assert textRAG user-facing documentation/prompt text does not contain an inaccurate “no graph relationship is used” claim.
- Assert it includes the distinction between citation metadata containment and graph relationship reasoning.

---

## 3. Normalize Retrieval Errors Before Final Synthesis

### Problem

Graph/vector failures currently become error claims in `Evidence`. If raw exception content reaches the final synthesis LLM, it can leak technical details such as Cypher syntax messages, provider exceptions, IDs, or implementation internals to users.

### Required Design

Introduce a user-safe retrieval status model. Keep detailed diagnostics only in server logs/observability; pass only normalized status codes and user-safe messages to synthesis.

Suggested shape:

```python
RetrievalStatus(
    source="graph" | "vector",
    outcome="ok" | "no_results" | "temporarily_unavailable" | "invalid_query",
    user_message_key="...",
    diagnostic_code="...",  # log-only; never sent to final prompt
)
```

Exact classes/field names may vary, but keep the separation between user-safe status and internal diagnostics.

### Required Behavior

1. **No results** is not an error. Represent it distinctly from a failed retrieval.
2. Graph Cypher syntax/runtime/provider failure becomes `temporarily_unavailable` or another stable user-safe category.
3. Vector embedding/index/provider failure becomes `temporarily_unavailable` without raw exception text.
4. Invalid/ambiguous user queries may become `invalid_query` only when that conclusion is reliable; otherwise use a concise clarification request.
5. Log internal exception class/message with a correlation/request ID, but never include it in `Evidence` passed to synthesis.
6. The final synthesis prompt must receive only safe status information, for example:

```text
그래프 구조 검색은 현재 일시적으로 사용할 수 없습니다.
텍스트 검색 결과는 계속 참고했습니다.
```

7. If graph retrieval fails but vector evidence exists, answer from vector evidence and state the graph limitation briefly.
8. If vector retrieval fails but graph evidence exists, answer from graph evidence and state the vector limitation briefly.
9. If both fail, return a localized user-friendly retry/clarification message without invoking model pretraining as fallback.
10. External authority failures retain the existing source-specific unavailable behavior; do not merge them with graph/vector retrieval errors.

### Tests

Add tests ensuring:

1. raw messages such as `CypherSyntaxError`, stack fragments, API keys, URLs with sensitive query parameters, and exception text never occur in evidence formatted for the final prompt.
2. graph `no_results` and graph failure render differently.
3. one successful source still produces an evidence-grounded answer when the other retrieval source fails.
4. both retrieval sources failing yields only a localized safe message.
5. diagnostics are available to logging/observability but excluded from final synthesis input.

---

## 4. Increase Broad-Query Authority Enrichment Capacity Transparently

### Problem

The current authority cap (three Person entities) protects latency and external service limits, but a broad biography/authority query can enrich only a subset of relevant people. If this truncation is not visible, users may interpret the output as comprehensive.

### Required Policy

Increase the default cap while retaining bounded behavior and transparency.

Initial target configuration:

```text
DEFAULT_PERSON_AUTHORITY_CAP = 10
DEFAULT_PLACE_AUTHORITY_CAP  = 5
DEFAULT_FETCHABLE_SOURCES_PER_ENTITY = 2
```

Make these configurable through named constants and optionally Streamlit secrets/environment configuration. Do not hard-code magic numbers throughout the orchestrator.

### Required Behavior

1. Enrich up to the configured caps for broad queries that genuinely request authority/biographical/place context.
2. Keep de-duplication by `source|node_type|original_authority_id`.
3. Keep source-per-entity limits, with an explicit cross-source comparison request allowed to raise that limit in a documented, bounded way.
4. Track:

```python
eligible_entity_count
enriched_entity_count
skipped_due_to_cap_count
```

separately for Person and Place when relevant.

5. If any entity was skipped due to a cap, add a structured, user-safe coverage note to the evidence. The final answer must state this clearly, for example:

```text
외부 authority 보강은 관련 인물 14명 중 10명에 적용했습니다.
나머지 인물은 시화총림 그래프 정보만으로 제시했습니다.
```

6. Do not show a truncation note if no eligible entity was skipped.
7. If the user explicitly requests a complete/exhaustive authority comparison, do not silently claim completeness when the configured safety cap remains in effect. Either:
   - apply a higher documented bounded cap; or
   - state that the result is a capped subset and offer a narrowed follow-up.
8. Preserve existing cache, timeout, node-type validation, response-schema validation, and failure behavior.

### Performance Requirements

- Do not run all external fetches concurrently without limits.
- Use a small bounded concurrency limit if concurrent fetching is introduced.
- Respect each provider’s rate limits and existing timeout behavior.
- Continue returning graph/vector answers when external enrichment is slow or partially unavailable.

### Tests

Add mocked tests for:

1. ten eligible Person entities produce at most the configured cap of authority enrichment attempts.
2. eleven eligible Person entities produce a coverage/truncation note with correct counts.
3. fewer than or equal to ten eligible entities produce no truncation note.
4. Person and Place caps are applied independently.
5. duplicate entities do not consume cap capacity twice.
6. a simple poem-list query still performs zero external calls.
7. explicit exhaustive/comparison wording never results in a false completeness claim.

---

## Acceptance Criteria

The task is complete when all conditions below are true:

1. The graphRAG normal path has bounded, graphRAG-isolated conversation context for follow-up reference resolution.
2. Previous assistant content is never treated as factual evidence without current graph/vector/external evidence.
3. textRAG descriptions accurately distinguish vector retrieval with citation metadata from graph relationship reasoning.
4. Final synthesis input contains no raw retrieval exception strings or stack details.
5. Graph/vector `no_results`, temporary failure, and invalid query states are distinguishable and localized.
6. Person authority enrichment defaults to 10 and Place enrichment defaults to 5, with safe caps retained.
7. Any authority truncation is explicitly reported with coverage counts; non-truncated answers do not show an unnecessary warning.
8. Existing Person/Place AKS Digerati namespace, endpoint, response-schema, cache, and citation safety tests continue to pass.
9. New tests pass without live Neo4j, Gemini, or external API access.
10. `IMPLEMENTATION_NOTE.md` documents history bounds, safe error categories, authority cap settings, and coverage-note behavior.

## Non-Goals

- Do not make textRAG a graph relationship-reasoning mode.
- Do not remove authority caps entirely.
- Do not expose raw backend diagnostics to users or the synthesis model.
- Do not use LLM pretraining to fill gaps from failed retrieval or skipped authority enrichment.

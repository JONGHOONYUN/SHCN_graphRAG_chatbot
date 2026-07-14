# GraphRAG Evidence Pipeline — Implementation Note

Refactor of the graphRAG chatbot so that Neo4j graph facts, Neo4j vector
evidence, and external authority data are collected as **structured evidence**
before a **single final synthesis** LLM call. Retrievers no longer write
user-facing prose.

## Files changed / added

| File | Change |
|------|--------|
| `tools/evidence.py` | **New.** Typed data contract (`Entity`, `Provenance`, `Evidence`) + pure normalization helpers (`docs_to_evidence`, `graph_rows_to_evidence`, `merge_entities`, `collect_person_entities`). No streamlit/neo4j/llm imports → fully unit-testable. |
| `tools/orchestrator.py` | **New.** `gather_graphrag_evidence(question, language)` sequences graph → vector → deterministic authority enrichment. `needs_authority()` gates external calls. Heavy deps are injectable for tests. |
| `tools/synthesis.py` | **New.** Pure final-synthesis helpers: `SYNTHESIS_SYSTEM_RULES` (source/conflict rules), `format_evidence_for_prompt` (labelled, size-bounded blocks), `build_citations` (never fabricates a link). |
| `tools/vector.py` | Added `retrieve_sihwa_evidence()` returning structured `Evidence` (no `create_stuff_documents_chain`, no answer). Added `aks_digerati` to `audiences`. Legacy `get_poetry_plot()` retained for the old ReAct tool. |
| `tools/cypher.py` | Added `cypher_qa_structured` (`return_intermediate_steps=True`) and `retrieve_graph_evidence()` returning rows-as-evidence. Prompt updated: it now RETURNs external-ID fields and no longer claims the Cypher chain fetches HTTP data. Standardized Person field aliases documented. |
| `tools/external_authority.py` | Added structured `fetch_authority()` (status: `ok`/`unavailable`/`error`), TTL+bounded in-process cache, `FETCHABLE_SOURCES` vs `LINK_ONLY_SOURCES` registry + `link_only_reference()`. Removed hard streamlit dependency (language read is now try/except). Legacy `external_authority_lookup()` string tool delegates to `fetch_authority`. |
| `agent.py` | Added `synthesis_chain` + `synthesize_answer()`. `generate_response()` now runs the evidence pipeline first and falls back to the legacy ReAct agent only on failure. External-Authority tool description now states fetchable vs link-only. |
| `tests/test_pipeline.py` | **New.** 24 stdlib-`unittest` tests, fully mocked (no Neo4j / API keys / network). |

## Evidence schema used

```
Evidence   { kind: "graph"|"vector"|"external",
             claims: list[dict], entities: list[Entity],
             documents: list[dict], provenance: list[Provenance] }
Entity     { node_id, node_type, name_kor, name_chi, name_eng,
             wikidata_id, aks_digerati_id }
Provenance { source_type: "neo4j_graph"|"neo4j_vector"|"wikidata"|"aks_digerati",
             source_url, entity_id, work_id, entry_id,
             poem_or_critique_id, label }
```

Authority IDs are only ever populated from a matching graph node — never guessed
from a name. Entities are de-duplicated transitively by any shared strong ID
(node_id / wikidata_id / aks_digerati_id).

## Authority call limits & cache behavior

- **Gating:** external lookups run only when `needs_authority()` matches a
  biographical / naming / alias / date / authority cue (KO/EN/ZH). Poem-list and
  pure-structural questions call nothing.
- **Never from a name:** an entity with no authority ID is never looked up.
- **Cap:** ≤ 3 distinct people enriched per request (`DEFAULT_AUTHORITY_CAP`);
  the orchestrator also de-dupes `(source, id)` pairs so each ID is fetched once.
- **Fetchable only:** `wikidata` and `aks_digerati`. AKS Encyclopedia, Sillok,
  CBDB, LOC, BnF, Britannica, etc. are **link-only** — never claimed as fetched.
- **AKS link:** the citable public link is the API's `canonical_link`; the
  numeric ID transform (`koreanPerson_18816` → `18816`) is used only for the API
  request URL.
- **Cache:** in-process TTL (1 h) + bounded (256 entries) keyed by `source:id`.
  Successes cached; failures NOT cached (retryable next turn). Works without a
  streamlit context.
- **Optional:** a failed lookup records `status="unavailable"` and preserves
  graph/vector evidence; the synthesis prompt forbids backfilling from
  pretraining.

## Compatibility decisions (textRAG & fallback)

- `text_rag.py` and `bot.py` mode selection are **unchanged**; textRAG keeps its
  direct vector-answer path.
- `get_poetry_plot()`, `cypher_qa_safe()`, and the ReAct agent are **retained**
  as a graphRAG fallback path (used only if the new pipeline raises), preserving
  availability.
- The Cypher prompt keeps literal `{{...}}` map escaping; a test asserts the
  template exposes exactly `{schema}` and `{question}` as variables.

## Running the tests

```
python -m unittest tests.test_pipeline -v
```

24 tests, no external services required. Coverage maps to acceptance criteria
1–10 (both-ID enrichment, dedup, vector-only enrichment, poem-list no-call,
missing-ID no-call, failure resilience, conflict display, bounded prompt,
brace safety, KeyError-free graphRAG question).

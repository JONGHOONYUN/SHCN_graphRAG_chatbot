# GraphRAG External Authority Pipeline — Implementation Note

Covers four work orders, applied in sequence:

1. **Evidence pipeline refactor** (`CLAUDE_CODE_REFACTOR_TASK.md`) — structured
   graph/vector/external evidence before one final synthesis LLM call.
2. **External authority expansion** (`CLAUDE_CODE_EXTERNAL_AUTHORITY_EXPANSION.md`)
   — registry-driven support for every Person/Place authority ID stored in Neo4j.
3. **AKS Digerati type-safety hardening** (`CLAUDE_CODE_AKS_DIGERATI_TYPE_SAFETY.md`)
   — strict Person/Place separation at request, response, cache, and citation level.
4. **Context, safe errors, authority scale** (`CLAUDE_CODE_RAG_CONTEXT_ERROR_AND_AUTHORITY_CAP.md`)
   — bounded conversation history on the normal graphRAG path, user-safe
   retrieval statuses, accurate textRAG wording, and transparent 10/5 authority
   caps with coverage notes.

## Architecture

```text
User question
  -> graph retrieval  (tools/cypher.py  -> Evidence kind="graph")
  -> vector retrieval (tools/vector.py  -> Evidence kind="vector")
  -> entity collection + transitive de-dup      (tools/evidence.py)
  -> intent-gated, registry-driven enrichment   (tools/orchestrator.py)
  -> per-source parsed, validated, capped data  (tools/external_authority.py)
  -> ONE final synthesis LLM call               (tools/synthesis.py + agent.py)
  -> answer with source-separated citations
```

Retrievers never write user-facing prose. Only `agent.synthesize_answer()` does.
The legacy ReAct agent remains solely as a failure fallback; textRAG mode
(`text_rag.py`, `bot.py`) is unchanged and makes no external API calls.

## Files changed / added

| File | Change |
|---|---|
| `tools/evidence.py` | Data contract: `Entity` (generic `authority_ids: dict` map, Person **and** Place), `Provenance`, `Evidence`. Pure normalization from vector docs / graph rows; transitive de-dup (node_id first, then source+ID pairs; never names alone; Person/Place never merge). Re-keys a Place's `idAKSdigerati` → `aks_digerati_place`; registry-filters authority keys per node type. |
| `tools/external_authority.py` | Single declarative `AUTHORITY_REGISTRY` (19 sources) with per-source ID regex (anchored `fullmatch`), node-type binding, request/citation URL builders, parser, **response validator**, field allowlist, TTL. Structured `fetch_authority(source, id, node_type=…)`; `link_only_reference()`; legacy `external_authority_lookup('source:id')` kept for the ReAct fallback. |
| `tools/orchestrator.py` | `gather_graphrag_evidence()`: registry-driven selection by node type + capability; Person/Place intent cues routed separately; caps; `source\|node_type\|id` call de-dup; link-only refs recorded as `status="link_only"`; failures recorded, never fatal. |
| `tools/synthesis.py` | Registry-driven per-source field allowlists; labelled, size-bounded evidence blocks (per-block 4 000 / total 14 000 chars); `[Person]`/`[Place]` tags with a cross-citation ban; link-only rendered as 참고 링크; `build_citations()` never emits a URL absent from evidence. |
| `tools/vector.py` | `retrieve_sihwa_evidence()` (structured, no answer generation). Projection carries **all 16 Person authority IDs** for creator/mentioned_persons/audiences and all 3 Place IDs. Prompt lists only verified link patterns; forbids fabricating links for unverified sources. |
| `tools/cypher.py` | `retrieve_graph_evidence()` returns raw rows as evidence (`return_intermediate_steps=True`). Prompt: standardized Person/Place authority aliases, role prefixes for multi-hop rows, explicit "no HTTP here", Person/Place ID non-interchangeability warning. Literal `{{…}}` escaping intact (test-asserted). |
| `agent.py` | `synthesis_chain` + `synthesize_answer()`; `generate_response()` runs the pipeline first, ReAct fallback on failure. Tool description enumerates actual fetchable/link-only/unsupported sources. |
| `docs/external_authority_sources.md` | Phase-1 capability matrix (every ID verified against a real stored value) + the Critical safety finding with schemas, validation rules, cache policy, and regression test names. |
| `tests/test_pipeline.py` | 60 stdlib-`unittest` tests, fully mocked — no live Neo4j, API keys, or network. |

## Evidence schema

```python
Entity     { node_id, node_type: "Person"|"Place"|…, name_kor/chi/eng,
             authority_ids: {registry_key: stored_id} }   # single source of truth
Provenance { source_type, source_url, entity_id, work_id, entry_id,
             poem_or_critique_id, label }
Evidence   { kind: "graph"|"vector"|"external", claims, entities, documents, provenance }
```

Legacy accessors `Entity.wikidata_id` / `Entity.aks_digerati_id` remain as
read-only properties over `authority_ids`.

## Source capability decisions (verified 2026-07-17, real stored IDs)

**Fetchable (7)** — official JSON API confirmed with a representative ID:

| Key | Neo4j property | Node | Endpoint |
|---|---|---|---|
| `wikidata` | `idWikidata` | Person | `wikidata.org/wiki/Special:EntityData/{Q}.json` |
| `aks_digerati` | `idAKSdigerati` | Person | `digerati.aks.ac.kr:85/api/IdValues/{n}` |
| `aks_digerati_place` | `idAKSdigerati` | Place | `digerati.aks.ac.kr:88/api/IdValues/{n}` |
| `loc` | `idLOC` | Person | `id.loc.gov/authorities/names/{id}.json` |
| `open_library` | `idOpenLibrary` | Person | `openlibrary.org/authors/{id}.json` |
| `cbdb` | `idCBDB` | Person | `cbdb.fas.harvard.edu/cbdbapi/person.php?id={id}&o=json` |
| `yale_lux` | `idYaleLux` | Person | `lux.collections.yale.edu/data/{id}` |

**Link-only (4)** — public URL verified, no usable API; cited as 참고 링크 only:
`aks_ency`, `britannica`, `bnf`, `world_history`.

**Unsupported (7)** — no verified endpoint/URL; structured non-fatal
`status="unsupported"`, no fetch, no link: `nlk` (DNS failure — re-assess with an
NLK Open API key), `aks_kdp` (404), `ency_china` (empty body), `academia_sinica`
(unconfirmed resolution), `british_museum` (403 bot-block), `aks_map` (host
unreachable; the AKS Place API's own `Link` is used instead), `aks_sillok`
(**stored value is a person name, not an ID** — needs a data fix).

## AKS Digerati type-safety (critical fix)

Both AKS ports answer HTTP 200 for any number in their own namespace — verified:
`:85/7249` → 신응시 (wrong person for Place 개성), `:88/18816` → 대홍산 (wrong
place for Person 이규보) — and the returned record's own id *matches* the
request, so neither HTTP status nor an ID check can catch the mixup. Layers now
enforced (all failing closed, before or without HTTP):

1. **Request**: separate registry entries/endpoints; anchored full-prefix
   validation (`koreanPerson_<n>` vs `koreanPlace_<n>`); explicit `node_type` on
   every call; `resolve_source()` maps legacy `aks_digerati`+Place to the Place
   config.
2. **Response**: per-source schema validators (Person must carry
   `AkspId`/`PersonId` and no Place fields; Place the inverse) plus an
   ID-consistency check; rejects return `status="error"` with **no `data`**.
3. **Cache**: key = `source|node_type|original_id`; failures/rejections uncached.
4. **Evidence/synthesis**: Place authority keyed `aks_digerati_place` end-to-end;
   Person/Place never merge; claims tagged `[Person]`/`[Place]` with a
   cross-citation ban; only `status="ok"` records are cited as fetched.

Invariant (verified live and by tests): *no `koreanPlace_*` ID can reach the
Person endpoint, no `koreanPerson_*` ID can reach the Place endpoint, and no
mismatched HTTP 200 can become external factual evidence.*

## Authority call policy, caps, cache

- **Gating**: keyword cue gate split by entity type (Person: 생몰/biography/生平…;
  Place: 어디/location/位置…; an explicit cross-source comparison request also
  counts as an authority request). Poem-list/structural questions trigger
  **zero** external calls. `want_authority` allows explicit override.
- **Caps** (configurable via env or Streamlit secrets — `AUTHORITY_PERSON_CAP`,
  `AUTHORITY_PLACE_CAP`, `AUTHORITY_SOURCES_PER_ENTITY`; resolved once per
  process):
  - `DEFAULT_PERSON_AUTHORITY_CAP = 10`, `DEFAULT_PLACE_AUTHORITY_CAP = 5`,
    `DEFAULT_FETCHABLE_SOURCES_PER_ENTITY = 2`;
  - an explicit cross-source comparison raises the per-entity source limit only
    to the documented bounded ceiling `EXHAUSTIVE_SOURCES_PER_ENTITY = 4` — caps
    are never removed;
  - calls de-duplicated by `source|node_type|original_id`; merged duplicate
    entities consume cap capacity once.
- **Coverage transparency**: the orchestrator tracks
  `eligible_entity_count` / `enriched_entity_count` / `skipped_due_to_cap_count`
  per node type. When at least one eligible entity was skipped, a structured
  coverage claim is added and synthesis renders a localized "Authority Coverage"
  block that MUST appear in the answer (e.g. "외부 authority 보강은 관련 인물
  14명 중 10명에 적용했습니다…"). No note is shown when nothing was skipped, and
  synthesis rule 11 forbids claiming completeness while a note is present.
- **Never from a name**: entities without a valid authority ID are skipped.
- **Cache**: in-process, TTL 1 h, bounded 256 entries; successes only.
- **HTTP hygiene**: descriptive User-Agent, 5–8 s timeouts, JSON content-type
  check, 2 MB size cap, sequential fetches (no unbounded concurrency), no retry
  storms; failures degrade to `unavailable`/`error` and the graph/vector answer
  still returns.

## Conversation history (graphRAG normal path)

- The normal path loads the `::graphRAG` Neo4j history (never `::textRAG`),
  serializes it with `tools.synthesis.serialize_chat_history()` — last
  **8 messages**, **400 chars/message**, **2 400 chars total**, user/assistant
  roles only; tool traces, raw authority payloads, and error text are excluded
  by marker filtering.
- The bounded text goes to (a) graph retrieval, appended to the Cypher-generation
  question strictly for pronoun/ellipsis resolution, and (b) final synthesis
  under `HISTORY_RULES`: history is never evidence, ambiguous antecedents get a
  clarification question, missing referents are never filled from pretraining.
  Vector search embeds the current question only.
- Persistence ownership: the normal path saves user+assistant messages itself
  after a successful answer; the legacy ReAct fallback keeps its
  `RunnableWithMessageHistory` persistence — one owner per code path, no double
  save. History load/save failures never block an answer (empty history / skip).
- Retention: reads are strictly bounded by the serializer; stored history stays
  in Neo4j chat nodes (`:Message`/`:Session`), which the corpus prompts already
  exclude. Periodic ops cleanup remains a deployment task (documented decision).

## User-safe retrieval statuses

- Retriever failures are normalized in `tools/orchestrator.py`
  (`_safe_retrieve`/`_normalize_evidence_status`): raised exceptions and legacy
  `{"type":"error"}` claims become `{"source", "outcome"}` statuses; the
  exception text is logged with a correlation code and **stripped from
  Evidence** — it can never reach the synthesis prompt.
- Outcomes: `ok` / `no_results` (an answerable state, not an error) /
  `temporarily_unavailable` / `invalid_query`. Localized wording (ko/en/zh)
  lives in `tools.synthesis.RETRIEVAL_STATUS_MESSAGES` and renders as a
  "Retrieval Status" block; synthesis rule 10 requires relaying it briefly.
- One-source failure: the answer proceeds from the surviving source with a brief
  localized limitation note. Both sources `temporarily_unavailable` with no
  external claims: `synthesize_answer()` returns
  `retrieval_failure_message(lang)` directly — no LLM call, no pretraining.
- External authority failures keep their existing source-specific statuses and
  are not merged into retrieval statuses.

## textRAG wording

All user-facing/prompt text (bot.py greeting + sidebar help, text_rag.py system
prompt and docstring) now states: textRAG performs semantic vector search over
Entry texts, does **not** perform graph relationship reasoning or structured
relationship queries, and uses the Entry–Work containment relation only to
attach source/citation metadata. The graphRAG-switch advice for structural
questions is retained; tests assert the old inaccurate "no graph relationships"
claim is gone.

## API keys and rate limits

**No enabled source requires an API key**; nothing was added to secrets. All
enabled APIs are public (Wikidata CC0, LOC, OpenLibrary, CBDB academic, Yale LUX,
AKS Digerati). Any future key (e.g. NLK) must live in Streamlit secrets/env only.

## Compatibility notes

- Legacy tool input `wikidata:<Q>` / `aks_digerati:<koreanPerson_n>` still works;
  `aks_digerati:<koreanPlace_n>` now auto-routes to the Place config.
- `collect_person_entities()` and `person_entities_from_vector_meta()` kept as
  wrappers; `get_poetry_plot()` / `cypher_qa_safe()` retained for the ReAct
  fallback path.
- textRAG behavior and the Streamlit UI are untouched.

## Tests

```
python -m unittest tests.test_pipeline -v     # 79 tests, all passing
```

Covers: JSONL schema + full registry coverage of stored properties; ID
extraction (Person 16 IDs, Place 3 IDs, role-prefixed rows, no name-based
entities); routing and request blocking in both directions; response-schema and
ID-mismatch rejection; HTTP guards (timeout/429/404/bad JSON/content-type/
oversize); caps, de-dup, intent routing, link-only handling; synthesis
allowlists, bounded blocks, conflict retention, citation safety; cache
namespace isolation; Cypher template brace safety; conversation-history
serialization bounds/filtering, `::graphRAG` isolation, history-rules wiring;
textRAG wording accuracy; retrieval-status normalization (raw exception text
never in the prompt, no_results vs failure, both-failed safe message,
log-only diagnostics); coverage counts/truncation notes and cap configuration.
Mutation-checked (removing a registry entry fails the coverage test). No test
requires live Neo4j, Gemini, or external API access.

---

## Security & reliability hardening (2026-07-21)

Work order: `CLAUDE_CODE_SECURITY_RELIABILITY_HARDENING.md`.

### Phase 1 — Cypher read-only defence

- New module `tools/cypher_safety.py`.
- `validate_read_only_cypher(query)` strips comments / string / backtick
  literals, tokenises, and rejects any occurrence of `CREATE`, `MERGE`,
  `DELETE`, `DETACH`, `SET`, `REMOVE`, `DROP`, `ALTER`, `RENAME`, `GRANT`,
  `DENY`, `REVOKE`, `LOAD`, `FOREACH`, `USE`, multi-statement `;`, or any
  `CALL` (procedure OR subquery) that is not in a small allowlist. Adds or
  lowers a trailing `LIMIT` to the configured max_rows cap.
- `SafeNeo4jGraph` proxy validates every `.query()` before the driver call.
  Two variants: a plain-Python proxy for tests / mocks, and a `Neo4jGraph`
  subclass that pydantic accepts on `GraphCypherQAChain(graph=…)` and
  reuses the connected inner instance (no second Bolt driver).
- Both `cypher_qa` and `cypher_qa_structured` in `tools/cypher.py` are now
  built with `graph=safe_graph(graph)`; `Neo4jChatMessageHistory` still
  writes through the plain graph.
- `UnsafeCypherError` carries a correlation id but NOT the offending query;
  callers log the id and return `invalid_query` status to synthesis.
- 39 unit tests in `tests/test_cypher_safety.py` cover legitimate reads,
  every forbidden keyword, case / whitespace / comment / backtick bypasses,
  multi-statement, CALL, missing RETURN, LIMIT enforcement, correlation
  id secrecy, and the proxy's mock-friendly path.

### Phase 2 — Auth-gated lazy initialization

- `bot.py` top-level imports no longer touch `agent`, `text_rag`, `llm`,
  `graph`, or `tools.*`. `from agent import generate_response` moved into
  `handle_submit()` so `sys.modules` handles caching after the first
  post-auth call. AST-level regression test in `tests/test_phase2_auth_init.py`
  fails if any of those roots are re-added at top level.
- `hmac.compare_digest` replaces the naive `==` password check. Rate-
  limiting policy is documented as a deployment-layer concern.
- `utils.get_session_id()` gains a stable-per-process `fallback-<uuid>`
  return value when `get_script_run_ctx()` returns None (tests / CLI).

### Phase 3 — Exception taxonomy and fallback policy

- New module `errors.py` with `ConfigurationError`, `TransientProviderError`,
  `UnsafeQueryError`, `RetrievalError`, `ModelResponseError` and an
  `is_fallback_eligible(exc)` predicate. Every class carries a
  `correlation_id`.
- `agent.generate_response`'s "swallow any Exception → ReAct fallback"
  pattern is gone. Only `TransientProviderError` triggers the ReAct path.
  `UnsafeCypherError`, `UnsafeQueryError`, `ConfigurationError`,
  `RetrievalError`, the Gemini empty-stream `ValueError`, and unclassified
  exceptions all produce the localized safe message with a correlation id
  logged server-side.
- `handle_submit()` wraps the whole call in a try/except and shows a
  language-aware safe message with the correlation id — no raw stack
  trace ever surfaces on the Streamlit page.
- 16 tests in `tests/test_phase3_fallback_policy.py` verify that only
  transient failures fall back, no_results doesn't fall back, and every
  error path emits one correlation id.

### Phase 4 — Introspection-based arity dispatch

- `tools/orchestrator._safe_retrieve` and `_call_fetcher` no longer use
  `try: fn(3-args) except TypeError: fn(2-args)`. New helper
  `_fn_accepts_arity` uses `inspect.signature` to pick the right arity
  BEFORE calling. `*args` callables (MagicMock) are treated as compatible
  with any arity.
- A `TypeError` raised INSIDE the callable is no longer interpreted as an
  arity mismatch — it is a retrieval failure. Side-effectful mocks and
  fetchers are guaranteed to be invoked at most once.
- 12 tests in `tests/test_phase4_signature_dispatch.py` cover canonical /
  legacy dispatch, body-`TypeError` non-retry, and the end-to-end
  orchestrator behaviour.

### Phase 5 — Embedding client hardening

- `llm.GoogleEmbeddings` now:
  * Pins the model via `GOOGLE_EMBEDDING_MODEL` in secrets (safe fallback
    `models/gemini-embedding-001`), never auto-discovers in the request
    path. `discover_available_model()` is kept as an admin-only helper.
  * Uses a bounded retry policy: 3 attempts, exponential backoff capped at
    8s + jitter, honours `Retry-After`, never blocks a live session for
    60s. 4xx responses raise `ConfigurationError` (no retry). 429/5xx
    exhaustion raises `TransientProviderError`.
  * Validates every response: HTTP status, JSON content-type, `embedding`
    schema, numeric vector, batch-count match. Malformed responses raise
    `ModelResponseError`.
  * `embed_documents([])` returns `[]` with ZERO network calls.
  * Optional `expected_dim` catches silent server-side model swaps. Not
    supplied → first successful response pins the dimension.
- HTTP client is injectable (`requests.Session`) so 16 unit tests exercise
  every branch without a network.

### Phase 6 — Single-source `INDEX_BY_LANG`

- New module `rag_config.py` owns the per-language vector-index config.
  `tools/vector.py` re-exports from it; `text_rag.py` imports from it.
  Neither module carries a literal `INDEX_BY_LANG = {...}` block.
- `index_config_for(lang)` centralises the fallback to Korean when a
  language key is unknown.
- 8 regression tests (`tests/test_phase6_rag_config.py`) fail if either
  module reintroduces a local dict definition.

### Phase 7 — Documentation

- `.streamlit/secrets.toml.example` created listing every required key
  (APP_PASSWORD, GOOGLE_*, NEO4J_*) and every optional cap knob. The
  real `secrets.toml` is untouched.
- README (README.adoc) documents Neo4j read-only account requirement,
  embedding-model / dimension contract, and how to run `unittest` /
  Streamlit.

### Test totals

Baseline: 79 tests. After hardening: 175 tests, all passing on
`python -m unittest discover -s tests`. No live Neo4j / Gemini / network
access is required to run the suite.

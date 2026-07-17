# GraphRAG External Authority Pipeline — Implementation Note

Covers three work orders, applied in sequence:

1. **Evidence pipeline refactor** (`CLAUDE_CODE_REFACTOR_TASK.md`) — structured
   graph/vector/external evidence before one final synthesis LLM call.
2. **External authority expansion** (`CLAUDE_CODE_EXTERNAL_AUTHORITY_EXPANSION.md`)
   — registry-driven support for every Person/Place authority ID stored in Neo4j.
3. **AKS Digerati type-safety hardening** (`CLAUDE_CODE_AKS_DIGERATI_TYPE_SAFETY.md`)
   — strict Person/Place separation at request, response, cache, and citation level.

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
  Place: 어디/location/位置…). Poem-list/structural questions trigger **zero**
  external calls. `want_authority` allows explicit override.
- **Caps**: ≤ 3 Person entities, ≤ 2 Place entities per request; ≤ 2 fetchable
  sources per entity (lifted when the user explicitly asks for cross-source
  comparison). Calls de-duplicated by `source|node_type|id`.
- **Never from a name**: entities without a valid authority ID are skipped.
- **Cache**: in-process, TTL 1 h, bounded 256 entries; successes only.
- **HTTP hygiene**: descriptive User-Agent, 5–8 s timeouts, JSON content-type
  check, 2 MB size cap, no retry storms; failures degrade to
  `unavailable`/`error` and the graph/vector answer still returns.

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
python -m unittest tests.test_pipeline -v     # 60 tests, all passing
```

Covers: JSONL schema + full registry coverage of stored properties; ID
extraction (Person 16 IDs, Place 3 IDs, role-prefixed rows, no name-based
entities); routing and request blocking in both directions; response-schema and
ID-mismatch rejection; HTTP guards (timeout/429/404/bad JSON/content-type/
oversize); caps, de-dup, intent routing, link-only handling; synthesis
allowlists, bounded blocks, conflict retention, citation safety; cache
namespace isolation; Cypher template brace safety. Mutation-checked (removing a
registry entry fails the coverage test).

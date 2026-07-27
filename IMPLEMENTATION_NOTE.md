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
5. **Deterministic Sources & provenance validity**
   (`CLAUDE_CODE_DETERMINISTIC_SOURCES_AND_PROVENANCE_FIX.md`) — the Sources
   section is assembled in CODE, not by the LLM; `(?)` / `Entry 0` /
   `Entry None` placeholders are structurally impossible; structured citation
   de-duplication; deterministic body entity links; P553/P1227 internal-ID
   identity invariant.
6. **All-node source link coverage**
   (`CLAUDE_CODE_ALL_NODE_SOURCE_LINK_COVERAGE.md`) — CriticalTerm's `CT###`
   ids (692 of them) now validate; a single `POETRYTALKS_BASE_URL` constant
   backs every URL/prompt/regex; a new `NodeReference` contract covers all 9
   node classes (not just Person/Place); nested `collect()`/map results and
   multiple same-class ids per row are extracted, bounded; the mandatory
   "poetrytalks wikidata" citation group is narrowed to ids the finished
   answer actually references; body auto-linking now covers Work/Entry/Poem/
   Critique/Topic/Era/CriticalTerm, with word-boundary-safe English matching.
7. **Property-name casing correction** (live smoke test finding, no separate
   work order file) — the internal node identifier is `ID` (two uppercase
   letters), not `id`; every Cypher literal/prompt/rule reading a node's own
   id was fixed. See "Live smoke test — CORRECTED finding" below.
8. **Graph ranking/aggregation reliability**
   (`CLAUDE_CODE_GRAPH_RANKING_RELIABILITY_FIX.md`) — deterministic
   parameterized template for "most mentioned X" questions (bypasses
   free-form Cypher generation entirely for this question class); pre-flight
   detection + one bounded recovery retry for the malformed backtick
   multi-label Cypher shape; ranking-aware authority routing so a bare
   ranking question makes zero external calls and an explicit follow-up
   enriches only the winner; `no_results`/`invalid_query`/
   `temporarily_unavailable` kept distinct for ranking failures;
   McCune-Reischauer as the sole romanization field end-to-end; bounded
   read-only retry for transient Bolt failures. See "Section 8" below.
9. **textRAG → vectorRAG UI label** (`CLAUDE_CODE_TEXTRAG_UI_LABEL_TO_VECTORRAG.md`)
   — user-facing mode name changed from `textRAG` to `vectorRAG` via a new
   `mode_labels.py` display-label mapping; internal mode key `"textRAG"`,
   `messages_by_mode["textRAG"]`, and the `::textRAG` Neo4j history suffix
   all deliberately unchanged for session/history compatibility.
10. **Language-aware quotes & named Poetry Talks sources**
    (`CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md`) — parallel
    source-text fields (textEng/textKor/textChi, including role-prefixed
    `<role>_text_eng|kor|chi` and nested collect() results) now present in
    the locked response language's priority order in both graphRAG
    (Graph/Vector evidence) and the independent vectorRAG path; every
    "poetrytalks wikidata" citation shows the node's bilingual name when
    available (`[P094](url) — Du Fu (두보)`); the actual legacy Cypher
    alias mismatch that dropped names to ID-only is fixed; vectorRAG gained
    a custom `document_prompt` and now reuses graphRAG's deterministic
    Sources-assembly boundary instead of letting the LLM write its own. See
    "Section 9" below.
11. **Mixed-script language detection and routing**
    (`CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md`) —
    replaced the character-priority-order `detect_language()`
    (한글→한자→라틴, first script found wins) with a grammar-signal-scored
    classifier (new `tools/language_policy.py`) so a sentence's own grammar
    always outranks an inserted entity name's script (`How is 杜甫
    critiqued?` is now correctly `en`, not `zh`); split the single
    `effective_language` state into `question_language` (search/index
    selection) and `response_language` (final output/citations/errors),
    with `effective_language` kept as a `response_language` compatibility
    alias; language-control phrases ("Answer in Korean:", "영어로
    답변해줘.") are now stripped from the text used for retrieval. See
    "Section 10" below.

## Section 6 — All-node source link coverage (work order 6)

### Authoritative domain

Confirmed `https://poetrytalks.org/` as authoritative: it is what the
repository, prior deployment, and every existing citation already use, and it
is DNS-resolvable; the alternative spelling `poetrtalks.org` mentioned in the
work order's own draft text has **no DNS record** and is treated as a typo. The
domain is now a single constant, `tools.evidence.POETRYTALKS_BASE_URL`
(overridable via the `POETRYTALKS_BASE_URL` env var), that every URL builder,
the citation-parsing regex, the Cypher-generation prompt, and the vector
retrieval prompt derive from — no second hardcoded domain literal remains in
any URL-*constructing* code path.

### Node ID prefix registry (single source of truth)

`tools.evidence.NODE_ID_PREFIXES` — measured against
`neo4j_data_import/neo4j_import_nodes.jsonl` (8,232 unique ids, zero misses):

| Prefix | Class | Count |
|---|---|---:|
| B | Work | 116 |
| E | Entry | 921 |
| M | Poem | 1,771 |
| C | Critique | 1,828 |
| P | Person | 1,255 |
| L | Place | 545 |
| T | Topic | 1,060 |
| H | Era | 44 |
| **CT** | **CriticalTerm** | **692** |

`is_valid_node_id`/`split_node_id`/`node_type_for_id` use a longest-prefix
match (`^([A-Z]{1,2})(\d{1,4})$`, prefix checked against the registry) so
`CT017` (CriticalTerm) and `C017` (Critique) never collide, and an
unregistered prefix — including external ids like `Q464558`, `E0063034`
(idAKSency), or `koreanPerson_16062` — is rejected fail-closed, exactly like
the existing external-id protections. Two previously-passing tests
(`tests/test_poetrytalks_wikidata_group.py`) asserted the OPPOSITE of this
fail-closed policy using fictional placeholders (`K123`, `R042`) instead of
the real `CT###` shape — updated to test the real prefix and to correctly
assert that a truly unregistered prefix is rejected (this was defect 1's root
cause: the fictional test fixture masked the gap).

### `NodeReference` data contract

```python
NodeReference(node_id, node_type, name_kor=None, name_chi=None, name_eng=None,
              source_type=None, work_id=None, entry_id=None)
```

Parallel to `Entity` (which stays Person/Place-only, for authority
enrichment): `NodeReference` covers all 9 node classes and is the single
citation/link-coverage inventory. `make_node_reference(id, ...)` is the only
constructor — it validates the id via the prefix registry and returns `None`
for anything invalid (an external id can never produce one), and it always
trusts the ID-inferred type over a caller-supplied `node_type` argument
(logged, not silently accepted, if they disagree). `merge_node_references`
de-duplicates strictly by `node_id` — matching external ids or matching names
never merge two different node ids (`P553`/`P1227` sharing `idWikidata`, or
two same-named Persons, both stay separate; the case-order does not matter
because merge is transitive-free by construction: key is always the id).
`Evidence.node_references: list[NodeReference]` is a new, additive field
(included in `to_dict()`); `collect_node_references(*evidences)` merges the
graph+vector inventories, analogous to `collect_entities`.

### Nested/graph/vector extraction (bounded)

- `tools/vector.py`'s retrieval-query projection now includes `.id` on
  `topics[]`, `forms_types[]`, `critical_terms[]`, and `era` (contained
  poems/critiques already had it); `node_references_from_vector_meta()`
  builds a `NodeReference` for the Entry itself, its Work, the creator,
  every `mentioned_persons`/`audiences`/`places`/`topics`/`forms_types`/
  `critical_terms` item, `era`, and every `contained_poems`/
  `contained_critiques` item (carrying `work_id`/`entry_id` context).
- `tools/evidence.py` adds a bounded recursive walker
  (`node_references_from_graph_row`, `_MAX_WALK_DEPTH=6`,
  `_MAX_WALK_ITEMS=200`) that finds ids nested inside `collect()`/map results
  (`RETURN collect({id: poem.id, nameKor: poem.nameKor}) AS poems`) and
  preserves **every** id of the same class in one row (previously only the
  first). Safety: an id is only ever collected under an id-shaped **key**
  (`"id"`, `"<role>_id"`, excluding `idWikidata`/`idAKSency`-style prefixed
  external keys AND the `wikidata_id`/`aks_map_id`-style suffixed authority
  row aliases at every depth) — a string is never treated as an id just
  because it happens to fit the shape, so an id-looking token inside a
  `textKor`/`textChi` prose field is never picked up. This is additive
  (`provenance_from_graph_row`'s existing one-breadcrumb-per-row behavior for
  the graph evidence block is unchanged).
- `tools/cypher.py`'s prompt gained a general rule (no question-specific
  wording) requiring every returned node — not only Person/Place or
  aggregation subjects — to include its own internal id, with explicit
  `collect()`-map guidance (`{id, nameKor, ...}` per element).

### `referenced_node_ids` — cited vs. merely retrieved

Rather than adding a structured JSON output contract to the synthesis LLM
call (a new parsing-failure surface that would itself need the same "safe
fallback" the work order requires), `tools/answer_renderer.derive_referenced_node_ids(body, node_references)`
derives the referenced set **deterministically from the already-assembled
body text** — this IS the accepted fallback design (work order Phase 4,
requirement 5/6): it matches (a) explicit id-shaped tokens already known to
evidence and (b) names that map to exactly one node id (ambiguous/homonym
names are never resolved); it never raises, degrading to an empty list on any
internal error. `build_citations(evidence, language, referenced_node_ids=None)`
gained an **optional** parameter: `None` (every existing caller) preserves
the exact legacy behavior (all evidence ids cited); a list narrows the
mandatory "poetrytalks wikidata" bullet group to only those ids.

**Deliberate scope decision**: Work/Entry/Poem/Critique **provenance
breadcrumbs** (the `{graph_prefix}: ...` source-citation lines) are **NOT**
filtered by `referenced_node_ids` — every such breadcrumb corresponds to a
document actually placed in the evidence blocks the LLM read, so its
citation stays available even if the model's prose didn't literally repeat
the id (e.g. it quoted the poem text without typing "E031"). Only the
entity-mention "poetrytalks wikidata" group — the part defect 3 was actually
about — is narrowed. Silently dropping a legitimate source breadcrumb because
of imperfect text-overlap detection would be a worse regression than the
narrow over-inclusion defect 3 describes.

### Body linking — all node classes

`link_entities_in_body()` is unchanged in API; `agent.py` now feeds it a
combined list of Entity dicts (Person/Place) **and** `NodeReference` dicts
(all 9 classes, via `collect_node_references`), since both share the same
`node_id`/`name_kor/chi/eng` shape. Added: pure-ASCII names now match on a
`\b`-word-boundary (so "Yi" never matches inside "Yield"/"Yielding"); CJK
names keep the previous substring match (Korean/Chinese grammatical particles
attach with no space, so a strict boundary would break normal sentences like
"허난설헌은").

### Tests

`tests/test_all_node_source_link_coverage.py` — 48 new tests: full
9-class id resolution + JSONL 8,232-id/zero-miss coverage + CT/C
non-collision; `NodeReference` construction/merge invariants (P553/P1227,
C017/CT017, homonyms); vector-metadata and graph-row extraction for
Topic/Era/CriticalTerm/contained-Poem/Critique, multi-same-kind preservation,
nested-external-id exclusion, source-text-substring exclusion, bounded
deep/wide payloads; `referenced_node_ids` filtering (cited-only, unknown/
external ids excluded, dedup, never-raises, homonym skip, breadcrumb
retained-when-referenced); body linking for Work/CriticalTerm/Topic/Era/
Poem/Critique, bilingual no-double-link, English substring-false-positive
guard, body/Sources URL identity, full contract under non-compliant LLM
output. Two pre-existing tests in `test_poetrytalks_wikidata_group.py` were
corrected (fictional `K123` → real `CT017`; the "any prefix" assertion
flipped to "unregistered prefix is rejected", matching the newly-enforced
fail-closed policy). **313 tests total, all passing**
(`python -m unittest discover -s tests`).

### Live smoke test — CORRECTED finding: property-name casing, not missing data

**Correction to an earlier note in this document.** The first live smoke test
run reported the live database's `id` property as universally empty and
framed it as a data/import gap requiring a live-DB backfill (out of scope).
That conclusion was **wrong**. The user pointed out the property is spelled
`ID` (two uppercase letters), not `id`, and a direct query confirmed it:

```text
MATCH (p:Person) RETURN p.id AS id LIMIT 3          -- {'id': None} × 3
MATCH (p:Person) RETURN p.ID AS ID, p.nameKor LIMIT 3 -- {'ID': 'P001', 'name': '이규보'}, ...

MATCH (n) RETURN labels(n), count(n), count(n.ID)
Entry 921/921  Poem 1771/1771  Critique 1828/1828  Work 116/116
Person 1255/1255  Place 545/545  Era 44/44  CriticalTerm 692/692  Topic 1060/1060
```

Every corpus node **does** carry its id — 100% populated across all 9
classes — under the property name `ID`. Neo4j property names are
case-sensitive, so `n.id` silently returns `null` on a graph where the real
property is `n.ID`; there is no error, which is exactly why this went
undetected through 313 passing (mocked) tests and three "successful" live
smoke test runs that quietly produced zero citations.

**Root cause, precisely**: every Cypher-generation prompt example, the
"Standardized authority aliases" section, the aggregation/general id-return
rules (added in this same work order), and `tools/vector.py`'s hand-written
retrieval-query projection all instructed lowercase `.id`. The live
`Neo4jGraph` schema introspection (`{schema}` in the prompt) almost certainly
already reported the correct `ID` casing, but the hand-written few-shot
examples and rules likely dominated Gemini's output, since every observed
live query consistently generated lowercase `p.id`/`poem.id`/etc.

**Fix applied** (`tools/vector.py`, `tools/cypher.py`): every occurrence of
`.id` used as a NODE'S-OWN-identifier accessor (`node.id`, `p.id`, `w.id`,
`e.id`, `t.id`, `ct.id`, `pm.id`, `pl.id`, `c.id`, ...) changed to `.ID`, in:
the vector retrieval-query Cypher literal (entry/work/creator/mentioned-
persons/audiences/places/topics/forms_types/critical_terms/era/contained-
poems/contained-critiques projections); the Cypher-generation prompt's
"Structural Properties" doc (now states the property is case-sensitively
`ID`, with an explicit "`n.id` returns null" warning), "Standardized
authority aliases", the aggregation rule, the general all-node-id rule added
earlier in this work order, the citation-format examples, and every few-shot
example that read a node's own id. External authority properties
(`idWikidata`, `idAKSency`, `idAKSdigerati`, ...) were already correctly
cased and are untouched — only the bare internal identifier was affected.

**Re-verified live after the fix** — both retrieval paths now return real
ids end-to-end:

```text
Vector path ("Which woman is mentioned the most…", graph blocked by an
unrelated Cypher-syntax issue, vector supplied the evidence):
  vector node_references: [('E220','Entry'), ('B009','Work'), ('P009','Person'),
                            ('P044','Person'), ('T023','Topic'), ...]
  citations: "- poetrytalks wikidata: [E220](https://poetrytalks.org/E220)", ...
  (previously: 4× "vector provenance skipped ... metadata contract violation",
   zero citations — this warning no longer appears)

Graph path ("기고(奇古) 비평용어가 사용된 비평문을 알려줘"):
  graph_row: {'critique_id': 'C001', 'entry_id': 'E001', 'work_id': 'B001',
              'work_name_kor': '백운소설', ...}
  graph node_references: [('C001','Critique'), ('E001','Entry'), ('B001','Work'), ...]
  (previously: critique_id=None, entry_id=None, work_id=None — zero references)
```

No test changes were required by this fix (all 313 tests still pass) — the
Python-side parsing already worked correctly from row *aliases*
(`person_id`, `entry_id`, ...); only the Cypher-side property accessor
feeding those aliases was wrong. `git diff --check` clean;
`python -m unittest discover -s tests` still reports 313/313 passing.

**Remaining known limitation**: the multi-label `WHERE text:Entry OR
text:Poem OR text:Critique` backtick-quoting pattern is occasionally
generated malformed by Gemini (e.g. producing
`` text:`Entry OR text`:`Poem OR text`:`Critique `` and bleeding backticks
into the following `RETURN` line), which the existing Cypher safety
validator correctly rejects (`query has no RETURN clause`) and the
orchestrator correctly degrades to vector-only evidence for that turn. This
is a separate, pre-existing LLM Cypher-generation reliability issue
(non-deterministic prompt output), unrelated to the id-casing defect fixed
here, and out of scope for this correction.

## Deterministic Sources assembly (work order 5)

- **Assembly boundary** (`tools/answer_renderer.py`, new): the synthesis LLM
  writes the answer BODY only. `assemble_final_answer()` then (a) strips any
  model-authored Sources/References/출처/참고문헌/来源/參考資料 section (full-line
  header match at any markdown depth, bold, or bare keyword+colon — mid-sentence
  "source" words are never cut), (b) deterministically links the first mention
  of each evidence entity name to its Poetry Talks URL, and (c) appends the
  code-rendered, localized Sources section built from `build_citations()`.
  The assembled result is the only shape shown to the user and saved to the
  `::graphRAG` history. Retrieval-failure safe messages return before assembly
  and carry no Sources. Empty citations → no orphan header.
- **Provenance validity** (`tools/evidence.py`): `_linked_id(None)` now returns
  `''` (never `?`); `normalize_entry_position()` maps 0/negative/None/non-numeric
  to unknown. Vector provenance policy: valid `entry_id` → Entry citation
  (position only when > 0); valid `work_id` only → work-only citation (no faked
  position); neither → NO user-facing breadcrumb, only a correlation-ID
  diagnostic log (metadata contract violation). Graph aggregation rows keep
  `person_id`-anchored provenance even without an Entry breadcrumb.
- **Citation format & dedup** (`tools/synthesis.py`): breadcrumbs render as
  `Work name [B023](url) > Entry 31 [E031](url)` (no `)(` double parens);
  structured dedup keys `source_type + entry_id / poem_or_critique_id /
  entity_id / work_id / source_url` with a rendered-line fallback — the same
  Entry retrieved repeatedly yields one bullet while distinct node ids never
  collapse. Dirty fallback labels (containing `(?)`, `Entry 0`, …) are skipped.
- **Body entity links** (`link_entities_in_body`): only evidence entity names
  map to links; a name claimed by two node ids (동명이인) is never linked; only
  shape-valid node ids produce URLs; existing markdown links/code/URLs are
  never rewritten; failures degrade to the unlinked body.
- **Identity invariant**: `P553` 허초희 and `P1227` 허난설헌 share
  `idWikidata=Q464558` but are distinct graph nodes — `merge_entities()` /
  `collect_entities()` keep them separate (regression-tested, including the
  anonymous-bridge case), counts are never summed, and the Cypher prompt now
  contains a GENERAL rule (no question-specific text) that aggregation/ranking
  queries must return each subject's internal node id and must group by node,
  never by external identifier.
- **Live smoke finding (data-layer limitation, out of scope here)**: on the
  live Neo4j instance the vector retrieval projects `entry_position` correctly
  but `node.id` / Work `id` come back **null** (`entry_id=None`,
  `source_work_id=None`) — the live DB's nodes are missing the `id` property
  values that the local JSONL export carries. This was the true origin of the
  observed `Entry 31 (?)` citations. The pipeline now suppresses such
  breadcrumbs and logs `vector provenance skipped [<correlation>] … metadata
  contract violation`; restoring `id` properties on the live DB will
  automatically re-enable full citations. Fixing the DB is explicitly a
  non-goal of this work order.

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
python -m unittest discover -s tests -v       # 313 tests, all passing
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

## Section 8 — Graph ranking/aggregation reliability (work order 8)

(`CLAUDE_CODE_GRAPH_RANKING_RELIABILITY_FIX.md`) — resolves the "remaining
known limitation" flagged at the end of the ID-casing correction above: the
malformed backtick multi-label Cypher (`` text:`Entry OR text`:Poem ``) that
Gemini occasionally produced for ranking/aggregation questions. That bug is
now structurally unreachable for the question class it affected — ranking
questions never go through free-form Cypher generation at all.

### P0-A — Malformed-label pre-flight detection + one bounded recovery retry

`tools/cypher_safety.py`:
- New `reason_code` on every `UnsafeCypherError`: `malformed_label_predicate`,
  `missing_return`, `forbidden_keyword`, `disallowed_call`, `multi_statement`,
  `empty_query`, `not_a_string`. `RECOVERABLE_REASON_CODES` = the first two
  only — a plausible LLM query-*shape* slip. Everything else (write/schema/
  CALL/multi-statement) is never retried.
- `_detect_malformed_label_predicate()` runs on the RAW query, before string/
  comment stripping (so it still fires even when the malformed backtick
  swallows the following `RETURN`). Signature is narrow by design:
  `` `[^`]*\bOR\b[^`]*`\s*: `` — a backtick-quoted identifier containing "OR"
  immediately followed by another `:label`. This is deliberately narrower
  than "any backtick containing OR" — the first, broader regex attempt broke
  the pre-existing `test_backtick_label_cannot_smuggle_keyword` test (a
  harmless `` n:`OR DELETE` `` single-label case that must keep passing), so
  it was tightened to the exact chained-label signature.

`tools/cypher.py`'s `retrieve_graph_evidence()`: on a recoverable
`UnsafeCypherError` (or `CypherSyntaxError`), regenerates the Cypher exactly
ONCE with a generalized hint (`_SHAPE_RETRY_HINT`) that never re-embeds the
original query text. A second failure of any kind ends with a user-safe
status (`invalid_query`), never a third attempt.

### P0-B — Deterministic ranking template (new `tools/graph_intent.py`)

Corpus-wide "most mentioned X" questions are answered by a parameterized
Cypher template run directly through `SafeNeo4jGraph`, never by free-form LLM
Cypher generation, so the malformed-label failure mode is structurally
impossible for this question class.

- `is_graph_aggregation_intent(question)` — multilingual cue detector
  (English/Korean/Chinese: "most mentioned", "가장 많이 언급", "最多", ...).
- `ROLE_TYPE_CUES` — registry mapping a role key (currently only `"king"`) to
  its exact `nameKor`/`nameEng`/`nameChi` values. **Live-verified, not
  assumed**: a CONTAINS-based probe (`nameKor CONTAINS '왕'` etc., matching
  the ORIGINAL failing query's own predicate style) returned T1052 (king, 20
  Person) but ALSO T1069 (queen consort, 5), T1070 (queen, 1), T183 ("poems
  by kings and queens"), and T527 ("kingfisher" — via the English substring
  "king"). Switching to EXACT equality (`nameKor = '왕' OR nameEng = 'king'
  OR nameChi = '王'`) returns only T1052. The template therefore matches by
  exact equality, never CONTAINS — documented in code comments at both the
  registry and the query builder so this doesn't regress.
- `ROLE_RELATIONSHIP = "HAS_TYPE"` — also live-verified: the original failing
  query assumed `HAS_OFFICE`, but a direct query showed `HAS_OFFICE` has NO
  king/왕/王-matching Topic in this dataset at all; `HAS_TYPE → Topic(T1052)`
  is the real relationship (20 Person nodes connected).
- `build_role_ranking_query(role, limit)` — pure function, returns
  `(cypher, params)`. Mentions are counted as `count(DISTINCT e)` over
  `(Entry)-[:HAS_SUBJECT_PERSON]->(Person)` — see canonical-definition note
  below. Standard aliases (`person_id`, `person_name_mr`, `wikidata_id`, ...)
  match the existing row-parsing convention in `tools/evidence.py` so
  `graph_rows_to_evidence()` needs no changes to build Entities from the
  ranking rows. `limit` is embedded as a literal int (not `$limit`) —
  `SafeNeo4jGraph._ensure_limit`'s trailing-`LIMIT`-detector only recognizes
  `LIMIT <digits>`, and a `LIMIT $limit` placeholder would be invisible to it
  and produce an invalid double-`LIMIT` query (caught during implementation
  by a live run that raised `Neo.ClientError.Statement.SyntaxError` on
  exactly this).
- `rank_rows_by_mention_count(rows)` — winners are every row tied for the
  max `mention_count` (handles ties; the live data has none for "king", but
  the code path is exercised by unit tests with a synthetic tie).
- `tools/cypher.py`'s `retrieve_role_ranking_evidence(role)` is the thin,
  side-effecting wrapper: resolves the query, runs it through
  `_safe_graph.query()`, classifies the outcome (`no_results` / `invalid_query`
  / `temporarily_unavailable`), and — on success — records a `{"type":
  "ranking", "winner_person_ids": [...], "winner_count": N}` claim that
  `tools/orchestrator.py` uses to scope authority enrichment (P0-D).

**Canonical "most mentioned" definition — confirmed live, default chosen,
flagged for maintainer confirmation per the work order's own instructions**:
compared Entry-only `HAS_SUBJECT_PERSON` counts against Entry+Poem+Critique
combined counts for every king-cohort Person:

```text
Entry-only:              선조(P519)=10, 성종(P513)=6,  광해군(P960)=6
Entry+Poem+Critique:     선조(P519)=16, 성종(P513)=9,  광해군(P960)=8
```

Both definitions agree on the #1 result (선조, no tie either way), so the
choice does not change the "king" answer, but the two counts themselves
differ and only one can be "the" displayed number. **Entry-only is used as
the implemented default** (`tools.graph_intent.MENTION_COUNT_UNIT =
"entry"`) because an Entry and the Poems/Critiques nested under it via
`HAS_PART` can each separately carry a `HAS_SUBJECT_PERSON` edge to the same
Person — summing all three node types risks double-counting one narrative
mention. Per the work order's explicit instruction ("합의된 기준이 없으면
기본값으로 'Entry 단위의 HAS_SUBJECT_PERSON 관계 수'를 사용하되 ... 사용자에게
확인이 필요한 결정으로 기록한다"), **this is a default, not a confirmed
product decision** — a maintainer may prefer the combined count as "true"
mention volume; switching it is a one-line change to `build_role_ranking_query`.

### P0-C — Status normalization (mostly pre-existing; ranking-specific gaps closed)

The `no_results` / `invalid_query` / `temporarily_unavailable` three-way
status contract, and the localized `RETRIEVAL_STATUS_MESSAGES` that render
them distinctly, already existed from the context/authority-cap work order
(Section on "User-safe retrieval statuses" above) and needed no structural
change — `retrieve_role_ranking_evidence()` was written to emit the same
three outcomes the existing `_normalize_evidence_status` already
distinguishes correctly. What this work order added to `tools/synthesis.py`:
- **Rule 13** (`SYNTHESIS_SYSTEM_RULES`): for a ranking/aggregation question,
  the answer must come only from Graph Evidence rows carrying a
  `mention_count` field — never estimated from Vector Evidence text, and a
  `no_results` outcome must be phrased as "the search found no match", never
  as a negative world-fact ("there is no such king").
- **Rule 7d**: `poetrytalks wikidata` links (including Topic ids like
  `T1052`) are this project's own internal wiki pages, never real
  Wikidata.org records — "according to Wikidata" may only describe a
  FETCHED external-authority block whose source is literally `"wikidata"`.

### P0-D — Ranking-aware authority routing (`tools/orchestrator.py`)

`_PERSON_CUES` includes "who is" / "tell me about" — necessary for real
biography questions, but the reference bug question itself ("Who is the
most mentioned king...?") also contains "who is", which is exactly how the
original incident enriched 10 of 14 irrelevant vector-surfaced candidates.

- `gather_graphrag_evidence()` now checks
  `is_graph_aggregation_intent(question)` + `detect_role_cue(question)`
  BEFORE any authority-cue routing. When both match a registered role, the
  free-form `graph_retriever` is bypassed entirely in favor of
  `role_ranking_retriever` (default `retrieve_role_ranking_evidence`).
- A separate, narrower gate (`_ranking_authority_intent`) replaces
  `authority_intent()` for this path: `_PERSON_CUES`/`_PLACE_CUES` are not
  consulted at all; only an unambiguous external-source cue (`wikidata`,
  `위키데이터`, `encyclopedia`, or a `_COMPARE_CUES` match) opts in.
- When it does opt in, the candidate pool is `_extract_ranking_winner_ids()`
  — the winner id(s) from the ranking template's own claim — intersected
  with the collected Person entities, NEVER the full `persons` list (which
  still includes every vector-surfaced candidate for body-linking purposes).
  A failed/empty ranking yields an empty winner set, which forces 0 fetches
  regardless of cues.
- New return key `"ranking_role"` (e.g. `"king"` or `None`) for callers/tests.

**Live-verified end-to-end** (real Neo4j, real vector index, real external
HTTP — no LLM call, no chat-history write):

```text
Q: "Who is the most mentioned king in Sihwa ch'ongnim?"
   ranking_role=king  authority_attempted=False
   graph status=ok  vector status=ok
   winner=P519 (선조)  mention_count=10  (14 candidates total, matching the
   incident report's own "14 후보" figure)

Q: "시화총림에서 가장 많이 언급된 왕은 누구인가요?" (Korean equivalent)
   ranking_role=king  authority_attempted=False   (identical routing)

Q: "Who is the most mentioned king, and what does Wikidata say about
    that person?"
   ranking_role=king  authority_attempted=True
   external claims: [(P519, wikidata, ok), (P519, loc, ok),
                      (P519, aks_ency, link_only)]
   — ALL three claims are for P519 only; P513 (runner-up) and every
   vector-surfaced candidate received ZERO fetches.
```

Additionally covered by mocks (`tests/test_ranking_orchestration.py`):
"Compare external sources for the most mentioned king" (winner-only,
compare-cap policy applies), a tied-winner scenario (both tied Persons
become eligible), `want_authority=False` overriding an explicit cue, and a
failed-ranking scenario keeping fetches at 0 even with an explicit cue.

### P0-E — McCune-Reischauer (MR) romanization consistency

`nameMR` is now the single authoritative Latin-script field for Korean-
related entities end-to-end: `tools/vector.py`'s retrieval-query projection
(added `nameMR` to places/topics/forms_types/critical_terms/era/work,
removed the response-facing `nameRR`/`creator_rr` projections),
`tools/evidence.py` (`Entity.name_mr` / `NodeReference.name_mr` /
`Provenance.work_name_mr`, threaded through `make_node_reference`,
`merge_node_references`, `merge_entities`, `_person_from_flat`,
`_place_from_flat`, `_row_entity`, `document_to_parts`), `tools/cypher.py`'s
prompt (corrected the false "nameRR does not exist" claim, added
`person_name_mr`/`place_name_mr` standardized aliases, rewrote the Language-
of-Response section so `nameEng` is never silently used as if it were "the"
romanization when `nameMR` is populated), `tools/synthesis.py` (`_entity_line`
renders an explicit `MR=` field; new rule 12 tells the LLM to prefer it over
re-romanizing or reading `nameEng` as a romanization), and
`tools/answer_renderer.py` (`_entity_names` includes `name_mr` as a body-link
candidate, so an MR-only mention in the LLM's prose still gets linked).
`nameRR` is read into NOTHING anywhere in this path — it stays in each
`reserved` field-exclusion set purely so it never leaks into `authority_ids`.

Live-verified per node class (`total` / `with nameMR` / `with nameRR`):
Person 1255/214/35, Place 545/23/16, Work 116/68/1, Era 44/10/1,
Topic 1060/505/1. Person example showing the two fields genuinely differ
(a real risk if the wrong one were ever read): 이규보 `nameMR='Yi Kyubo'` vs
`nameRR='Yi Gyubo'`. 선조 (this work order's ranking winner) has no stored
`nameMR` (`nameEng='King Sŏnjo'` only) — confirms the "no MR → keep the
stored English name, never fabricate a romanization" fallback path is
exercised by the live answer, not just a hypothetical.

### P1 — Bounded read-only retry for transient Bolt failures

`tools/cypher_safety.py`: `SafeNeo4jGraph.query()` and
`_SafeNeo4jGraphSubclass.query()` now wrap their inner `.query()` call in
`_read_with_retry()` — up to `READ_RETRY_MAX_ATTEMPTS` (default 3, env-
overridable via `GRAPH_READ_RETRY_MAX_ATTEMPTS`) attempts with doubling
backoff from `READ_RETRY_BACKOFF_SECONDS` (default 0.5s, env-overridable),
retrying ONLY `ServiceUnavailable` / `SessionExpired` / `TransientError` /
`OSError` (the exact exception shapes behind the observed "Failed to read
from defunct connection ... OSError('No data')" log). Any other exception
(including `UnsafeCypherError`, which is raised and re-raised BEFORE the
retry wrapper is ever entered) propagates on the first attempt — retrying a
safety rejection or a logic error would just repeat it. Because this lives
inside `SafeNeo4jGraph`/`_SafeNeo4jGraphSubclass` specifically, it applies
uniformly to both the free-form LLM Cypher path and the new ranking
template, and never to `Neo4jChatMessageHistory` (writes) or external-
authority HTTP fetches (neither goes through this wrapper) — satisfying the
work order's "never apply this to write/history/authority paths" constraint
by construction rather than by a separate check. `graph.py` gained a
documenting comment only; no `driver_config` override was added; the
existing `neo4j`/`langchain_neo4j` version pair was not verified to need one
beyond the driver's own defaults, and the work order explicitly disallows
guessing unsupported connection arguments.

### Files changed / added (work order 8)

- New: `tools/graph_intent.py`, `tests/test_graph_intent.py`,
  `tests/test_ranking_orchestration.py`, `tests/test_mr_romanization.py`.
- Modified: `tools/cypher_safety.py` (reason codes, malformed-label
  detection, bounded read retry), `tools/cypher.py` (recovery retry wiring,
  ranking template wrapper, MR prompt fixes), `tools/orchestrator.py`
  (ranking-aware routing), `tools/synthesis.py` (rules 7d/12/13), `graph.py`
  (documenting comment only), `tools/vector.py` (MR/RR projection fixes),
  `tools/evidence.py` (name_mr plumbing), `tools/answer_renderer.py`
  (name_mr body-link candidate), `tests/test_cypher_safety.py` (29 new
  cases: malformed-label, reason-code classification, bounded retry).

### Test totals (work order 8)

315 → **390 tests**, all passing on `python -m unittest discover -s tests`.
No live Neo4j / Gemini / network access is required to run the suite —
every ranking/orchestrator test injects its retrievers and fetchers.

### Live smoke test summary (read-only for Neo4j; no chat-history write; no LLM call)

All items in work order §5.6 were run against the live database EXCEPT a
full `agent.synthesize_answer()` pass, which was deliberately skipped: that
path writes to `Neo4jChatMessageHistory` (a real write against the live
Neo4j instance) and calls the live Gemini API, both outside the "쓰기 없는"
(no-write) scope §5.6 states for ALL of its verification items, including
item 4. Everything read-only-verifiable was run instead, directly against
`tools.orchestrator.gather_graphrag_evidence()` with its REAL (non-mocked)
graph/vector retrievers — see the P0-D section above for the exact
transcript. Confirmed live: correct winner + count, zero UnknownLabelWarning
(the template never uses the failing backtick shape), no "no RETURN"
rejection, zero irrelevant authority fetches for the bare question, and
exactly the winner enriched (3 external claims, all for P519) for the
explicit-Wikidata follow-up.

### Remaining limitations / decisions for the maintainer

1. **Mention-count canonical definition** (see P0-B above) is a default
   (Entry-only `HAS_SUBJECT_PERSON`), not a confirmed product decision — the
   work order explicitly requires flagging this rather than silently
   picking one.
2. **Role registry currently covers only `"king"`.** Extending
   `tools.graph_intent.ROLE_TYPE_CUES` to other role/type questions (e.g.
   "most mentioned queen/monk/official") requires the same live
   exact-vs-CONTAINS verification documented above for each new role before
   adding it — do not copy the pattern blind.
3. **No full LLM synthesis run was performed** for the ranking question (see
   live-smoke-test note above) — the final prose output (rule 12/13
   compliance in an actual model response, not just the rule text existing
   in the prompt) is unverified beyond what the existing prompt-content
   tests can pin statically. A maintainer with authorization to write test
   chat-history rows and spend LLM quota should run
   `agent.synthesize_answer("Who is the most mentioned king in Sihwa
   ch'ongnim?", "en")` once to close this gap.
4. **Bounded read retry (P1) was verified with synthetic mocks**
   (`ServiceUnavailable` raised twice then succeeding, and exhausting all
   attempts), not by reproducing an actual live Bolt disconnect — that
   failure mode is inherently hard to trigger on demand against a healthy
   Aura instance.

## Section 9 — Language-aware quotes & named Poetry Talks sources (work order 10)

(`CLAUDE_CODE_LANGUAGE_AWARE_QUOTES_AND_NAMED_SOURCES.md`) — fixes two
related defects observed in a live English graphRAG response to "How is Du
Fu critiqued in Sihwa Ch'ongnim?": quoted source text always appeared
`textKor -> textEng` regardless of response language, and every
"poetrytalks wikidata" Sources bullet was ID-only even for nodes whose name
WAS present in the generated Cypher row — because the actual alias shapes
Gemini produced (`subject_name_kor`/`critic_name_kor`/`critical_term_kor`)
didn't match what the extraction code expected
(`subject_person_name_kor`/`critic_person_name_kor`/
`critical_term_name_kor`).

### Phase 1 — Language-locked source-text presentation order

`tools/synthesis.py`:
- `source_text_priority(language)` — single source of truth: `en` ->
  (textEng, textKor, textChi); `ko` -> (textKor, textEng, textChi); `zh` ->
  (textChi, textKor, textEng); unsupported/unknown -> `ko`'s order (existing
  project-wide language-fallback convention).
- `reorder_source_text_fields(value, language)` — pure, recursive dict/list
  copy that reorders any sibling family of parallel source-text fields
  (bare `textEng/textKor/textChi`, and role-prefixed
  `<role>_text_eng|kor|chi`, e.g. `critique_text_eng`) at ANY nesting depth
  (top-level and inside `collect()`/map results), in the locked language's
  priority order. Values are never altered — only presentation order
  changes; a family with only one member present is a no-op; unrelated
  keys/values pass through in their original relative position; the input
  is never mutated.
- `_format_graph_block(graph, outcome, language)` — signature extended with
  `language`; each row is passed through `reorder_source_text_fields`
  before `json.dumps()` (previously the raw Cypher `RETURN` key order —
  effectively arbitrary — went straight into the prompt).
- `_format_vector_block(...)` — the previous FIXED tuple
  `("textChi", "textKor", "textEng", "descEng")` is now
  `source_text_priority(language) + ("descEng",)` — `descEng` always stays
  last, per the work order's explicit ordering rule.
- `SYNTHESIS_SYSTEM_RULES` rule 8 extended to state the ordering policy
  explicitly (so the LLM doesn't re-shuffle multi-language quotes back to
  Korean-first out of habit); new rule 8b on using the locked-language name
  in body prose (the system's own Sources section already shows both names
  bilingually).

### Phase 2 — Legacy Cypher alias normalization (the actual root cause)

`tools/evidence.py`:
- New `_LEGACY_SIBLING_NAME_KEYS` — an EXPLICIT (never a generic
  stem-stripping transform) map from an exact `id_key` to its legacy name
  aliases: `subject_person_id` -> `subject_name_kor|chi|eng|mr`,
  `critic_person_id` -> `critic_name_kor|chi|eng|mr`, `critical_term_id` ->
  `critical_term_kor|chi|eng|mr` (no `_name_` infix at all for this one).
  Deliberately narrow and per-id_key so a role can NEVER inherit another
  role's name (e.g. `subject_person_id` must never read
  `critic_name_eng`) — this constraint is enforced by a dedicated
  regression test (`test_no_broad_fallback_across_unrelated_roles`).
- `_row_entity()` (Person/Place extraction) and `_sibling_names_for_id_key()`
  (generic all-node-class NodeReference walker, used for `critical_term_id`
  among others) both consult this map as a fallback AFTER the standardized
  `<stem>_name_kor|chi|eng|mr` form and BEFORE the bare-camelCase
  (`nameKor`) form.

`tools/cypher.py` prompt:
- The "MULTI-HOP results" guidance now shows the full paired ID+name alias
  block (`subject.ID AS subject_person_id, subject.nameKor AS
  subject_person_name_kor, ...`) with an explicit "WRONG (do not do this):
  `critic_name`, `critical_term_kor`" callout.
- The two few-shot examples that previously taught the WRONG pattern
  outright (the 최치원 critical-term example used bare `critic.nameKor AS
  critic_name` with no ID at all; the 이백 intertextual example used
  `critic.nameKor AS critic_name`) are rewritten to the standardized
  alias contract, and now also demonstrate the `<role>_text_eng|kor|chi`
  convention Phase 1 depends on.

**Live-verified this was the actual fix**, not just a plausible theory: a
live `agent.synthesize_answer("How is Du Fu critiqued in Sihwa
Ch'ongnim?", "en")` run against the SAME live database, AFTER this phase's
prompt fix, produced Cypher reading:

```cypher
RETURN subject.ID AS subject_person_id,
       subject.nameKor AS subject_person_name_kor,
       ...
       critic.ID AS critic_person_id,
       critic.nameKor AS critic_person_name_kor,
       ...
```

— i.e. Gemini's own generated Cypher changed to the corrected alias shape
once the prompt taught it correctly, and the resulting Sources bullet came
out named: `- poetrytalks wikidata: [P094](https://poetrytalks.org/P094) — Du Fu (두보)`.

### Phase 3 — Named "poetrytalks wikidata" citations

`tools/synthesis.py`:
- `_collect_node_names(graph, vector)` — `node_id -> {name_kor, name_chi,
  name_eng}`, merged from `node_references` (primary, all node classes)
  then `entities` (Person/Place, fills gaps only) — first-seen non-empty
  wins, mirroring `merge_node_references()`'s policy but operating on the
  already-serialized dict shape `build_citations()` receives.
- `_format_citation_name(name_kor, name_eng, name_chi, language)` — renders
  the ` — <primary> (<secondary>)` suffix per the work order's exact rule
  list: en -> nameEng primary, differing nameKor in parens; ko -> nameKor
  primary, differing nameEng in parens; zh -> nameChi primary (existing zh
  convention) but nameKor/nameEng preserved as secondary, never both
  dropped; only one present -> that one alone; equal-after-trim -> shown
  once; neither present -> `""` (caller keeps the existing ID-only bullet);
  nameMR/nameChi NEVER substitute for a missing nameEng in the en/ko cases
  — only zh may prioritize nameChi.
- `build_citations()`'s "poetrytalks wikidata" bullet loop now appends this
  suffix after the existing `[id](url)` link — the link itself is
  UNCHANGED, preserving every existing substring-based test's compatibility
  (verified: all 409 pre-existing tests still pass unmodified).
- Identity/safety invariants unchanged and re-verified: citation identity
  is still solely the internal `node_id` (P553/P1227 sharing an external
  Wikidata id stay two separate, distinctly-named bullets); external
  authority ids (Q-ids, `koreanPerson_*`, `idAKSency` codes) never appear as
  a Poetry Talks node id.
- Chose NOT to implement Phase 3 item 6's optional live batch name-backfill
  query (`MATCH (n) WHERE n.ID IN $ids`) — see "Remaining limitations"
  below.

### Phase 4 — Independent vectorRAG (`text_rag.py`) brought onto the same contract

New pure module `tools/vectorrag_prompt.py` (no streamlit/neo4j/llm
import — same testability rationale as `tools/graph_intent.py`):
- `quoted_text_block(meta, language)` — language-ordered, null-omitted
  rendering of the three parallel source texts.
- `provenance_block(meta)` — Entry ID/name/position + Work ID/name +
  poetrytalks_link, omitting absent parts (never `Entry None`).
- `prepare_documents_for_prompt(docs, language)` — returns NEW Document
  objects (inputs never mutated) whose metadata gains these two computed
  keys, needed because a raw `{field}` placeholder in a LangChain
  `PromptTemplate` renders a missing/null metadata value as the literal
  string `"None"` — the null-aware composition has to happen in Python,
  not in the template.
- `document_prompt_for_lang(language)` — the custom `PromptTemplate` passed
  to `create_stuff_documents_chain(..., document_prompt=...)`.

`text_rag.py` changes (internal function/session-suffix NAMES unchanged,
per the work order's explicit constraint and the prior UI-label work
order's own non-goal list):
- `_build_light_retrieval_query()` gained `entry_name_kor`/`entry_name_eng`
  — the only piece missing for `tools.evidence.node_references_from_vector_meta`
  (which already reads exactly these two keys) to name the Entry itself.
- `_get_text_retriever_for_lang()` now caches a composite Runnable
  (`(lambda x: x["input"]) | base_retriever | RunnableLambda(prepare_fn)`)
  instead of the bare `.as_retriever()` object — `create_retrieval_chain`
  only auto-extracts `x["input"]` for a genuine `BaseRetriever` instance,
  so once the retriever becomes a composite `RunnableSequence` the
  extraction step must be included explicitly, or the retriever would
  receive the whole input dict instead of the query string.
- `_build_prompt()`'s system message dropped the old self-written Sources
  section instructions (18 lines of per-language label examples) and the
  hardcoded "always quote Chinese first" bilingual rule, replacing both
  with: write the body only (the system appends Sources deterministically),
  and the context's `quoted_text_block` is already in this session's
  response-language order — don't reorder it back.
- `generate_text_rag_response()`: after the chain returns, `result["context"]`
  (the prepared Documents) normalizes via `docs_to_evidence()` and flows
  through the EXACT SAME `build_citations()` + `assemble_final_answer()`
  boundary graphRAG uses — so a fake Sources section the model writes
  anyway is discarded and replaced exactly once, identical to the graphRAG
  guarantee. `RunnableWithMessageHistory` still auto-saves the raw
  pre-assembly body to Neo4j chat history unchanged (a deliberate,
  documented scope decision — see "Remaining limitations").

**Live-verified end-to-end** (real Neo4j vector index, real Gemini call,
both English and Korean):
- English: "How is Du Fu critiqued..." -> body quotes English-first, Sources
  header appears exactly once, named Work citations
  (`[B023](...) — Hogok's Remarks on Poetry (호곡시화)`), unnamed Entries
  correctly ID-only.
- Korean: moonlight-in-poetry question -> Korean-first quoting, `## 출처`
  header exactly once.

### Phase 5 — External authority path: regression-verified, not modified

No code in `tools/orchestrator.py` or `tools/external_authority.py` was
touched. Confirmed via both a live `agent.synthesize_answer()` run (the Du
Fu critique question produced **zero** external authority claims — a
critique-relation question is not a biography/location/external-source
intent, so the gate correctly stayed off) and new mock-based regression
tests (`tests/test_external_authority_regression_lang_order.py`):
reproduction question -> 0 fetches; explicit biography question -> fetches
only graph-stored valid ids; `status=ok`/`link_only` + URL -> citation;
`unavailable`/`error` -> no citation, no asserted fact.

Four potential follow-up risks were identified but deliberately NOT fixed
in this work order (per its own explicit "found but out of scope" list):
missing scheme/hostname re-validation on final external citation URLs, no
upper bound on authority-cap environment overrides, possible link-only
references skipped at the entity-cap boundary, and a possible mismatch
between the count of external claims shown in-prompt vs. the full citation
traversal range.

### Files changed / added (work order 10)

- New: `tools/vectorrag_prompt.py`, `tests/test_source_text_language_order.py`,
  `tests/test_named_poetrytalks_citations.py`,
  `tests/test_vectorrag_document_prompt.py`,
  `tests/test_external_authority_regression_lang_order.py`.
- Modified: `tools/synthesis.py` (language-order helpers, named-citation
  helpers, rules 8/8b), `tools/evidence.py` (`_LEGACY_SIBLING_NAME_KEYS`,
  `_row_entity`/`_sibling_names_for_id_key` fallback), `tools/cypher.py`
  (MULTI-HOP guidance + two corrected few-shot examples), `text_rag.py`
  (retriever composition, document_prompt, deterministic Sources
  assembly, prompt rule rewrite).
- Untouched (regression-verified only): `tools/orchestrator.py`,
  `tools/external_authority.py`, `tools/vector.py` (graphRAG's own vector
  projection already carried the right fields), `bot.py`, `agent.py`.

### Test totals (work order 10)

409 (post work-order-8/9 baseline) -> **487 tests**, all passing on
`python -m unittest discover -s tests -p "test_*.py"`. No live Neo4j /
Gemini / network access is required to run the suite —
`tools/vectorrag_prompt.py` was specifically extracted into its own
side-effect-free module so Phase 4 could be unit-tested without importing
`text_rag.py` (which constructs live Gemini/Neo4j clients at import time,
per this project's established Phase-2 lazy-import convention).

### Live smoke test summary

All of §7.1/7.2/7.3's manual-verification scenarios were run live (real
Neo4j, real Gemini, English AND Korean, both graphRAG and independent
vectorRAG) rather than only unit-tested — see the Phase 2 and Phase 4
sections above for transcripts. Every exact-format assertion from the work
order (`— Du Fu (두보)` / `— 두보 (Du Fu)`) was reproduced by a real model
response, not just a hand-built fixture.

### Remaining limitations / decisions for the maintainer

1. **Phase 3 item 6's optional live batch name-backfill query was not
   implemented.** The work order frames it conditionally ("보장해야
   한다면") for the residual case where a retrieval result carries only an
   id and the LLM's own Cypher genuinely omitted any name field despite
   the corrected prompt instructions. Given Phase 2 fixes the actual
   observed root cause (alias mismatch, not absent projection) and the
   acceptance criteria's "이름이 없는 node는 ID-only로 fallback한다"
   requirement is already satisfied without it, this was scoped out to
   avoid adding a new live Neo4j round-trip (latency + failure surface) to
   every graphRAG turn without a demonstrated remaining need. A maintainer
   who observes ID-only citations for nodes with real DB-side names after
   this fix ships should revisit this.
2. **vectorRAG's chat history still stores the pre-assembly body**, not
   the final Sources-appended answer — `RunnableWithMessageHistory` saves
   automatically during `.invoke()`, before the deterministic assembly
   step runs. This mirrors the PRE-EXISTING vectorRAG history behavior
   (unlike graphRAG, which manually saves the fully assembled output) and
   was left as-is to avoid restructuring history persistence, which is
   outside this work order's stated scope; conversation history is only
   ever used for pronoun/reference resolution, never re-cited as fact.
3. **`build_citations()` for vectorRAG uses `referenced_node_ids=None`**
   (the pre-existing legacy default — every retrieved node id is included,
   not narrowed to only the ones the model's prose actually mentioned).
   graphRAG's optional narrowing (`agent.py`'s `derive_referenced_node_ids`)
   was not ported to vectorRAG, consistent with the work order's own
   explicit non-goal ("검색된 모든 graph provenance를 본문 사용 여부로
   추가 필터링하는 정책" is out of scope).

## Section 10 — Mixed-script language detection and routing (work order 11)

(`CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md`) — the
existing `bot.py::detect_language()` picked a language by FIRST SCRIPT
FOUND (한글 → 한자 → 라틴, in that priority order), so a single inserted
Chinese entity name could outvote an entire English sentence:
`How is 杜甫 critiqued?` was misclassified `zh`, which then queried the
Chinese vector index and could steer the whole answer into Chinese. This
work order replaces that character-priority heuristic with a grammar-signal
score, and separates "what language to search with" from "what language to
answer in" — a single question can legitimately need both, once a user
locks a response language ("Answer in Korean: How is 杜甫 critiqued?").

### New pure module: `tools/language_policy.py`

No streamlit/agent/text_rag/Neo4j/LangChain/LLM import — stdlib only
(`re`, `unicodedata`, `dataclasses`, `typing`), so it is fully unit-testable
without triggering `bot.py`'s page-config/auth side effects.

- **`detect_question_language(text)`** — scores each of ko/en/zh as
  `grammar_matches * GRAMMAR_WEIGHT(5) + min(content_units, CONTENT_CAP(6))`
  and returns the highest-scoring language. Grammar signals: English
  question words/copulas/imperatives (`who/what/how/is/are/tell me/...`,
  word-boundary + case-insensitive); Korean question words/sentence
  endings (`누구/어떻게/-는가/-나요/...`) plus a hangul-attached PARTICLE
  check (`[가-힣](은|는|이|가|을|를|에서|에게)(?=[^가-힣]|$)` — requires the
  particle to be attached to an actual Korean word, so `Fu는` or `(杜甫)는`
  never fires just because a bracketed/Latin-suffixed name happens to
  precede it); Chinese question/narrative markers (`如何/评价/什么/谁/...`,
  both simplified and traditional) plus sentence-final particles
  (`吗/嗎/呢`). Content signals: Latin word-token count (a run of
  ASCII + Latin-1 Supplement/Extended-A/B letters with internal
  apostrophes, so `Ch'ongnim`/`Hŏ` count as ONE token, matching this
  project's McCune-Reischauer spellings), Hangul syllable count
  (`[가-힣]`), Han character count (`[㐀-䶿一-鿿]`). Tie-break (§4.5):
  highest score → most grammar matches → most content units → fixed
  `ko → zh → en` order on a full tie (implemented by listing candidates in
  that order and using Python's `max()`, which keeps the FIRST maximal item
  — deterministic regardless of dict/set iteration order) → `ko` when there
  is no script signal at all (digits/emoji/punctuation only).
- **`detect_language_control(text)` / `remove_language_control(text, spans)`**
  — port the pre-existing `bot.py::EXPLICIT_LOCK_PATTERNS`/
  `RELEASE_LOCK_PATTERNS` (same trigger set, same first-match-wins
  semantics) into this module as the single source of truth, extended so a
  match's span can be removed cleanly: Korean lock patterns now absorb the
  trailing verb conjugation and sentence terminator
  (`영어로 답변해줘.` → the WHOLE clause, not just `영어로 답변`), and the
  `请用<lang>` Chinese forms are tried BEFORE the bare `用<lang>` forms so
  `请用英文回答。` is captured as one span instead of leaving a dangling
  `请` behind (the bare-form regex would otherwise match starting at `用`,
  since `.search()` doesn't require a match at position 0). A generic
  leading/trailing punctuation+whitespace cleanup handles the remaining
  cases (e.g. `Answer in Korean: ...` leaves a bare `:` that the English
  patterns don't need to special-case).
- **`resolve_languages(prompt, locked_language=None)`** — returns a
  `LanguageResolution(original_prompt, question_text, question_language,
  response_language, locked_language, action, control_only, debug_info)`.
  `action` is `"lock"` when a lock phrase is present (even if a release
  phrase ALSO matched in the same message — lock wins, per work order
  §6.3), `"release"` when only a release phrase matched, else `"none"`.
  `control_only` is true when nothing but the control phrase was left in
  the text after stripping — bot.py uses this to skip the RAG backend
  entirely for a pure "change my language setting" message.

**Live-verified against the exact reproduction input**, both at the pure
function level and through the full `bot.py`-simulated session-state →
`agent.generate_response()` path (real Neo4j Cypher generation, real
Gemini call):

```text
"How is 杜甫 critiqued?"
  question_text = "How is 杜甫 critiqued?"   (nothing to strip)
  question_language = en                      (was "zh" before this fix)
  response_language = en
  → generated Cypher matched on subject.nameChi CONTAINS '杜甫' (correct —
    that's the entity lookup, unrelated to response language) and the
    FINAL ANSWER was written in English, as required.
```

### Session-state contract (`bot.py`)

Three keys now coexist: `question_language` (vector-index selection),
`response_language` (LLM directive/Sources/citation order/error text/
external-authority locale), `locked_language` (the `response_language`
override once a user says "answer in X"). `effective_language` is kept,
always equal to `response_language` — every pre-existing reader of
`effective_language` (fallback tests, other modules not yet migrated)
keeps working unchanged. The real regex/scoring logic that used to live in
`bot.py` (`detect_language`, `EXPLICIT_LOCK_PATTERNS`,
`RELEASE_LOCK_PATTERNS`, `detect_explicit_lock`, `detect_release_request`)
is now a thin wrapper delegating to `tools.language_policy` — kept for any
other code that might still reference those names, with a lazy
(function-body-local) import so `bot.py`'s pre-existing "never import
`tools.*` at module top level" hardening rule (Phase 2 auth-gated lazy
init, `tests/test_phase2_auth_init.py`) is not violated even though
`tools.language_policy` itself has zero heavy side effects.

Control-only turns (e.g. "Answer in English", "자동 언어 감지") get a new
deterministic, localized confirmation message (`_LOCK_ONLY_CONFIRMATION`/
`_RELEASE_ONLY_CONFIRMATION` in `bot.py`) and never reach
`agent.generate_response()`/`text_rag.generate_text_rag_response()` — no
Sources section, no graph/vector/external call.

### graphRAG propagation (`agent.py`, `tools/orchestrator.py`, `tools/vector.py`)

- `agent.synthesize_answer(user_input, response_language,
  question_language=None)` — `question_language` is new and OPTIONAL,
  defaulting to `response_language` when omitted (silent-behavior-preserving
  for any caller still passing a single language value).
  `gather_graphrag_evidence` is called with `question_language` positionally
  (unchanged slot) and the new `response_language=` keyword; retrieval
  failure messages, evidence formatting, citations, and final assembly all
  use `response_language`.
- `agent.generate_response(user_input)` reads BOTH
  `st.session_state["response_language"]` (falling back to
  `effective_language`) and `st.session_state["question_language"]`
  (falling back to the resolved response language), then passes both
  through — including into the legacy ReAct fallback path, whose
  `language_directive` uses `response_language` while `Action Input:` still
  carries the untranslated `user_input` (which is already `question_text`
  by the time it reaches `agent.py`, since `bot.py` passes
  `resolution.question_text`, never the raw prompt).
- `tools.orchestrator.gather_graphrag_evidence()` gained an optional
  keyword-only `response_language` (default: equal to the positional
  `language` argument — this is the exact backward-compatibility mechanism:
  every existing single-language caller, including every retriever lambda
  signature already exercised by `tests/test_pipeline.py`, is completely
  unaffected). The positional `language` argument's role is unchanged
  (question/vector-index language, forwarded to `vector_retriever` exactly
  as before); the new `response_language` is forwarded to the external
  authority fetcher's `language` argument and returned in the result dict
  as both `question_language` and `response_language` (plus the original
  `language` key, untouched, for any code still reading it). The graph
  retriever was already language-agnostic (`_default_graph_retriever`
  ignores its `language` parameter entirely) — no change needed there
  beyond confirming it stays that way.
- `tools/vector.py`: `get_poetry_plot()` (the legacy ReAct tool) now selects
  its retriever via `question_language` (falling back to
  `effective_language` for old sessions) while `_build_prompt()` uses
  `response_language` (same fallback chain) — previously both read the same
  `effective_language` key, which is exactly the bug this phase closes for
  the legacy path too. `retrieve_sihwa_evidence(query, language=None)`'s
  docstring now states explicitly that `language` here means
  QUESTION/INDEX language, not response language; its session-state
  fallback (only exercised by direct/legacy callers — the orchestrator
  always passes `language` explicitly) now prefers `question_language` over
  `effective_language`.

### Independent vectorRAG propagation (`text_rag.py`, `tools/vectorrag_prompt.py`)

The pre-existing `_get_text_retriever_for_lang(lang)` baked
`prepare_documents_for_prompt(docs, lang)` INTO the cached per-language
Runnable closure — harmless when question and response language were
always the same value, but wrong the moment they split: the cached
retriever would keep preparing documents (and therefore ordering quoted
text) in whatever language it was FIRST built for, regardless of the
CURRENT turn's actual response language.

Fix: the cache now holds only `(lambda x: x["input"]) | base_retriever`,
keyed by `question_language`. `generate_text_rag_response()` appends
`RunnableLambda(lambda docs: prepare_documents_for_prompt(docs,
response_language))` FRESH on every call, and passes `response_language`
into both `_build_prompt()` (now takes it as an explicit parameter instead
of reading `st.session_state` internally, so it can never drift from what
the caller already resolved) and `document_prompt_for_lang()`.
`generate_text_rag_response()` itself gained optional `response_language`/
`question_language` parameters (mirroring `agent.synthesize_answer`'s
pattern), defaulting to session-state reads when omitted — `bot.py`'s
existing single-argument call site (`generate_text_rag_response(message)`)
is unaffected.

**Live-verified the exact split combination the work order mandates**
(`question_language=en, response_language=ko`): a real Gemini call against
the real `EntryTextsEng` vector index produced a Korean-language answer
(the model translated the retrieved English source text into Korean prose,
as instructed) with the `## 출처` Sources header appearing exactly once.

### Phase 5 — downstream policy preserved, verified not modified

`rag_config.py`, `tools/synthesis.py`, `tools/answer_renderer.py`, and
`tools/external_authority.py` were not touched in this work order (`git
status` confirms only `agent.py`, `bot.py`, `text_rag.py`,
`tools/orchestrator.py`, `tools/vector.py` were modified, plus the new
`tools/language_policy.py`) — `INDEX_BY_LANG` is unchanged, no new
language was added beyond ko/en/zh, the response-language-ordered source
text priority from work order 10 is untouched, and external-authority
API/URL/cap selection logic is untouched (only the `language` VALUE now
reaching it via `response_language` changed, not the logic that consumes
it).

### Files changed / added (work order 11)

- New: `tools/language_policy.py`, `tests/test_language_policy.py`,
  `tests/test_language_routing.py`.
- Modified: `bot.py` (resolve_languages wiring, control-only handling,
  compat wrappers), `agent.py` (`synthesize_answer`/`generate_response`
  question/response split), `tools/orchestrator.py`
  (`gather_graphrag_evidence`'s `response_language` keyword),
  `tools/vector.py` (`get_poetry_plot`/`_build_prompt`/
  `retrieve_sihwa_evidence` split), `text_rag.py` (retriever-cache split,
  `_build_prompt`/`generate_text_rag_response` explicit `response_language`
  parameter), `tests/test_deterministic_sources.py` +
  `tests/test_vectorrag_document_prompt.py` (two stale literal-substring
  assertions updated for the `user_language` → `response_language` rename;
  no assertion was weakened, only the searched-for identifier name).

### Test totals (work order 11)

551 → started from 487 (post work-order-10 baseline) and grew to
**551 tests**, all passing on `python -m unittest discover -s tests
-p "test_*.py"`. No live Neo4j/Gemini/network access required.

### Live smoke test summary

Beyond the pure-function fixture/matrix verification (§4.6/§9.1/§9.2, all
passing), three full live end-to-end runs were made (real Neo4j, real
Gemini):
1. `agent.generate_response("How is 杜甫 critiqued?")` with
   `question_language=response_language=en` (as `resolve_languages` itself
   computed) — generated Cypher, English-language final answer, confirming
   the exact work-order reproduction case is fixed.
2. `agent.generate_response("How is Du Fu critiqued in Sihwa Ch'ongnim?")`
   in the prior (work order 10) session already confirmed the
   English-grammar/named-citation path; re-run here implicitly via the
   same code paths.
3. `text_rag.generate_text_rag_response(question, response_language="ko",
   question_language="en")` — confirmed the EntryTextsEng index was
   searched while the final answer, quoting order, and Sources header were
   Korean.
`streamlit run bot.py --server.headless true` was also started and
confirmed to boot with HTTP 200 and no import/runtime errors, then
stopped.

### Remaining limitations / decisions for the maintainer

1. **Grammar-cue lists are a closed, hand-curated set** (work order §4.2's
   own recommended minimum) — a question using none of these cues and
   relying purely on script-content fallback (e.g. a terse phrase with no
   question word) still resolves correctly via the content-count fallback,
   but a maintainer adding new phrasing patterns to the assistant's
   supported question styles should extend `_EN_GRAMMAR_WORDS`/
   `_KO_GRAMMAR_PHRASES`/`_ZH_GRAMMAR_PHRASES` deliberately, the same way
   this work order did, rather than assuming the content-count fallback
   alone will keep disambiguating correctly as more mixed-script questions
   are added.
2. **NFC normalization is applied to the DETECTION working copy only**;
   span removal for control-phrase stripping operates on the ORIGINAL
   (non-normalized) text for simplicity, since real user input is
   overwhelmingly already NFC-composed in practice. An NFD-typed control
   phrase (rare) would still be correctly DETECTED (detection normalizes
   first) but its span might not align perfectly against the original
   string in a pathological case; this was judged an acceptable, documented
   simplification given no fixture in the work order exercises NFD control
   phrases specifically.
3. **`_generate_response_react`'s tools (`get_poetry_plot`, `cypher_qa_safe`)
   still read session state independently** rather than receiving
   `question_language`/`response_language` as explicit call arguments —
   `get_poetry_plot` was updated to read the correct NEW session keys
   (`question_language` for retrieval, `response_language` for its prompt),
   but this is still an implicit session-state read, not a parameter
   threaded through the ReAct `AgentExecutor`'s tool-calling machinery
   (which does not support that without a broader refactor of the tool
   definitions themselves — out of scope for this work order, and the
   ReAct path is already documented elsewhere as a rarely-used fallback).

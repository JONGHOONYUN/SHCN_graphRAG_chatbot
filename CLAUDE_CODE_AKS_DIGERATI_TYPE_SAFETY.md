# Claude Code Work Order: AKS Digerati Person/Place Type-Safety Hardening

## Severity and Goal

This is a **critical data-integrity and citation-safety fix**.

The Neo4j property `idAKSdigerati` is shared by two node types but represents different authority namespaces:

```text
Person.idAKSdigerati = koreanPerson_<integer>
Place.idAKSdigerati  = koreanPlace_<integer>
```

AKS Digerati returns HTTP `200` even when a Place numeric ID is sent to the Person endpoint. It may return a completely unrelated Person record with the same numeric database ID. Therefore, HTTP status handling alone cannot detect the error.

Implement strict node-type, ID-prefix, endpoint, response-schema, response-ID, and cache separation so a Person and a Place can never be cross-enriched.

## Confirmed Production-Risk Example

```text
Neo4j Place ID: koreanPlace_7249

Correct request:
https://digerati.aks.ac.kr:88/api/IdValues/7249
-> HTTP 200, Place/Location record (e.g. 개성 / 開城)

Incorrect request:
https://digerati.aks.ac.kr:85/api/IdValues/7249
-> HTTP 200, unrelated Person record
```

The wrong response is dangerous because it looks successful and can be passed to the LLM as an externally fetched fact. It must be impossible for this request to occur.

## Files in Scope

- `tools/external_authority.py`
- `tools/evidence.py`
- `tools/orchestrator.py`
- `tools/vector.py`
- `tools/cypher.py`
- `tools/synthesis.py`
- `tests/` (add/update unit tests)
- `docs/external_authority_sources.md`
- `IMPLEMENTATION_NOTE.md`

## Required Invariants

All of these must hold:

1. `koreanPerson_<n>` can only use the AKS Person source and verified Person endpoint (`:85`).
2. `koreanPlace_<n>` can only use the AKS Place source and verified Place endpoint (`:88`).
3. A Person-pattern ID sent with `node_type="Place"`, or a Place-pattern ID sent with `node_type="Person"`, fails before any HTTP request.
4. A response with the wrong schema or mismatched returned ID is rejected even if HTTP status is 200.
5. Cache entries are separated by source, node type, and original authority ID.
6. Rejected, invalid, mismatched, unavailable, and failed responses must never become external factual evidence for final LLM synthesis.
7. The final answer must not cite a source as fetched if its result was rejected or unavailable.

## 1. Separate the Registry Entries

### `tools/external_authority.py`

Create two distinct registry entries. They must not share an endpoint, validator, parser, or cache namespace merely because their Neo4j property name is the same.

```python
AuthoritySourceConfig(
    key="aks_digerati",
    neo4j_property="idAKSdigerati",
    node_types={"Person"},
    request_url="https://digerati.aks.ac.kr:85/api/IdValues/{id}",
    ...,
)

AuthoritySourceConfig(
    key="aks_digerati_place",
    neo4j_property="idAKSdigerati",
    node_types={"Place"},
    request_url="https://digerati.aks.ac.kr:88/api/IdValues/{id}",
    ...,
)
```

Requirements:

- Preserve compatibility for existing Person calls using source key `aks_digerati`.
- Use `aks_digerati_place` as the explicit internal key for Place calls.
- Pass `node_type` explicitly to `fetch_authority()`; do not infer it from only the numeric tail.
- Reject source/node-type combinations not permitted by the registry.

## 2. Validate the Full Prefix Before Transforming IDs

Implement and use exact patterns:

```python
RE_AKS_PERSON = re.compile(r"^(?:koreanPerson_)?(\d+)$")
RE_AKS_PLACE = re.compile(r"^(?:koreanPlace_)?(\d+)$")
```

Important: the underscore is required in the prefixed form. Do **not** use patterns such as `koreanPerson*` or `koreanPlace*`; they do not validate the intended string.

Requirements:

- Use `fullmatch()` or anchored patterns only.
- Validate the original ID string before extracting its numeric tail.
- Person source accepts only `koreanPerson_<n>` or the documented legacy bare numeric form, if legacy support is required.
- Place source accepts only `koreanPlace_<n>` or the documented legacy bare numeric form, if legacy support is required.
- If bare numeric IDs are retained, require the caller’s `node_type` to choose the namespace. A bare number must never be auto-routed based on guesswork.
- On validation failure, return a structured result such as:

```python
{
    "source": "aks_digerati",
    "node_type": "Person",
    "id": "koreanPlace_7249",
    "status": "error",
    "error": "authority ID prefix does not match Person source",
}
```

- Validation failure must occur before the HTTP fetcher is called.

## 3. Validate the API Response, Not Only the Request

The endpoint and prefix protections are necessary but not sufficient. Add response-side verification before returning `status="ok"`.

### Person response acceptance

Accept only a response matching the documented Person schema, such as a list whose selected record contains Person-specific identifiers:

```text
AkspId and/or PersonId
```

Reject a response that appears to be a Place/Location schema.

### Place response acceptance

Accept only a response matching the documented Place schema, such as a list whose selected record contains Place-specific identifiers:

```text
AksloId and/or LocationId
```

Reject a response that appears to be a Person schema.

### ID consistency check

For both node types:

1. Extract the numeric request ID only after prefix validation.
2. Extract the appropriate numeric identifier from the parsed API record.
3. Compare the returned identifier with the requested numeric ID.
4. If they differ, return `status="error"` with a non-sensitive diagnostic and discard parsed data.

Example rejection:

```python
{
    "source": "aks_digerati_place",
    "node_type": "Place",
    "id": "koreanPlace_7249",
    "status": "error",
    "error": "response schema or identifier does not match requested Place authority record",
}
```

Do not expose raw response bodies to the final LLM or user-facing error message.

## 4. Implement a Dedicated Place Parser

Create a parser separate from `parse_aks_digerati()` for Place records, e.g.:

```python
parse_aks_digerati_place(data, requested_numeric_id) -> dict
```

Only extract verified fields that the Place endpoint actually returns and that are appropriate for a Place answer, for example after confirming the API schema:

- Korean/Chinese names
- place/location identifier
- canonical link
- permitted geographic or classification fields

Do not reuse Person fields such as birth/death years, aliases in the Person sense, or examination entries.

Update final-synthesis allowlists so `aks_digerati` and `aks_digerati_place` have distinct allowed fields.

## 5. Preserve Node Type Through the Full Evidence Pipeline

### `tools/evidence.py`

- Every extracted entity must retain `node_type`.
- A `Place` with `idAKSdigerati` must become an authority record keyed as `aks_digerati_place`, not `aks_digerati`.
- Person-only IDs (Wikidata, CBDB, etc.) must not be attached to Place entities.
- De-duplication must include node type. A Person and Place with the same numeric authority tail must never merge.

### `tools/vector.py` and `tools/cypher.py`

- Preserve each returned entity’s Neo4j node ID, node type, name fields, and authority IDs.
- Make Place authority metadata explicit:

```text
idAKSdigerati -> aks_digerati_place when node_type == Place
idAKSmap      -> aks_map
idAKSency     -> aks_ency
```

### `tools/orchestrator.py`

- Make source selection registry-driven using both `node_type` and authority key.
- Pass the entity node type to `fetch_authority()`.
- De-duplicate calls by `source | node_type | original_authority_id`.
- Keep Person and Place enrichment caps separate.

## 6. Cache Isolation

Change the cache key from any form equivalent to:

```text
source:id
```

to:

```text
source|node_type|original_authority_id
```

Examples:

```text
aks_digerati|Person|koreanPerson_7249
aks_digerati_place|Place|koreanPlace_7249
```

Requirements:

- Successful Person and Place results with the same numeric suffix must never share a cache entry.
- Failures/rejections remain uncached.
- Add regression tests for cache separation.

## 7. Synthesis and Citation Safety

### `tools/synthesis.py`

- Include only `status="ok"` records with validated source, node type, response schema, and response ID in external factual evidence.
- Display `aks_digerati_place` as a human-readable source label such as `AKS Digerati (Place)`.
- For `error`/`unavailable` results, say only that the relevant authority data could not be used; do not provide parsed fields or infer facts.
- Citation URLs must come from validated response `canonical_link` values or verified registry URLs.
- Never cite a Person result in a Place answer, or a Place result in a Person answer.

## 8. Mandatory Tests

Add mocked tests. No live API or Neo4j access is required.

### Request blocking

```python
def test_place_id_cannot_reach_person_endpoint():
    calls = []
    result = fetch_authority(
        "aks_digerati",
        "koreanPlace_7249",
        node_type="Person",
        fetcher=lambda url: calls.append(url),
    )
    assert result["status"] == "error"
    assert calls == []
```

Also add the reverse test:

```python
def test_person_id_cannot_reach_place_endpoint():
    calls = []
    result = fetch_authority(
        "aks_digerati_place",
        "koreanPerson_18816",
        node_type="Place",
        fetcher=lambda url: calls.append(url),
    )
    assert result["status"] == "error"
    assert calls == []
```

### Correct routing

- `koreanPerson_18816` + `Person` uses only `:85/api/IdValues/18816`.
- `koreanPlace_7249` + `Place` uses only `:88/api/IdValues/7249`.
- Assert the wrong port is absent from every generated request URL.

### Response validation

- HTTP 200 Person-schema response for a Place request is rejected.
- HTTP 200 Place-schema response for a Person request is rejected.
- Correct schema with a mismatched returned numeric ID is rejected.
- Correct schema and matching ID is accepted.
- Rejected responses do not include `data` in final external evidence.

### Pipeline and cache regression

- A Place entity never produces `aks_digerati` Person enrichment.
- A Person entity never produces `aks_digerati_place` enrichment.
- Same numeric suffix in Person/Place IDs yields two cache namespaces, never a shared cached response.
- Final evidence formatting and citations omit rejected results.

## 9. Documentation and Completion Criteria

Update `docs/external_authority_sources.md` with a clearly marked **Critical safety finding** section documenting:

- the 200-success/wrong-record behavior;
- the Person `:85` and Place `:88` endpoint distinction;
- representative IDs and observed schemas;
- request-side and response-side validation rules;
- cache isolation policy;
- test names proving regression coverage.

Update `IMPLEMENTATION_NOTE.md` with changed files and compatibility notes.

The task is complete only when all mandatory tests pass and the following statement is true:

> No `koreanPlace_*` authority ID can reach the AKS Digerati Person endpoint, no `koreanPerson_*` authority ID can reach the Place endpoint, and no mismatched HTTP 200 response can be used as external factual evidence.

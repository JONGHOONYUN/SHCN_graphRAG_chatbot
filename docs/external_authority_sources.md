# External Authority Source Capability Matrix

**Phase 1 delivery gate.** Every capability below was verified by an actual HTTP
request using a **real ID stored in `neo4j_data_import/neo4j_import_nodes.jsonl`**.
No source is marked `fetchable` on the basis of documentation alone, and no
endpoint path was guessed.

Verified on **2026-07-17** with `User-Agent: SihwaGraphRAG/0.2 (academic research chatbot)`.

Legend:
- **fetchable** — official JSON API verified with a stored ID; parsed fields may
  be cited as *fetched facts*.
- **link_only** — no usable public API, but the public URL pattern was verified
  (HTTP 200). May be shown as a 참고 링크 only; contents must never be asserted.
- **unsupported** — endpoint/URL pattern could not be verified, is bot-blocked,
  or the stored value is not a usable identifier. No fetch, no link.

## Source data (verified against the JSONL)

Schema confirmed: top-level `ID`, `label`, and node fields under `properties`
(no `labels` array). Node counts: Entry 921, Poem 1771, Critique 1828, Work 116,
Person 1255, Place 545, Era 44, CriticalTerm 692, Topic 1060.

---

## Person sources

| Registry key | Neo4j property | Count | Example ID | Capability | Endpoint / URL verified | Notes |
|---|---|---:|---|---|---|---|
| `wikidata` | `idWikidata` | 375 | `Q2913717` | **fetchable** | `GET https://www.wikidata.org/wiki/Special:EntityData/{id}.json` → 200 JSON | Pre-existing handler. Labels/descriptions/aliases/P569/P570. |
| `aks_digerati` | `idAKSdigerati` | 739 | `koreanPerson_18816` | **fetchable** | `GET https://digerati.aks.ac.kr:85/api/IdValues/{int}` → 200 JSON | Pre-existing. `koreanPerson_<n>` → `<n>` for the request only; cite `Link`. |
| `loc` | `idLOC` | 161 | `n82037407` | **fetchable** | `GET https://id.loc.gov/authorities/names/{id}.json` → 200 JSON (MADS/RDF) | Verified: returns `authoritativeLabel` (`Yi, Kyu-bo, 1168-1241`, `李 奎報`, `이 규보`), `variantLabel`s, `birthDate` 1168, `deathDate` 1241. |
| `open_library` | `idOpenLibrary` | 96 | `OL1304292A` | **fetchable** | `GET https://openlibrary.org/authors/{id}.json` → 200 JSON | Verified: `name` `Yi, Kyu-bo`, `birth_date` 1168, `death_date` 1241, `remote_ids` (viaf/wikidata/isni/lc_naf) — enables cross-source ID confirmation. |
| `cbdb` | `idCBDB` | 116 | `0103442` | **fetchable** | `GET https://cbdb.fas.harvard.edu/cbdbapi/person.php?id={id}&o=json` → 200 JSON | Verified: `BasicInfo` (`李齊賢`/`Li Qixian`, 1287–1367, Dynasty 元), `PersonAliases` (字 仲思, 諡號 文忠, 別號 益齋), `PersonAddresses`. Invalid id → HTTP 422 JSON error (graceful). |
| `yale_lux` | `idYaleLux` | 98 | `person/a6a10198-…` | **fetchable** | `GET https://lux.collections.yale.edu/data/{id}` → 200 JSON-LD (Linked Art) | Verified: `_label` `Yi, Kyu-bo, 1168-1241`, nested `born`/`died` timespans, `identified_by` names. Deeply nested → strict field extraction only. |
| `aks_ency` | `idAKSency` | 364 | `E0043772` | link_only | `https://encykorea.aks.ac.kr/Article/{id}` → 200 **HTML** | No public JSON API found. Link verified for Person (`E0043772`) and Place (`E0065119`). |
| `britannica` | `idBritannica` | 53 | `biography/Yi-Kyu-Bo` | link_only | `https://www.britannica.com/{id}` → 200 HTML | Stored value already contains the path segment. No public API. |
| `bnf` | `idBNF` | 58 | `123214461` | link_only | `https://data.bnf.fr/ark:/12148/cb{id}` → 200 HTML | data.bnf.fr returned HTML, not JSON, for `#about.json`. SRU exists but returns XML for bibliographic records, not this authority id — **not** verified for these IDs → link only. |
| `world_history` | `idWorldHistory` | 7 | `Choe_Chiwon` | link_only | `https://www.worldhistory.org/{id}/` → 200 HTML | No API. |
| `british_museum` | `idBritishMuseum` | 15 | `14547` | **unsupported** | `https://www.britishmuseum.org/collection/term/BIOG{id}` → **403** (Cloudflare bot block) | Could not verify the URL resolves to the right person; bot-blocked. Scraping is out of scope per the work order. |
| `ency_china` | `idEncyChina` | 89 | `213586` | **unsupported** | `https://www.zgbk.com/ecph/words?id={id}` → 200 but **empty body** (0 bytes) | URL pattern unverifiable; content is JS-rendered. No API. |
| `academia_sinica` | `idAcademiaSinica` | 35 | `018284` | **unsupported** | `https://newarchive.ihp.sinica.edu.tw/sncaccgi/sncacFtp?@{id}` → 200 HTML | Could not confirm the ID resolves to the stored person; no documented API. |
| `nlk` | `idNLK` | 142 | `KAC200105537` | **unsupported** | `lod.nl.go.kr` → **DNS/connection failure** | The LOD service did not resolve from this environment. Re-assess before enabling; may need an authenticated NLK Open API key. |
| `aks_kdp` | `idAKSkdp` | 46 | `EXM_MN_6JOb_1616_005166` | **unsupported** | `people.aks.ac.kr/front/tabCon/exm/exmView.aks?exmId={id}` → **404** | URL pattern not verified. Do not guess. |
| `aks_sillok` | `idAKSsillok` | 4 | `송인(宋寅)` | **unsupported** | — | **The stored value is a person name, not an identifier.** No URL can be built. Data-quality issue; excluded by design. |

## Place sources

| Registry key | Neo4j property | Count | Example ID | Capability | Endpoint / URL verified | Notes |
|---|---|---:|---|---|---|---|
| `aks_digerati_place` | `idAKSdigerati` | 367 | `koreanPlace_7249` | **fetchable** | `GET https://digerati.aks.ac.kr:88/api/IdValues/{int}` → 200 JSON | **Distinct schema** from Person: `AksloId`, `LocationId`, `Source`, `ChName`, `KoName`, `Link`. Verified `7249` → 開城/개성. |
| `aks_ency` | `idAKSency` | 64 | `E0065119` | link_only | `https://encykorea.aks.ac.kr/Article/{id}` → 200 HTML | Same handler as Person; node-type agnostic link. |
| `aks_map` | `idAKSmap` | 297 | `DYD_11_02_0073` | **unsupported** | `kostma.aks.ac.kr` e-map → **SSL/connection failure** | The AKS Place API returns a `Link` of this form itself; we surface **that API-provided link** rather than constructing one from `idAKSmap`. Direct construction is unverified. |

---

## ⚠ Critical safety finding: Person/Place port confusion

### Observed behavior (verified live, 2026-07-17)

The two AKS Digerati ports answer **HTTP 200 for any number that exists in their
own namespace**, in BOTH directions:

| Request | Response |
|---|---|
| `:85/api/IdValues/18816` (correct Person) | 200 — `AkspId=18816`, 이규보 李奎報 1168–1241 |
| `:88/api/IdValues/7249` (correct Place) | 200 — `AksloId=7249`, 개성 開城, `LocationId=DYD_13_04_0008` |
| **`:85/api/IdValues/7249`** (Place number → Person port) | **200 — `AkspId=7249`, 신응시 (unrelated person)** |
| **`:88/api/IdValues/18816`** (Person number → Place port) | **200 — `AksloId=18816`, 대홍산 (unrelated place)** |

Two consequences:

1. **HTTP status handling cannot detect the error** — the wrong request succeeds.
2. **An ID-equality check alone cannot detect it either**: the wrong record's own
   id (`AkspId=7249` / `AksloId=18816`) *matches* the requested number, because
   each port's number is a primary key in its own namespace. Only the **response
   schema** distinguishes the namespaces.

Observed schemas:

- Person (`:85`): `AkspId, PersonId, Source, ChName, KoName, Gender, YearBirth,
  YearDeath, Link, aks_PersonAliases, aks_Address, aks_Entry`
- Place (`:88`): `AksloId, LocationId, Source, ChName, KoName, Link`

### Enforcement (request side)

- `koreanPerson_<n>` and `koreanPlace_<n>` have **separate registry entries**
  (`aks_digerati` / `aks_digerati_place`) with distinct endpoints, parsers,
  allowlists, and **distinct normalized id-keys** in `Entity.authority_ids`.
- ID validation uses **anchored `fullmatch`** on the ORIGINAL string, prefix
  included (`^(?:koreanPerson_)?(\d+)$` / `^(?:koreanPlace_)?(\d+)$`), before the
  numeric tail is extracted. A cross-namespace or malformed id fails with
  `status="error"` **before any HTTP request is built**.
- `node_type` is passed explicitly to `fetch_authority()`; a bare numeric id is
  routed by the caller's node type, never by guesswork.
- `tools/evidence.py` re-keys a Place's `idAKSdigerati` to `aks_digerati_place`
  during extraction, and drops a `koreanPerson_*` value found on a Place.

### Enforcement (response side — defense in depth)

Before `status="ok"` is returned, the raw payload must pass a per-source
`response_validator`:

- Person: record must carry `AkspId`/`PersonId`, must NOT carry Place fields
  (`AksloId`/`LocationId`), and `AkspId` must equal the requested number.
- Place: record must carry `AksloId`/`LocationId`, must NOT carry Person fields,
  and `AksloId` must equal the requested number.
- On failure: `status="error"` with a non-sensitive message, **no `data` field**,
  not cached, never cited. Raw bodies are never exposed.

### Cache isolation

Cache key = `source_key|node_type|original_authority_id`, e.g.
`aks_digerati|Person|koreanPerson_7249` vs
`aks_digerati_place|Place|koreanPlace_7249` — same numeric suffix, two
namespaces, never a shared entry. Failures/rejections are not cached.

### Regression coverage (tests/test_pipeline.py)

- `test_place_id_cannot_reach_person_endpoint` / `test_person_id_cannot_reach_place_endpoint`
- `test_person_and_place_use_distinct_handlers` (wrong port asserted absent)
- `TestAksResponseValidation`: `test_place_schema_rejected_for_person_request`,
  `test_person_schema_rejected_for_place_request`,
  `test_mismatched_returned_id_rejected`, `test_matching_schema_and_id_accepted`,
  `test_rejected_response_not_cached`, `test_rejected_result_never_becomes_evidence`
- `test_cache_key_separates_node_types`, `test_place_entity_never_uses_person_source`,
  `test_person_and_place_never_merge`

## Operational policy

- **API keys:** none of the enabled sources require authentication. No secrets
  were added. Any future key must live in Streamlit secrets/env only.
- **Rate limits / attribution:** all requests send a descriptive User-Agent;
  timeout 5 s (Wikidata/AKS) – 8 s (LUX/LOC, larger payloads); no retry storms;
  results cached with TTL. Wikidata (CC0), LOC, OpenLibrary (public APIs), CBDB
  (academic use), LUX (Yale, public API) — cited by URL in every answer.
- **Failure policy:** timeout / 4xx / 5xx / invalid JSON / wrong content-type /
  oversized body all degrade to a structured `unavailable` or `error` status; the
  graphRAG answer still returns graph/vector evidence.
- **Response size guard:** responses over 2 MB are rejected unparsed.

## Re-assessment queue (intentionally not enabled)

`nlk` (142 ids — highest-value unsupported source; needs a reachable endpoint or
an NLK Open API key), `aks_kdp` (46), `ency_china` (89), `academia_sinica` (35),
`british_museum` (15), `aks_sillok` (4 — needs a data fix first), `aks_map` (297 —
usable today only via the AKS Place API's own `Link`).

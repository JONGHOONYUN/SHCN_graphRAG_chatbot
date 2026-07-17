"""Unit tests for the graphRAG evidence + external authority pipeline.

Runs with the stdlib unittest runner (no pytest):

    python -m unittest tests.test_pipeline -v

Neo4j, the vector retriever, and HTTP are mocked via dependency injection /
unittest.mock. No live API key, no live Neo4j, no live authority service, and no
network call is made by this suite.

Covers the work-order Test Plan items 1–15 (see class docstrings).
"""

import ast
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tools.evidence import (  # noqa: E402
    Entity,
    Evidence,
    collect_entities,
    collect_person_entities,
    docs_to_evidence,
    entities_from_graph_row,
    graph_rows_to_evidence,
    merge_entities,
)
from tools.orchestrator import (  # noqa: E402
    authority_intent,
    gather_graphrag_evidence,
    needs_authority,
)
from tools import external_authority as ea  # noqa: E402
from tools import synthesis  # noqa: E402

JSONL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "neo4j_data_import", "neo4j_import_nodes.jsonl"
)


def person(node_id=None, name="이규보", **ids):
    return Entity(node_id=node_id, node_type="Person", name_kor=name,
                  authority_ids={k: v for k, v in ids.items() if v})


def place(node_id=None, name="개성", **ids):
    return Entity(node_id=node_id, node_type="Place", name_kor=name,
                  authority_ids={k: v for k, v in ids.items() if v})


class RecordingFetcher:
    """Fake authority fetcher; records (source, id, node_type) calls."""

    def __init__(self, status="ok", data=None):
        self.calls = []
        self.status = status
        self.data = data or {"primary_name": "李奎報"}

    def __call__(self, source, ext_id, language, node_type="Person"):
        self.calls.append((source, ext_id, node_type))
        if self.status != "ok":
            return {"source": source, "id": ext_id, "status": self.status,
                    "node_type": node_type, "error": "unavailable"}
        return {"source": source, "id": ext_id, "status": "ok", "node_type": node_type,
                "url": f"http://example/{ext_id}", "data": dict(self.data)}


def fake_response(status=200, ctype="application/json", payload=None, raw=None, size=None):
    """Build a mock `requests` response for HTTP-guard tests."""
    r = mock.Mock()
    r.status_code = status
    r.headers = {"content-type": ctype}
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    r.content = b"x" * size if size else body
    if raw is not None:
        r.json.side_effect = ValueError("invalid json")
    else:
        r.json.return_value = payload or {}
    return r


# ── 1. JSONL schema ───────────────────────────────────────────────────────────
class TestJsonlSchema(unittest.TestCase):
    """Test plan 1: parse the JSONL schema — top-level ID, label, properties."""

    def setUp(self):
        if not os.path.exists(JSONL_PATH):
            self.skipTest("neo4j_import_nodes.jsonl not present")

    def test_top_level_schema(self):
        with open(JSONL_PATH, encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        self.assertIn("ID", first)
        self.assertIn("label", first)
        self.assertIn("properties", first)
        self.assertNotIn("labels", first)          # not a labels[] array
        self.assertIsInstance(first["properties"], dict)

    def test_registry_covers_every_stored_authority_property(self):
        """Every id* property present on Person/Place nodes must be declared in
        the registry with a capability (acceptance criterion 1)."""
        found = set()
        with open(JSONL_PATH, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                o = json.loads(line)
                if o.get("label") not in ("Person", "Place"):
                    continue
                for k, v in (o.get("properties") or {}).items():
                    if k.startswith("id") and v not in (None, "", []):
                        found.add(k)
        declared = {c.neo4j_property for c in ea.AUTHORITY_REGISTRY.values()}
        self.assertTrue(found)
        self.assertEqual(found - declared, set(), f"undeclared properties: {found - declared}")


# ── 2/3. ID extraction from retrieval ─────────────────────────────────────────
class TestIdExtraction(unittest.TestCase):
    """Test plan 2 & 3: extract all Person IDs, and Place IDs without treating a
    Place as a Person."""

    def test_all_person_ids_from_vector_metadata(self):
        doc = {
            "page_content": "matched",
            "metadata": {
                "entry_id": "E003", "entry_position": 3,
                "source_work_kor": "지봉유설", "source_work_id": "B016",
                "original_chinese": "兩兩佳人弄夕暉。", "korean_translation": "쌍쌍의 가인",
                "creator": "이규보", "creator_id": "P001",
                "creator_external_ids": {
                    "wikidata": "Q2913717", "aks_digerati": "koreanPerson_18816",
                    "loc": "n82037407", "open_library": "OL1304292A",
                    "cbdb": "0103442", "yale_lux": "person/uuid",
                    "aks_ency": "E0043772", "nlk": "KAC200105537",
                    "bnf": None,  # null props must be dropped
                },
                "mentioned_persons": [{"id": "P002", "nameKor": "이제현", "cbdb": "0103442"}],
                "audiences": [{"id": "P050", "nameKor": "권필", "loc": "n83051597"}],
            },
        }
        ev = docs_to_evidence([doc])
        d = ev.documents[0]
        self.assertEqual(d["textChi"], "兩兩佳人弄夕暉。")   # verbatim

        creator = next(e for e in ev.entities if e.node_id == "P001")
        self.assertEqual(creator.authority_ids["wikidata"], "Q2913717")
        self.assertEqual(creator.authority_ids["loc"], "n82037407")
        self.assertEqual(creator.authority_ids["cbdb"], "0103442")
        self.assertEqual(creator.authority_ids["yale_lux"], "person/uuid")
        self.assertNotIn("bnf", creator.authority_ids)      # null dropped
        # No ID is lost in Document.metadata -> Evidence
        self.assertEqual(len(creator.authority_ids), 8)

        mentioned = next(e for e in ev.entities if e.node_id == "P002")
        self.assertEqual(mentioned.authority_ids["cbdb"], "0103442")
        audience = next(e for e in ev.entities if e.node_id == "P050")
        self.assertEqual(audience.authority_ids["loc"], "n83051597")

    def test_place_ids_extracted_and_not_person(self):
        doc = {
            "page_content": "m",
            "metadata": {
                "entry_id": "E010",
                "places": [{
                    "id": "L001", "nameKor": "자화사", "nameChi": "慈化寺",
                    "aks_digerati": "koreanPlace_7249", "aks_map": "DYD_11_02_0073",
                    "aks_ency": "E0065119",
                    "gis": "37° 56' 17.50\" N", "image": "http://img",
                }],
            },
        }
        ev = docs_to_evidence([doc])
        pl = next(e for e in ev.entities if e.node_id == "L001")
        self.assertEqual(pl.node_type, "Place")
        # idAKSdigerati on a Place is re-keyed into the Place namespace
        self.assertEqual(pl.authority_ids["aks_digerati_place"], "koreanPlace_7249")
        self.assertNotIn("aks_digerati", pl.authority_ids)
        self.assertEqual(pl.authority_ids["aks_map"], "DYD_11_02_0073")
        self.assertEqual(pl.authority_ids["aks_ency"], "E0065119")
        self.assertNotIn("gis", pl.authority_ids)      # display data, not authority
        self.assertNotIn("image", pl.authority_ids)
        # A Place must never surface as a Person
        self.assertEqual(collect_person_entities(ev), [])
        self.assertEqual(len(collect_entities(ev, node_types=("Place",))), 1)

    def test_graph_row_person_and_place_do_not_contaminate(self):
        row = {
            "person_id": "P001", "person_name_kor": "이규보", "wikidata_id": "Q2913717",
            "place_id": "L001", "place_name_kor": "자화사", "aks_map_id": "DYD_11_02_0073",
        }
        ents = entities_from_graph_row(row)
        p = next(e for e in ents if e.node_type == "Person")
        pl = next(e for e in ents if e.node_type == "Place")
        self.assertEqual(p.authority_ids.get("wikidata"), "Q2913717")
        self.assertNotIn("aks_map", p.authority_ids)      # Place-only key
        self.assertEqual(pl.authority_ids.get("aks_map"), "DYD_11_02_0073")
        self.assertNotIn("wikidata", pl.authority_ids)    # Person-only key

    def test_role_prefixed_multihop_row(self):
        row = {
            "critic_person_id": "P001", "critic_wikidata_id": "Q2913717",
            "subject_person_id": "P002", "subject_cbdb_id": "0103442",
        }
        ents = entities_from_graph_row(row)
        ids = {e.node_id: e.authority_ids for e in ents}
        self.assertEqual(ids["P001"]["wikidata"], "Q2913717")
        self.assertEqual(ids["P002"]["cbdb"], "0103442")

    def test_bare_name_row_is_not_enrichable(self):
        # Test plan: no name-based guessing — a row with only a name yields no entity
        self.assertEqual(entities_from_graph_row({"person_name_kor": "황진이"}), [])


# ── 4/5/6. Registry behavior ──────────────────────────────────────────────────
class TestRegistry(unittest.TestCase):
    """Test plan 4, 5, 6: legacy behavior intact, Person/Place separation,
    invalid IDs make no HTTP request."""

    def setUp(self):
        ea.clear_authority_cache()

    def test_existing_wikidata_and_aks_person_still_work(self):
        def fake(url):
            if "wikidata" in url:
                return {"entities": {"Q2913717": {"labels": {"ko": {"value": "이규보"}}}}}
            return [{"AkspId": 18816, "PersonId": "EXM_KS_x", "KoName": "이규보",
                     "ChName": "李奎報", "YearBirth": 1168,
                     "Link": "https://people.aks.ac.kr/x"}]

        w = ea.fetch_authority("wikidata", "Q2913717", fetcher=fake)
        a = ea.fetch_authority("aks_digerati", "koreanPerson_18816", fetcher=fake)
        self.assertEqual(w["status"], "ok")
        self.assertEqual(w["data"]["primary_name"], "이규보")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(a["data"]["name_chi"], "李奎報")
        self.assertEqual(a["url"], "https://people.aks.ac.kr/x")   # canonical link

    def test_person_and_place_use_distinct_handlers(self):
        urls = []

        def fake(url):
            urls.append(url)
            if ":88" in url:
                return [{"AksloId": 7249, "KoName": "개성", "ChName": "開城",
                         "LocationId": "DYD_13_04_0008", "Link": "http://kostma/x"}]
            return [{"AkspId": 18816, "PersonId": "EXM_x", "KoName": "이규보",
                     "Link": "http://people/x"}]

        p = ea.fetch_authority("aks_digerati", "koreanPerson_18816",
                               node_type="Person", fetcher=fake)
        # Legacy source key + Place node_type resolves to the Place config.
        pl = ea.fetch_authority("aks_digerati", "koreanPlace_7249",
                                node_type="Place", fetcher=fake)
        self.assertEqual(len(urls), 2)
        self.assertIn("digerati.aks.ac.kr:85/api/IdValues/18816", urls[0])
        self.assertNotIn(":88", urls[0])           # wrong port absent
        self.assertIn("digerati.aks.ac.kr:88/api/IdValues/7249", urls[1])
        self.assertNotIn(":85", urls[1])           # wrong port absent
        self.assertEqual(p["data"]["name_kor"], "이규보")
        self.assertEqual(pl["data"]["location_id"], "DYD_13_04_0008")
        self.assertEqual(pl["key"], "aks_digerati_place")

    def test_person_id_cannot_reach_place_endpoint(self):
        calls = []
        result = ea.fetch_authority(
            "aks_digerati_place", "koreanPerson_18816", node_type="Place",
            fetcher=lambda url: calls.append(url))
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, [])

    def test_place_id_cannot_reach_person_endpoint(self):
        """The critical safety case: :85 returns HTTP 200 with a WRONG person for
        a Place number, so the Place id must be rejected before any request."""
        calls = []
        r = ea.fetch_authority("aks_digerati", "koreanPlace_7249", node_type="Person",
                               fetcher=lambda u: calls.append(u))
        self.assertEqual(r["status"], "error")
        self.assertEqual(calls, [])          # no HTTP request at all
        r2 = ea.fetch_authority("aks_digerati", "koreanPerson_18816", node_type="Place",
                                fetcher=lambda u: calls.append(u))
        self.assertEqual(r2["status"], "error")
        self.assertEqual(calls, [])

    def test_invalid_ids_make_no_request(self):
        calls = []
        sink = lambda u: calls.append(u)  # noqa: E731
        for source, bad in [
            ("wikidata", "NOT-A-QID"), ("loc", "!!!"), ("open_library", "OL???"),
            ("cbdb", "abc"), ("yale_lux", "person/xyz"), ("aks_digerati", "koreanPerson_"),
        ]:
            r = ea.fetch_authority(source, bad, fetcher=sink)
            self.assertEqual(r["status"], "error", f"{source} accepted {bad!r}")
        self.assertEqual(calls, [])

    def test_link_only_emits_reference_not_facts(self):
        calls = []
        r = ea.fetch_authority("aks_ency", "E0043772", fetcher=lambda u: calls.append(u))
        self.assertEqual(r["status"], "link_only")
        self.assertNotIn("data", r)                       # no factual claim
        self.assertEqual(r["url"], "https://encykorea.aks.ac.kr/Article/E0043772")
        self.assertEqual(calls, [])                        # never fetched
        self.assertFalse(r["fetchable"])

    def test_link_only_rejects_bad_id_and_missing_id(self):
        self.assertIsNone(ea.link_only_reference("aks_ency", ""))
        self.assertIsNone(ea.link_only_reference("aks_ency", "not-an-id"))
        self.assertIsNone(ea.link_only_reference("wikidata", "Q1"))  # not link-only

    def test_unsupported_sources_are_structural_and_non_fatal(self):
        for source, sid in [("nlk", "KAC200105537"), ("aks_kdp", "EXM_MN_x"),
                            ("aks_sillok", "송인(宋寅)"), ("british_museum", "14547"),
                            ("ency_china", "213586"), ("academia_sinica", "018284"),
                            ("aks_map", "DYD_11_02_0073")]:
            r = ea.fetch_authority(source, sid, fetcher=lambda u: 1 / 0)
            self.assertEqual(r["status"], "unsupported", source)
            self.assertNotIn("data", r)
            self.assertTrue(r.get("note"))
            self.assertIsNone(ea.link_only_reference(source, sid))  # no link either

    def test_cache_hits_and_failures_not_cached(self):
        seen = []

        def ok(url):
            seen.append(url)
            return {"entities": {"Q1": {"labels": {"en": {"value": "X"}}}}}

        ea.fetch_authority("wikidata", "Q1", fetcher=ok)
        ea.fetch_authority("wikidata", "Q1", fetcher=ok)
        self.assertEqual(len(seen), 1)                     # cached

        fails = []
        ea.fetch_authority("wikidata", "Q2", fetcher=lambda u: fails.append(u))
        ea.fetch_authority("wikidata", "Q2", fetcher=lambda u: fails.append(u))
        self.assertEqual(len(fails), 2)                    # failure not cached

    def test_cache_key_separates_node_types(self):
        seen = []

        def fake(url):
            seen.append(url)
            if ":88" in url:
                return [{"AksloId": 7249, "KoName": "개성", "Link": "http://pl"}]
            return [{"AkspId": 7249, "PersonId": "EXM_x", "KoName": "신응시",
                     "Link": "http://pe"}]

        p1 = ea.fetch_authority("aks_digerati", "koreanPerson_7249",
                                node_type="Person", fetcher=fake)
        pl1 = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                                 node_type="Place", fetcher=fake)
        # Same numeric suffix, different namespaces -> two requests, two entries
        self.assertEqual(len(seen), 2)
        self.assertIn(":85", seen[0])
        self.assertIn(":88", seen[1])
        # Cached separately: repeat calls fetch nothing new and never cross over
        p2 = ea.fetch_authority("aks_digerati", "koreanPerson_7249",
                                node_type="Person", fetcher=fake)
        pl2 = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                                 node_type="Place", fetcher=fake)
        self.assertEqual(len(seen), 2)
        self.assertEqual(p2["data"]["name_kor"], "신응시")
        self.assertEqual(pl2["data"]["name_kor"], "개성")
        self.assertNotEqual(p1["data"], pl1["data"])


# ── AKS response-side validation (type-safety work order §3) ──────────────────
class TestAksResponseValidation(unittest.TestCase):
    """A wrong-schema or wrong-record HTTP 200 must be rejected and must never
    surface parsed data. Payload shapes mirror the live API (verified:
    :85/7249 → AkspId=7249 '신응시'; :88/18816 → AksloId=18816 '대홍산')."""

    PERSON_OK = [{"AkspId": 18816, "PersonId": "EXM_KS_x", "KoName": "이규보",
                  "Link": "http://people/x"}]
    PLACE_OK = [{"AksloId": 7249, "LocationId": "DYD_13_04_0008", "KoName": "개성",
                 "Link": "http://kostma/x"}]

    def setUp(self):
        ea.clear_authority_cache()

    def test_place_schema_rejected_for_person_request(self):
        r = ea.fetch_authority("aks_digerati", "koreanPerson_18816",
                               node_type="Person", fetcher=lambda u: self.PLACE_OK)
        self.assertEqual(r["status"], "error")
        self.assertNotIn("data", r)

    def test_person_schema_rejected_for_place_request(self):
        r = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                               node_type="Place", fetcher=lambda u: self.PERSON_OK)
        self.assertEqual(r["status"], "error")
        self.assertNotIn("data", r)

    def test_mismatched_returned_id_rejected(self):
        wrong = [{"AkspId": 99999, "PersonId": "EXM_other", "KoName": "다른사람",
                  "Link": "http://people/other"}]
        r = ea.fetch_authority("aks_digerati", "koreanPerson_18816",
                               node_type="Person", fetcher=lambda u: wrong)
        self.assertEqual(r["status"], "error")
        self.assertNotIn("data", r)

        wrong_pl = [{"AksloId": 12345, "LocationId": "DYD_other", "KoName": "딴곳"}]
        r2 = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                                node_type="Place", fetcher=lambda u: wrong_pl)
        self.assertEqual(r2["status"], "error")
        self.assertNotIn("data", r2)

    def test_matching_schema_and_id_accepted(self):
        p = ea.fetch_authority("aks_digerati", "koreanPerson_18816",
                               node_type="Person", fetcher=lambda u: self.PERSON_OK)
        pl = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                                node_type="Place", fetcher=lambda u: self.PLACE_OK)
        self.assertEqual(p["status"], "ok")
        self.assertEqual(pl["status"], "ok")
        self.assertEqual(pl["data"]["name_kor"], "개성")

    def test_rejected_response_not_cached(self):
        calls = []

        def bad_then_good(url):
            calls.append(url)
            return self.PLACE_OK if len(calls) > 1 else self.PLACE_OK[:0] or [
                {"AkspId": 1, "KoName": "x"}]

        r1 = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                                node_type="Place", fetcher=bad_then_good)
        self.assertEqual(r1["status"], "error")
        r2 = ea.fetch_authority("aks_digerati_place", "koreanPlace_7249",
                                node_type="Place", fetcher=bad_then_good)
        self.assertEqual(r2["status"], "ok")       # rejection was not cached
        self.assertEqual(len(calls), 2)

    def test_rejected_result_never_becomes_evidence(self):
        """End-to-end: a rejected fetch yields no data in external claims, and
        the citation list omits it (invariants 6 & 7)."""
        from tools.orchestrator import gather_graphrag_evidence

        g = Evidence(kind="graph", entities=[
            place("L001", aks_digerati_place="koreanPlace_7249")])
        r = gather_graphrag_evidence(
            "개성은 어디에 있는 장소인가요?", "ko",
            graph_retriever=lambda q, l: g,
            vector_retriever=lambda q, l: Evidence(kind="vector"),
            authority_fetcher=lambda s, i, lang, nt="Place": ea.fetch_authority(
                s, i, node_type=nt, fetcher=lambda u: self.PERSON_OK))
        claim = next(c for c in r["external"].claims
                     if c["source"] in ("aks_digerati_place", "aks_digerati"))
        self.assertEqual(claim["status"], "error")
        self.assertNotIn("data", claim)
        out = synthesis.format_evidence_for_prompt(r, "ko")
        self.assertNotIn("이규보", out)            # wrong person never surfaces
        cites = "\n".join(synthesis.build_citations(r))
        self.assertNotIn("Digerati", cites)        # rejected -> not cited


# ── 7. Parsers surface only allowed fields ────────────────────────────────────
class TestParsers(unittest.TestCase):
    """Test plan 7: every fetchable source parses a representative mocked payload
    into only allowed fields (payload shapes captured from the live APIs)."""

    def setUp(self):
        ea.clear_authority_cache()

    def _fetch(self, source, sid, payload, node_type="Person"):
        return ea.fetch_authority(source, sid, node_type=node_type,
                                  fetcher=lambda u: payload)

    def test_loc_parser(self):
        payload = [
            {"@id": "http://id.loc.gov/authorities/names/n82037407",
             "http://www.loc.gov/mads/rdf/v1#authoritativeLabel": [
                 {"@value": "Yi, Kyu-bo, 1168-1241"},
                 {"@value": "李 奎報, 1168-1241", "@language": "und-hani"}]},
            {"@type": ["http://www.loc.gov/mads/rdf/v1#PersonalName",
                       "http://www.loc.gov/mads/rdf/v1#Variant"],
             "http://www.loc.gov/mads/rdf/v1#variantLabel": [{"@value": "Paegun Kŏsa"}]},
            {"@type": ["http://www.loc.gov/mads/rdf/v1#RWO"],
             "http://www.loc.gov/mads/rdf/v1#birthDate": [{"@value": "1168"}],
             "http://www.loc.gov/mads/rdf/v1#deathDate": [{"@value": "1241"}]},
        ]
        r = self._fetch("loc", "n82037407", payload)
        d = r["data"]
        self.assertEqual(d["year_birth"], "1168")
        self.assertEqual(d["year_death"], "1241")
        self.assertIn("李 奎報, 1168-1241",
                      [x["value"] for x in d["authoritative_labels"]])
        self.assertIn("Paegun Kŏsa", d["variant_labels"])

    def test_open_library_parser(self):
        payload = {"name": "Yi, Kyu-bo", "personal_name": "Yi, Kyu-bo",
                   "birth_date": "1168", "death_date": "1241",
                   "remote_ids": {"wikidata": "Q2913717", "lc_naf": "n82037407",
                                  "secret_internal": "should-not-pass"},
                   "links": [{"url": "http://x"}] * 50}
        d = self._fetch("open_library", "OL1304292A", payload)["data"]
        self.assertEqual(d["primary_name"], "Yi, Kyu-bo")
        self.assertEqual(d["year_birth"], "1168")
        self.assertEqual(d["cross_source_ids"]["wikidata"], "Q2913717")
        self.assertNotIn("secret_internal", d["cross_source_ids"])
        self.assertNotIn("links", d)              # unparsed junk never surfaces

    def test_cbdb_parser(self):
        payload = {"Package": {"PersonAuthority": {"PersonInfo": {"Person": {
            "BasicInfo": {"ChName": "李齊賢", "EngName": "Li Qixian",
                          "YearBirth": "1287", "YearDeath": "1367",
                          "Dynasty": "元", "IndexAddr": "高麗"},
            "PersonAliases": {"Alias": [{"AliasType": "字", "AliasName": "仲思"},
                                        {"AliasType": "諡號", "AliasName": "文忠"}]},
            "PersonAddresses": {"Address": {"AddrType": "籍貫", "AddrName": "高麗"}},
        }}}}}
        d = self._fetch("cbdb", "0103442", payload)["data"]
        self.assertEqual(d["name_chi"], "李齊賢")
        self.assertEqual(d["year_birth"], "1287")
        self.assertEqual(d["dynasty"], "元")
        self.assertEqual(d["aliases"][0], {"type": "字", "name": "仲思"})
        self.assertEqual(d["addresses"][0]["name"], "高麗")   # single dict tolerated

    def test_cbdb_validation_error_is_unavailable(self):
        payload = {"error": {"code": 422, "message": "Validation failed."}}
        r = self._fetch("cbdb", "123", payload)
        self.assertEqual(r["status"], "unavailable")
        self.assertNotIn("data", r)

    def test_yale_lux_parser(self):
        payload = {
            "_label": "Yi, Kyu-bo, 1168-1241",
            "born": {"timespan": {"identified_by": [{"content": "1168"}]}},
            "died": {"timespan": {"identified_by": [{"content": "1241"}]}},
            "identified_by": [{"type": "Name", "content": "Yi Kyu-bo"}],
            "subject_of": [{"huge": "x" * 10000}],
        }
        d = self._fetch("yale_lux", "person/" + "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12,
                        payload)["data"]
        self.assertEqual(d["primary_name"], "Yi, Kyu-bo, 1168-1241")
        self.assertEqual(d["year_birth"], "1168")
        self.assertEqual(d["aliases"], ["Yi Kyu-bo"])
        self.assertNotIn("subject_of", d)         # raw graph never surfaced

    def test_aks_place_parser(self):
        payload = [{"AksloId": 7249, "LocationId": "DYD_13_04_0008",
                    "Source": "한국학자료센터 동여도", "ChName": "開城", "KoName": "개성",
                    "Link": "http://kostma.aks.ac.kr/e-map/x"}]
        r = self._fetch("aks_digerati", "koreanPlace_7249", payload, node_type="Place")
        d = r["data"]
        self.assertEqual(d["name_kor"], "개성")
        self.assertEqual(d["location_id"], "DYD_13_04_0008")
        self.assertEqual(r["url"], "http://kostma.aks.ac.kr/e-map/x")   # API canonical link
        self.assertIn("MUST_NOT_ADD", d)          # place-specific guardrails present


# ── 13. HTTP guards ───────────────────────────────────────────────────────────
class TestHttpGuards(unittest.TestCase):
    """Test plan 13: timeout, 429, 404, invalid JSON, wrong content-type, and
    oversized responses all degrade gracefully."""

    def setUp(self):
        ea.clear_authority_cache()

    def _fetch_with(self, response=None, side_effect=None):
        with mock.patch.object(ea.requests, "get") as g:
            if side_effect is not None:
                g.side_effect = side_effect
            else:
                g.return_value = response
            return ea.fetch_authority("wikidata", "Q2913717")

    def test_timeout(self):
        import requests as rq
        r = self._fetch_with(side_effect=rq.Timeout("timed out"))
        self.assertEqual(r["status"], "unavailable")

    def test_429_and_404(self):
        for code in (429, 404, 500):
            r = self._fetch_with(fake_response(status=code))
            self.assertEqual(r["status"], "unavailable", f"status {code}")

    def test_invalid_json(self):
        r = self._fetch_with(fake_response(raw=b"<html>nope</html>"))
        self.assertEqual(r["status"], "unavailable")

    def test_wrong_content_type(self):
        r = self._fetch_with(fake_response(ctype="text/html", payload={"entities": {}}))
        self.assertEqual(r["status"], "unavailable")

    def test_oversized_response(self):
        r = self._fetch_with(fake_response(size=ea.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(r["status"], "unavailable")

    def test_connection_error(self):
        import requests as rq
        r = self._fetch_with(side_effect=rq.ConnectionError("dns"))
        self.assertEqual(r["status"], "unavailable")


# ── 10/11/12. Orchestrator policy ─────────────────────────────────────────────
class TestOrchestrator(unittest.TestCase):
    """Test plan 10, 11, 12: caps + de-duplication, poem-list makes no call,
    place questions can enrich Place entities."""

    def _run(self, graph_ev, vector_ev, fetcher, question="이규보에 대해 자세히 알려줘", **kw):
        return gather_graphrag_evidence(
            question, "ko",
            graph_retriever=lambda q, l: graph_ev,
            vector_retriever=lambda q, l: vector_ev,
            authority_fetcher=fetcher, **kw,
        )

    def test_poem_list_makes_no_external_call(self):
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1",
                                                    aks_digerati="koreanPerson_1")])
        f = RecordingFetcher()
        r = self._run(g, Evidence(kind="vector"), f, question="황진이는 어떤 시를 썼나요?")
        self.assertEqual(f.calls, [])
        self.assertFalse(r["authority_attempted"])

    def test_biography_question_enriches_person(self):
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1",
                                                    aks_digerati="koreanPerson_1")])
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f)
        self.assertEqual(sorted(c[0] for c in f.calls), ["aks_digerati", "wikidata"])

    def test_place_question_enriches_place(self):
        g = Evidence(kind="graph", entities=[
            place("L001", aks_digerati_place="koreanPlace_7249",
                  aks_map="DYD_11_02_0073"),
        ])
        f = RecordingFetcher()
        r = self._run(g, Evidence(kind="vector"), f, question="자화사는 어디에 위치한 장소인가요?")
        self.assertEqual(f.calls, [("aks_digerati_place", "koreanPlace_7249", "Place")])
        self.assertTrue(r["places"])
        # aks_map is unsupported -> no fetch, no link-only claim
        sources = {c["source"] for c in r["external"].claims}
        self.assertNotIn("aks_map", sources)

    def test_person_question_does_not_enrich_place(self):
        g = Evidence(kind="graph", entities=[
            person("P1", wikidata="Q1"),
            place("L001", aks_digerati_place="koreanPlace_7249"),
        ])
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f, question="이규보의 생몰년은?")
        self.assertEqual([c[2] for c in f.calls], ["Person"])

    def test_place_entity_never_uses_person_source(self):
        """Pipeline regression (§8): a Place entity must never produce
        'aks_digerati' (Person) enrichment, and vice versa — even when its
        authority map wrongly carries the other namespace's key."""
        g = Evidence(kind="graph", entities=[
            place("L001", aks_digerati="koreanPlace_7249"),   # wrong (Person) key
            person("P1", aks_digerati_place="koreanPlace_7249"),  # wrong (Place) key
        ])
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f,
                  question="이 인물과 장소에 대해 자세히, 위치는 어디인지 알려줘")
        sources = {(c[0], c[2]) for c in f.calls}
        self.assertNotIn(("aks_digerati", "Place"), sources)
        self.assertNotIn(("aks_digerati_place", "Person"), sources)

    def test_duplicate_entities_one_call_per_source_id(self):
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1")])
        v = Evidence(kind="vector", entities=[person("P1", aks_digerati="koreanPerson_1")])
        f = RecordingFetcher()
        r = self._run(g, v, f)
        self.assertEqual(len(r["persons"]), 1)                 # merged
        self.assertEqual(len(f.calls), 2)                      # one per source
        self.assertEqual(len({(c[0], c[1]) for c in f.calls}), 2)

    def test_person_and_place_caps(self):
        persons = [person(f"P{i}", wikidata=f"Q{i}") for i in range(10)]
        places = [place(f"L{i}", aks_digerati_place=f"koreanPlace_{i}") for i in range(10)]
        g = Evidence(kind="graph", entities=persons + places)
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f,
                  question="이 인물들의 생몰년과 장소는 어디인가요?")
        self.assertEqual(len([c for c in f.calls if c[2] == "Person"]), 3)   # person cap
        self.assertEqual(len([c for c in f.calls if c[2] == "Place"]), 2)    # place cap

    def test_sources_per_entity_cap_and_compare_override(self):
        e = person("P1", wikidata="Q1", aks_digerati="koreanPerson_1",
                   loc="n82037407", open_library="OL1304292A", cbdb="0103442")
        g = Evidence(kind="graph", entities=[e])
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f)
        self.assertEqual(len(f.calls), 2)          # default per-entity source cap

        f2 = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f2,
                  question="이규보의 생몰년을 여러 출처로 비교해줘")
        self.assertGreater(len(f2.calls), 2)       # explicit comparison lifts the cap

    def test_missing_ids_no_name_lookup(self):
        g = Evidence(kind="graph", entities=[person("P2", name="아무개")])
        f = RecordingFetcher()
        self._run(g, Evidence(kind="vector"), f, question="이 인물의 생애를 자세히 알려줘")
        self.assertEqual(f.calls, [])

    def test_failed_authority_keeps_evidence(self):
        g = Evidence(kind="graph", documents=[{"person_name_kor": "이규보"}],
                     entities=[person("P1", wikidata="Q1")])
        v = Evidence(kind="vector", documents=[{"textKor": "원문"}])
        f = RecordingFetcher(status="unavailable")
        r = self._run(g, v, f)
        self.assertTrue(r["graph"].documents)
        self.assertTrue(r["vector"].documents)
        claim = r["external"].claims[0]
        self.assertEqual(claim["status"], "unavailable")
        self.assertNotIn("data", claim)

    def test_link_only_recorded_without_fetch(self):
        g = Evidence(kind="graph", entities=[person("P1", wikidata="Q1",
                                                    aks_ency="E0043772")])
        f = RecordingFetcher()
        r = self._run(g, Evidence(kind="vector"), f)
        self.assertNotIn("aks_ency", [c[0] for c in f.calls])   # never fetched
        link_claims = [c for c in r["external"].claims if c["status"] == "link_only"]
        self.assertEqual(len(link_claims), 1)
        self.assertNotIn("data", link_claims[0])
        self.assertEqual(link_claims[0]["url"],
                         "https://encykorea.aks.ac.kr/Article/E0043772")

    def test_intent_routing(self):
        self.assertFalse(needs_authority("황진이는 어떤 시를 썼나요?"))
        self.assertTrue(authority_intent("이규보의 생몰년은?")["Person"])
        self.assertFalse(authority_intent("이규보의 생몰년은?")["Place"])
        self.assertTrue(authority_intent("자화사는 어디에 있나요?")["Place"])
        self.assertTrue(authority_intent("where is this temple located?")["Place"])


# ── 14/15. Synthesis ──────────────────────────────────────────────────────────
class TestSynthesis(unittest.TestCase):
    """Test plan 14 & 15: bounded, source-separated evidence; citations never use
    unverified URLs; conflicts stay source-specific."""

    def test_source_labels_and_bounded_allowlisted_payload(self):
        big = "X" * 60000
        evidence = {
            "graph": Evidence(kind="graph", documents=[{"person_name_kor": "이규보",
                                                        "blob": big}]),
            "vector": Evidence(kind="vector", documents=[{"textKor": "원문",
                                                          "entry_id": "E003"}]),
            "external": Evidence(kind="external", claims=[{
                "entity": "이규보", "source": "wikidata", "source_label": "Wikidata",
                "status": "ok", "url": "http://x",
                "data": {"primary_name": "李奎報", "raw_blob": big},
            }]),
        }
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("## Graph Evidence", out)
        self.assertIn("## Vector Evidence", out)
        self.assertIn("## External Authority Evidence", out)
        self.assertNotIn("raw_blob", out)          # not in the registry allowlist
        self.assertNotIn("X" * 5000, out)          # truncated
        self.assertLessEqual(len(out), synthesis._MAX_TOTAL_CHARS + 50)

    def test_per_source_allowlist_applies_to_each_source(self):
        evidence = {"graph": Evidence(kind="graph"), "vector": Evidence(kind="vector"),
                    "external": Evidence(kind="external", claims=[
                        {"entity": "A", "source": "cbdb", "source_label": "CBDB",
                         "status": "ok", "url": "http://c",
                         "data": {"name_chi": "李齊賢", "internal_junk": "LEAK"}},
                        {"entity": "B", "source": "loc", "source_label": "LOC",
                         "status": "ok", "url": "http://l",
                         "data": {"year_birth": "1168", "other_junk": "LEAK2"}},
                    ])}
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("李齊賢", out)
        self.assertIn("1168", out)
        self.assertNotIn("LEAK", out)
        self.assertNotIn("LEAK2", out)

    def test_link_only_marked_and_not_fetched(self):
        evidence = {"graph": Evidence(kind="graph"), "vector": Evidence(kind="vector"),
                    "external": Evidence(kind="external", claims=[{
                        "entity": "이규보", "source": "aks_ency",
                        "source_label": "AKS 한국민족문화대백과", "status": "link_only",
                        "url": "https://encykorea.aks.ac.kr/Article/E0043772",
                    }])}
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("LINK-ONLY", out)
        cites = "\n".join(synthesis.build_citations(evidence))
        self.assertIn("참고 링크", cites)

    def test_conflicting_values_kept_source_specific(self):
        evidence = {
            "graph": Evidence(kind="graph", documents=[{"person_name_kor": "이규보",
                                                        "yearBirth": 1168}]),
            "vector": Evidence(kind="vector"),
            "external": Evidence(kind="external", claims=[
                {"entity": "이규보", "source": "wikidata", "source_label": "Wikidata",
                 "status": "ok", "url": "http://w", "data": {"birth_time": "+1200-00-00T00:00:00Z"}},
                {"entity": "이규보", "source": "cbdb", "source_label": "CBDB",
                 "status": "ok", "url": "http://c", "data": {"year_birth": "1250"}},
            ]),
        }
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        for v in ("1168", "1200", "1250"):
            self.assertIn(v, out)      # every source's own value survives, unmerged
        self.assertIn("CONFLICTS", synthesis.SYNTHESIS_SYSTEM_RULES)

    def test_citations_never_fabricate(self):
        evidence = {"graph": Evidence(kind="graph"), "vector": Evidence(kind="vector"),
                    "external": Evidence(kind="external", claims=[
                        {"entity": "A", "source": "wikidata", "source_label": "Wikidata",
                         "status": "ok", "url": "http://wd/Q1"},
                        {"entity": "B", "source": "cbdb", "status": "ok"},        # no url
                        {"entity": "C", "source": "loc", "status": "unavailable"},
                    ])}
        joined = "\n".join(synthesis.build_citations(evidence))
        self.assertIn("[A](http://wd/Q1)", joined)
        self.assertNotIn("[B]", joined)      # no url -> no citation
        self.assertNotIn("[C]", joined)      # unavailable -> no citation

    def test_must_not_add_guardrails_survive(self):
        evidence = {"graph": Evidence(kind="graph"), "vector": Evidence(kind="vector"),
                    "external": Evidence(kind="external", claims=[{
                        "entity": "이규보", "source": "aks_digerati",
                        "source_label": "AKS Digerati", "status": "ok", "url": "http://a",
                        "data": {"name_kor": "이규보",
                                 "MUST_NOT_ADD": ["관직 이력 (이 API는 반환하지 않습니다)"]},
                    }])}
        out = synthesis.format_evidence_for_prompt(evidence, "ko")
        self.assertIn("MUST_NOT_ADD", out)
        self.assertIn("관직 이력", out)


# ── Entity merge ──────────────────────────────────────────────────────────────
class TestEntityMerge(unittest.TestCase):
    def test_merge_by_shared_authority_id_transitively(self):
        a = person(None, wikidata="Q1")
        b = person("P1", aks_digerati="koreanPerson_1")
        c = person("P1", wikidata="Q1")
        merged = merge_entities([a, b, c])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].node_id, "P1")
        self.assertEqual(merged[0].authority_ids["wikidata"], "Q1")
        self.assertEqual(merged[0].authority_ids["aks_digerati"], "koreanPerson_1")

    def test_person_and_place_never_merge(self):
        # Same numeric tail on both sides — node type and namespaced keys keep
        # them apart (type-safety work order §5).
        p = person("P1", aks_digerati="koreanPerson_7249")
        pl = place("L1", aks_digerati_place="koreanPlace_7249")
        self.assertEqual(len(merge_entities([p, pl])), 2)

    def test_distinct_nodes_not_merged_on_name(self):
        self.assertEqual(len(merge_entities([person("P1"), person("P2")])), 2)

    def test_legacy_accessors(self):
        e = person("P1", wikidata="Q1", aks_digerati="koreanPerson_1")
        self.assertEqual(e.wikidata_id, "Q1")
        self.assertEqual(e.aks_digerati_id, "koreanPerson_1")
        self.assertEqual(e.to_dict()["wikidata_id"], "Q1")


# ── Graph smoke + prompt safety ───────────────────────────────────────────────
class TestGraphAndPrompts(unittest.TestCase):
    def test_graphrag_question_smoke(self):
        g = graph_rows_to_evidence(
            [{"person_name_kor": "황진이", "poem_id": "M100", "work_id": "B016"}],
            cypher="MATCH ...")
        f = RecordingFetcher()
        r = gather_graphrag_evidence(
            "황진이는 어떤 시를 썼나요?", "ko",
            graph_retriever=lambda q, l: g,
            vector_retriever=lambda q, l: Evidence(kind="vector"),
            authority_fetcher=f)
        self.assertEqual(f.calls, [])
        self.assertTrue(r["graph"].provenance)

    def test_cypher_template_has_no_unintended_variables(self):
        from langchain_core.prompts import PromptTemplate

        template = _extract_module_string("tools/cypher.py", "CYPHER_GENERATION_TEMPLATE")
        self.assertIsNotNone(template)
        pt = PromptTemplate.from_template(template)
        self.assertEqual(set(pt.input_variables), {"schema", "question"})

    def test_prompts_do_not_advertise_unsupported_sources(self):
        """Phase 7: prompts must describe only what the registry supports."""
        text = _read("tools/vector.py") + _read("agent.py")
        self.assertNotIn("https://sillok.history.go.kr/", text)
        # The Digerati API endpoint must not be offered as a user-facing link.
        self.assertNotIn("https://digerati.aks.ac.kr:85/api/IdValues/{{id}}", text)


def _read(rel_path):
    with open(os.path.join(os.path.dirname(__file__), "..", rel_path),
              encoding="utf-8") as fh:
        return fh.read()


def _extract_module_string(rel_path, var_name):
    """Read a module-level string constant via AST, without importing the module
    (which would pull in streamlit/neo4j)."""
    tree = ast.parse(_read(rel_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        return node.value.value
    return None


if __name__ == "__main__":
    unittest.main()

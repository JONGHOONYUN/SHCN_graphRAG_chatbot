"""External Authority Enrichment — declarative source registry.

One registry (`AUTHORITY_REGISTRY`) declares every known authority source for
Neo4j `Person` and `Place` nodes: its Neo4j property, allowed node types, ID
validation/transform, request builder, parser, citation URL, and the allowlist
of parsed fields that may ever reach the LLM.

Capabilities are evidence-based (see `docs/external_authority_sources.md` — every
`fetchable` source was verified against a real stored ID):

    fetchable   — official JSON API verified; parsed fields may be cited as facts.
    link_only   — public URL verified, but no API; may be shown as a 참고 링크 only.
    unsupported — endpoint/URL unverified, bot-blocked, or the stored value is not
                  a usable identifier. No fetch, no link.

⚠ Person/Place safety: `GET :85/api/IdValues/7249` (a Place number on the Person
port) returns HTTP 200 with a *Person* record. Person and Place therefore have
separate registry entries, separate ID validators, and the node type is carried
into every request and cache key so the two can never be confused.

No source enabled here requires an API key. Any future key must come from
Streamlit secrets/env — never hard-coded, never logged.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests


# ──────────────────────────────────────────────
# HTTP policy
# ──────────────────────────────────────────────
DEFAULT_TIMEOUT_SEC = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # reject oversized payloads unparsed
USER_AGENT = "SihwaGraphRAG/0.2 (academic research chatbot; https://poetrytalks.org)"
_JSON_CONTENT_TYPES = ("application/json", "application/ld+json", "text/json")

CACHE_KEY = "external_authority_cache"  # legacy key (kept for compatibility)

CAPABILITY_FETCHABLE = "fetchable"
CAPABILITY_LINK_ONLY = "link_only"
CAPABILITY_UNSUPPORTED = "unsupported"


# ──────────────────────────────────────────────
# ID validators / transforms
#
# All patterns are matched with fullmatch() (see AuthoritySourceConfig.validate_id)
# so a trailing newline or any suffix can never sneak through — '$' alone would
# still match 'koreanPerson_123\n'.
#
# ⚠ The AKS prefixes REQUIRE the underscore form. `koreanPerson_<n>` and
# `koreanPlace_<n>` are DIFFERENT authority namespaces that share the Neo4j
# property name `idAKSdigerati`; validating the full prefix (not just the numeric
# tail) is what keeps them apart.
# ──────────────────────────────────────────────
_RE_WIKIDATA = re.compile(r"Q\d+")
_RE_AKS_PERSON = re.compile(r"(?:koreanPerson_)?(\d+)")
_RE_AKS_PLACE = re.compile(r"(?:koreanPlace_)?(\d+)")
_RE_LOC = re.compile(r"[a-z]{1,3}\d{6,12}")
_RE_OPENLIBRARY = re.compile(r"OL\d+A")
_RE_CBDB = re.compile(r"\d{1,9}")
_RE_YALE_LUX = re.compile(r"person/[0-9a-fA-F-]{36}")
_RE_AKS_ENCY = re.compile(r"E\d{7}")
_RE_BNF = re.compile(r"[0-9a-z]{6,12}")
_RE_BRITANNICA = re.compile(r"[A-Za-z0-9_\-/]{3,120}")
_RE_WORLD_HISTORY = re.compile(r"[A-Za-z0-9_\-]{2,80}")


def _digits_from(pattern: re.Pattern) -> Callable[[str], Optional[str]]:
    """Build a transform that extracts the numeric part for the API request.

    The transform is bound to a node-type-specific pattern and uses fullmatch, so
    a `koreanPlace_7249` value can never produce a Person request id. The FULL
    original string is validated before its numeric tail is extracted."""

    def _transform(raw_id: str) -> Optional[str]:
        m = pattern.fullmatch((raw_id or "").strip())
        return m.group(1) if m else None

    return _transform


def _identity(raw_id: str) -> Optional[str]:
    return raw_id or None


# Backward-compatible export (legacy name used by earlier code/tests).
def _transform_aks_digerati_id(raw_id: str) -> Optional[str]:
    """'koreanPerson_18816' → '18816'. Person-only; rejects koreanPlace_*."""
    return _digits_from(_RE_AKS_PERSON)(raw_id)


ID_TRANSFORMS = {"aks_digerati_id": _transform_aks_digerati_id}


# ──────────────────────────────────────────────
# Wikidata language preference
# ──────────────────────────────────────────────
_WIKIDATA_LANG_PRIORITY = {
    "ko": ["ko", "en", "zh", "ja"],
    "en": ["en", "ko", "zh"],
    "zh": ["zh", "en", "ko", "ja"],
}


def _effective_language(explicit: Optional[str] = None) -> str:
    """Resolve response language without hard-depending on streamlit."""
    if explicit:
        return explicit
    try:
        import streamlit as st

        lang = st.session_state.get("effective_language")
        if lang:
            return lang
    except Exception:
        pass
    return "ko"


# ──────────────────────────────────────────────
# TTL + bounded cache. Key = source|node_type|normalized_id.
# Successes cached; failures never cached (retryable next turn).
# ──────────────────────────────────────────────
CACHE_MAX_SIZE = 256
_authority_cache: "dict[str, tuple[float, Any]]" = {}


def _cache_get(key: str):
    entry = _authority_cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() >= expires_at:
        _authority_cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_sec: int) -> None:
    if len(_authority_cache) >= CACHE_MAX_SIZE:
        for old in sorted(_authority_cache, key=lambda k: _authority_cache[k][0])[:16]:
            _authority_cache.pop(old, None)
    _authority_cache[key] = (time.time() + ttl_sec, value)


def clear_authority_cache() -> None:
    """Test/maintenance helper — empties the in-process authority cache."""
    _authority_cache.clear()


# ──────────────────────────────────────────────
# HTTP fetch with content-type / size / status guards
# ──────────────────────────────────────────────
def _fetch(url: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> Optional[dict]:
    """GET + JSON parse. Returns None on ANY failure (timeout, non-200, wrong
    content-type, oversized body, invalid JSON). Never raises, never logs the
    response body."""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        if resp.status_code != 200:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if not any(t in ctype for t in _JSON_CONTENT_TYPES):
            return None
        if len(resp.content) > MAX_RESPONSE_BYTES:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


# ──────────────────────────────────────────────
# Parsers — each returns ONLY explicitly extracted fields (never raw payloads)
# ──────────────────────────────────────────────
def parse_wikidata(data: dict, entity_id: str, user_language: str = "ko") -> dict:
    """Wikidata Special:EntityData JSON → person summary."""
    entity = (data.get("entities") or {}).get(entity_id, {})
    labels = entity.get("labels", {})
    descs = entity.get("descriptions", {})
    aliases = entity.get("aliases", {})
    claims = entity.get("claims", {})
    priority = _WIKIDATA_LANG_PRIORITY.get(user_language, ["en", "ko", "zh"])

    def _pick(field_dict: dict) -> Optional[str]:
        for lang in priority:
            if lang in field_dict:
                return field_dict[lang].get("value")
        for _lang, blob in field_dict.items():
            if isinstance(blob, dict):
                return blob.get("value")
        return None

    def _time_claim(prop: str) -> Optional[str]:
        try:
            return claims[prop][0]["mainsnak"]["datavalue"]["value"]["time"]
        except (KeyError, IndexError, TypeError):
            return None

    result = {
        "source": "wikidata",
        "url": f"https://www.wikidata.org/wiki/{entity_id}",
        "primary_name": _pick(labels),
        "primary_description": _pick(descs),
        "names_by_lang": {
            lang: labels[lang]["value"]
            for lang in ("ko", "en", "zh", "ja")
            if lang in labels and isinstance(labels[lang], dict)
        },
        "descriptions_by_lang": {
            lang: descs[lang]["value"]
            for lang in ("ko", "en", "zh")
            if lang in descs and isinstance(descs[lang], dict)
        },
        "aliases": [
            a["value"]
            for lang in ("en", "ko")
            for a in aliases.get(lang, [])
            if isinstance(a, dict) and "value" in a
        ][:10],
        "birth_time": _time_claim("P569"),
        "death_time": _time_claim("P570"),
    }
    return {k: v for k, v in result.items() if v not in (None, [], {})}


def parse_aks_digerati(data, entity_id: str, user_language: str = "ko") -> dict:
    """AKS Digerati **Person** (port 85). Verified fields only.

    The API does NOT return career/office history, family relations, work lists,
    or narrative biography — MUST_NOT_ADD encodes that for the LLM."""
    base = {
        "source": "aks_digerati",
        "node_type": "Person",
        "api_url": f"https://digerati.aks.ac.kr:85/api/IdValues/{entity_id}",
        "schema_hint": (
            "AKS Digerati Person API에 실제로 존재하는 필드만 사용하세요: "
            "KoName, ChName, YearBirth, YearDeath, Gender, Link, "
            "aks_PersonAliases(字/號/諡號 등), aks_Address(籍貫 등), "
            "aks_Entry(급제/입사 이력)."
        ),
        "MUST_NOT_ADD": [
            "관직 이력 (이 API는 관직명을 반환하지 않습니다)",
            "가족 관계 (아버지·어머니·아들·형제 등 이 API는 반환하지 않습니다)",
            "관련 인물 (스승·동료·후원자 등 이 API는 반환하지 않습니다)",
            "저작 목록 (어떤 저작명도 이 API는 반환하지 않습니다)",
            "문학적 특징·평가 (이 API는 반환하지 않습니다)",
            "출생지 지명 확대 (aks_Address 원문 그대로만. 예: '驪州'를 '황해도 해주'로 확대 금지)",
            "본관 창작 (aks_Address의 '籍貫' 값 그대로만 표기)",
        ],
        "answer_template_when_missing": (
            "AKS Digerati 데이터베이스에는 이 인물의 [X] 정보가 포함되어 있지 않습니다."
        ),
    }
    if not isinstance(data, list) or not data:
        base["error"] = "empty or unexpected response shape"
        return base
    entry = data[0]
    if not isinstance(entry, dict):
        base["error"] = "first item not a dict"
        return base

    base["canonical_link"] = entry.get("Link")
    base["name_kor"] = entry.get("KoName")
    base["name_chi"] = entry.get("ChName")
    if entry.get("YearBirth"):
        base["year_birth"] = entry["YearBirth"]
    if entry.get("YearDeath"):
        base["year_death"] = entry["YearDeath"]
    if entry.get("Gender") is not None:
        base["gender_code"] = entry["Gender"]
    base["source_label"] = entry.get("Source")
    base["aks_person_id"] = entry.get("PersonId")

    aliases = entry.get("aks_PersonAliases") or []
    if aliases:
        base["aliases"] = [
            {"type": a.get("AliasType"), "name": a.get("AliasName")}
            for a in aliases
            if isinstance(a, dict) and a.get("AliasName")
        ]
    addresses = entry.get("aks_Address") or []
    if addresses:
        base["addresses"] = [
            {"type": a.get("AddrType"), "name": a.get("AddrName")}
            for a in addresses
            if isinstance(a, dict) and a.get("AddrName")
        ]
    entries = entry.get("aks_Entry") or []
    if entries:
        base["examination_entries"] = [
            {k: v for k, v in e.items() if v} for e in entries if isinstance(e, dict)
        ]
    if len(data) > 1:
        base["additional_matches_count"] = len(data) - 1
    return {k: v for k, v in base.items() if v not in (None, [], {})}


def parse_aks_digerati_place(data, entity_id: str, user_language: str = "ko") -> dict:
    """AKS Digerati **Place** (port 88). Verified schema — DIFFERENT from Person:
    AksloId, LocationId, Source, ChName, KoName, Link. No coordinates, no
    history, no description."""
    base = {
        "source": "aks_digerati_place",
        "node_type": "Place",
        "api_url": f"https://digerati.aks.ac.kr:88/api/IdValues/{entity_id}",
        "schema_hint": (
            "AKS Digerati Place API가 반환하는 필드는 이것이 전부입니다: "
            "KoName, ChName, LocationId(동여도 지명 ID), Source, Link."
        ),
        "MUST_NOT_ADD": [
            "좌표·위경도 (이 API는 좌표를 반환하지 않습니다)",
            "지역 연혁·역사 서술 (이 API는 반환하지 않습니다)",
            "행정구역 확대 해석 (반환된 지명 원문 그대로만 사용)",
            "인물·사건 연관 정보 (이 API는 반환하지 않습니다)",
        ],
    }
    if not isinstance(data, list) or not data:
        base["error"] = "empty or unexpected response shape"
        return base
    entry = data[0]
    if not isinstance(entry, dict):
        base["error"] = "first item not a dict"
        return base

    base["canonical_link"] = entry.get("Link")
    base["name_kor"] = entry.get("KoName")
    base["name_chi"] = entry.get("ChName")
    base["source_label"] = entry.get("Source")
    base["location_id"] = entry.get("LocationId")
    if len(data) > 1:
        base["additional_matches_count"] = len(data) - 1
    return {k: v for k, v in base.items() if v not in (None, [], {})}


def _loc_values(node: dict, suffix: str) -> list:
    """Collect @value strings for a MADS/RDF predicate ending with `suffix`."""
    out = []
    for key, val in node.items():
        if not key.endswith(suffix):
            continue
        items = val if isinstance(val, list) else [val]
        for item in items:
            if isinstance(item, dict) and item.get("@value"):
                lang = item.get("@language")
                out.append({"value": item["@value"], "language": lang} if lang
                           else {"value": item["@value"]})
    return out


def parse_loc(data, entity_id: str, user_language: str = "ko") -> dict:
    """LOC id.loc.gov MADS/RDF JSON → authoritative + variant labels, dates.

    Verified with n82037407: authoritativeLabel 'Yi, Kyu-bo, 1168-1241' plus
    ko-hang / und-hani forms, variantLabels, birthDate 1168, deathDate 1241."""
    base = {
        "source": "loc",
        "node_type": "Person",
        "url": f"https://id.loc.gov/authorities/names/{entity_id}",
        "schema_hint": (
            "LOC Name Authority가 반환하는 것은 이름 표목(authoritative/variant labels)과 "
            "생몰년(birthDate/deathDate)뿐입니다. 전기 서술은 없습니다."
        ),
        "MUST_NOT_ADD": [
            "전기 서술·관직·가족 (LOC 표목 레코드에는 없습니다)",
        ],
    }
    if not isinstance(data, list):
        base["error"] = "unexpected response shape"
        return base

    target = f"/authorities/names/{entity_id}"
    authoritative, variants, birth, death = [], [], None, None
    for node in data:
        if not isinstance(node, dict):
            continue
        nid = node.get("@id") or ""
        if nid.endswith(target):
            authoritative.extend(_loc_values(node, "#authoritativeLabel"))
        types = node.get("@type") or []
        types = types if isinstance(types, list) else [types]
        if any("Variant" in str(t) for t in types):
            variants.extend(_loc_values(node, "#variantLabel"))
        if any(str(t).endswith("#RWO") for t in types):
            b = _loc_values(node, "#birthDate")
            d = _loc_values(node, "#deathDate")
            if b:
                birth = b[0]["value"]
            if d:
                death = d[0]["value"]

    if authoritative:
        base["authoritative_labels"] = authoritative[:6]
    if variants:
        base["variant_labels"] = [v["value"] for v in variants][:10]
    if birth:
        base["year_birth"] = birth
    if death:
        base["year_death"] = death
    return {k: v for k, v in base.items() if v not in (None, [], {})}


def parse_open_library(data, entity_id: str, user_language: str = "ko") -> dict:
    """OpenLibrary /authors/{id}.json → name, dates, cross-source remote_ids.

    Verified with OL1304292A: name 'Yi, Kyu-bo', 1168–1241, remote_ids carrying
    wikidata/lc_naf/viaf/isni (useful for cross-source confirmation)."""
    base = {
        "source": "open_library",
        "node_type": "Person",
        "url": f"https://openlibrary.org/authors/{entity_id}",
        "schema_hint": (
            "OpenLibrary Author 레코드가 반환하는 것은 이름·생몰년·타 authority ID뿐입니다."
        ),
        "MUST_NOT_ADD": ["전기 서술·평가 (OpenLibrary author 레코드에는 없습니다)"],
    }
    if not isinstance(data, dict):
        base["error"] = "unexpected response shape"
        return base
    base["primary_name"] = data.get("name")
    base["personal_name"] = data.get("personal_name")
    if data.get("birth_date"):
        base["year_birth"] = data["birth_date"]
    if data.get("death_date"):
        base["year_death"] = data["death_date"]
    if data.get("alternate_names"):
        base["aliases"] = [n for n in data["alternate_names"] if isinstance(n, str)][:10]
    remote = data.get("remote_ids")
    if isinstance(remote, dict):
        base["cross_source_ids"] = {
            k: v for k, v in remote.items()
            if k in ("wikidata", "viaf", "isni", "lc_naf") and isinstance(v, str)
        }
    return {k: v for k, v in base.items() if v not in (None, [], {})}


def _cbdb_list(blob, key: str) -> list:
    """CBDB nests XML-ish structures; a single item may be a dict, many a list."""
    if not isinstance(blob, dict):
        return []
    inner = blob.get(key)
    if inner is None:
        return []
    return inner if isinstance(inner, list) else [inner]


def parse_cbdb(data, entity_id: str, user_language: str = "ko") -> dict:
    """CBDB person API (o=json) → BasicInfo + aliases + addresses.

    Verified with 0103442: 李齊賢 / Li Qixian, 1287–1367, Dynasty 元,
    aliases 字 仲思 / 諡號 文忠 / 別號 益齋."""
    base = {
        "source": "cbdb",
        "node_type": "Person",
        "url": f"https://cbdb.fas.harvard.edu/cbdbapi/person.php?id={entity_id}",
        "schema_hint": (
            "CBDB가 반환하는 필드: ChName/EngName, YearBirth/YearDeath, Dynasty, "
            "IndexAddr(籍貫), PersonAliases(字/號/諡號), PersonAddresses."
        ),
        "MUST_NOT_ADD": ["반환되지 않은 관직·친족·저작 정보 (파싱된 필드 외 추가 금지)"],
    }
    if not isinstance(data, dict):
        base["error"] = "unexpected response shape"
        return base
    if isinstance(data.get("error"), dict):
        base["error"] = "CBDB validation error"
        return base

    person = (
        data.get("Package", {})
        .get("PersonAuthority", {})
        .get("PersonInfo", {})
        .get("Person", {})
    )
    if not isinstance(person, dict) or not person:
        base["error"] = "person not found in response"
        return base

    info = person.get("BasicInfo") or {}
    if isinstance(info, dict):
        base["name_chi"] = info.get("ChName")
        base["primary_name"] = info.get("EngName")
        if info.get("YearBirth"):
            base["year_birth"] = info["YearBirth"]
        if info.get("YearDeath"):
            base["year_death"] = info["YearDeath"]
        if info.get("Dynasty"):
            base["dynasty"] = info["Dynasty"]
        if info.get("IndexAddr"):
            base["index_address"] = info["IndexAddr"]

    aliases = _cbdb_list(person.get("PersonAliases"), "Alias")
    if aliases:
        base["aliases"] = [
            {"type": a.get("AliasType"), "name": a.get("AliasName")}
            for a in aliases
            if isinstance(a, dict) and a.get("AliasName")
        ][:10]
    addresses = _cbdb_list(person.get("PersonAddresses"), "Address")
    if addresses:
        base["addresses"] = [
            {"type": a.get("AddrType"), "name": a.get("AddrName")}
            for a in addresses
            if isinstance(a, dict) and a.get("AddrName")
        ][:5]
    return {k: v for k, v in base.items() if v not in (None, [], {})}


def _lux_timespan(blob) -> Optional[str]:
    """Extract the display date from a Linked Art born/died timespan."""
    if not isinstance(blob, dict):
        return None
    ts = blob.get("timespan")
    if not isinstance(ts, dict):
        return None
    for ident in ts.get("identified_by") or []:
        if isinstance(ident, dict) and ident.get("content"):
            return ident["content"]
    return None


def parse_yale_lux(data, entity_id: str, user_language: str = "ko") -> dict:
    """Yale LUX Linked Art JSON-LD → label, dates, names.

    Verified with person/a6a10198-…: _label 'Yi, Kyu-bo, 1168-1241', nested
    born/died timespans, identified_by names. Only these are extracted; the raw
    JSON-LD graph is never surfaced."""
    base = {
        "source": "yale_lux",
        "node_type": "Person",
        "url": f"https://lux.collections.yale.edu/data/{entity_id}",
        "schema_hint": "Yale LUX에서 추출하는 것은 대표 라벨·이름 표기·생몰년뿐입니다.",
        "MUST_NOT_ADD": ["전기 서술·소장품 해석 (추출된 필드 외 추가 금지)"],
    }
    if not isinstance(data, dict):
        base["error"] = "unexpected response shape"
        return base
    base["primary_name"] = data.get("_label")
    born = _lux_timespan(data.get("born"))
    died = _lux_timespan(data.get("died"))
    if born:
        base["year_birth"] = born
    if died:
        base["year_death"] = died
    names = []
    for ident in data.get("identified_by") or []:
        if isinstance(ident, dict) and ident.get("type") == "Name" and ident.get("content"):
            names.append(ident["content"])
    if names:
        base["aliases"] = names[:10]
    return {k: v for k, v in base.items() if v not in (None, [], {})}


# ──────────────────────────────────────────────
# Response-side validation (defense in depth)
#
# Request-side prefix + endpoint separation is necessary but NOT sufficient:
# AKS Digerati answers HTTP 200 on both ports for any number that exists in that
# port's own namespace, and the returned record's own id then MATCHES the request
# (verified: :85/7249 → AkspId=7249 '신응시'; :88/18816 → AksloId=18816 '대홍산').
# So an id-equality check alone cannot detect cross-namespace contamination —
# only the SCHEMA can. These validators reject a 200 whose shape belongs to the
# other node type, and additionally catch a server returning a different record
# than the one requested.
# ──────────────────────────────────────────────
_PERSON_ID_FIELDS = ("AkspId", "PersonId")
_PLACE_ID_FIELDS = ("AksloId", "LocationId")


def _first_record(data: Any) -> tuple:
    """(record, error). AKS endpoints return a list of records."""
    if not isinstance(data, list) or not data:
        return None, "empty or unexpected response shape"
    rec = data[0]
    if not isinstance(rec, dict):
        return None, "first item is not a record"
    return rec, None


def _ids_match(returned: Any, requested: str) -> bool:
    """Compare the record's own numeric id with the requested numeric id."""
    if returned is None:
        return True          # field absent → nothing to contradict
    return str(returned).strip() == str(requested).strip()


def validate_aks_person_response(data: Any, requested_id: str) -> Optional[str]:
    """Accept only a Person-schema record whose AkspId matches the request.
    Returns an error string, or None when the response is acceptable."""
    rec, err = _first_record(data)
    if err:
        return err
    if any(f in rec for f in _PLACE_ID_FIELDS):
        return "response schema or identifier does not match requested Person authority record"
    if not any(f in rec for f in _PERSON_ID_FIELDS):
        return "response is missing Person identifiers"
    if not _ids_match(rec.get("AkspId"), requested_id):
        return "response schema or identifier does not match requested Person authority record"
    return None


def validate_aks_place_response(data: Any, requested_id: str) -> Optional[str]:
    """Accept only a Place-schema record whose AksloId matches the request."""
    rec, err = _first_record(data)
    if err:
        return err
    if any(f in rec for f in _PERSON_ID_FIELDS):
        return "response schema or identifier does not match requested Place authority record"
    if not any(f in rec for f in _PLACE_ID_FIELDS):
        return "response is missing Place identifiers"
    if not _ids_match(rec.get("AksloId"), requested_id):
        return "response schema or identifier does not match requested Place authority record"
    return None


# ──────────────────────────────────────────────
# Declarative registry
# ──────────────────────────────────────────────
@dataclass(frozen=True)
class AuthoritySourceConfig:
    key: str                       # unique registry key
    id_key: str                    # normalized key used in Entity.authority_ids
    neo4j_property: str
    node_types: frozenset
    capability: str
    label: str = ""
    id_pattern: Optional[re.Pattern] = None
    id_transform: Optional[Callable[[str], Optional[str]]] = None
    request_url: Optional[str] = None      # '{id}' template (transformed id)
    citation_url: Optional[str] = None     # '{id}' template (raw stored id)
    parser: Optional[Callable] = None
    # (raw_response, requested_id) -> error string | None. Runs BEFORE parsing so
    # a wrong-schema HTTP 200 can never become factual evidence.
    response_validator: Optional[Callable[[Any, str], Optional[str]]] = None
    allowed_fields: tuple = ()
    cache_ttl_sec: int = 3600
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    note: str = ""

    def validate_id(self, raw_id: str) -> bool:
        """Anchored, full-string validation of the ORIGINAL id (prefix included)."""
        if not raw_id or not isinstance(raw_id, str):
            return False
        if self.id_pattern is None:
            return True
        return bool(self.id_pattern.fullmatch(raw_id.strip()))

    def request_id(self, raw_id: str) -> Optional[str]:
        """Numeric/normalized id for the request — only after full validation."""
        if not self.validate_id(raw_id):
            return None
        if self.id_transform is not None:
            return self.id_transform(raw_id.strip())
        return raw_id.strip()


_WIKIDATA_FIELDS = (
    "primary_name", "primary_description", "names_by_lang",
    "descriptions_by_lang", "aliases", "birth_time", "death_time", "url",
)
_AKS_PERSON_FIELDS = (
    "name_kor", "name_chi", "year_birth", "year_death", "aliases",
    "addresses", "examination_entries", "canonical_link", "source_label",
)
_AKS_PLACE_FIELDS = (
    "name_kor", "name_chi", "location_id", "canonical_link", "source_label",
)
_LOC_FIELDS = ("authoritative_labels", "variant_labels", "year_birth", "year_death", "url")
_OPENLIBRARY_FIELDS = (
    "primary_name", "personal_name", "year_birth", "year_death", "aliases",
    "cross_source_ids", "url",
)
_CBDB_FIELDS = (
    "primary_name", "name_chi", "year_birth", "year_death", "dynasty",
    "index_address", "aliases", "addresses", "url",
)
_YALE_LUX_FIELDS = ("primary_name", "year_birth", "year_death", "aliases", "url")


AUTHORITY_REGISTRY: "dict[str, AuthoritySourceConfig]" = {
    c.key: c for c in [
        # ── fetchable ────────────────────────────────────────────────
        AuthoritySourceConfig(
            key="wikidata", id_key="wikidata", neo4j_property="idWikidata",
            node_types=frozenset({"Person"}), capability=CAPABILITY_FETCHABLE,
            label="Wikidata", id_pattern=_RE_WIKIDATA, id_transform=_identity,
            request_url="https://www.wikidata.org/wiki/Special:EntityData/{id}.json",
            citation_url="https://www.wikidata.org/wiki/{id}",
            parser=parse_wikidata, allowed_fields=_WIKIDATA_FIELDS,
        ),
        AuthoritySourceConfig(
            key="aks_digerati", id_key="aks_digerati", neo4j_property="idAKSdigerati",
            node_types=frozenset({"Person"}), capability=CAPABILITY_FETCHABLE,
            label="AKS Digerati (Person)", id_pattern=_RE_AKS_PERSON,
            id_transform=_digits_from(_RE_AKS_PERSON),
            request_url="https://digerati.aks.ac.kr:85/api/IdValues/{id}",
            parser=parse_aks_digerati, response_validator=validate_aks_person_response,
            allowed_fields=_AKS_PERSON_FIELDS,
            note="Port 85, koreanPerson_<n> ONLY. Cite the API's canonical Link, "
                 "never a URL built from koreanPerson_<n>.",
        ),
        AuthoritySourceConfig(
            # Distinct key AND distinct id_key: a Place's idAKSdigerati is a
            # different authority namespace, not the Person one.
            key="aks_digerati_place", id_key="aks_digerati_place",
            neo4j_property="idAKSdigerati",
            node_types=frozenset({"Place"}), capability=CAPABILITY_FETCHABLE,
            label="AKS Digerati (Place)", id_pattern=_RE_AKS_PLACE,
            id_transform=_digits_from(_RE_AKS_PLACE),
            request_url="https://digerati.aks.ac.kr:88/api/IdValues/{id}",
            parser=parse_aks_digerati_place,
            response_validator=validate_aks_place_response,
            allowed_fields=_AKS_PLACE_FIELDS,
            note="Port 88, koreanPlace_<n> ONLY. NEVER route a Place id to :85 — "
                 "it answers 200 with an unrelated Person record.",
        ),
        AuthoritySourceConfig(
            key="loc", id_key="loc", neo4j_property="idLOC",
            node_types=frozenset({"Person"}), capability=CAPABILITY_FETCHABLE,
            label="Library of Congress", id_pattern=_RE_LOC, id_transform=_identity,
            request_url="https://id.loc.gov/authorities/names/{id}.json",
            citation_url="https://id.loc.gov/authorities/names/{id}",
            parser=parse_loc, allowed_fields=_LOC_FIELDS, timeout_sec=8,
        ),
        AuthoritySourceConfig(
            key="open_library", id_key="open_library", neo4j_property="idOpenLibrary",
            node_types=frozenset({"Person"}), capability=CAPABILITY_FETCHABLE,
            label="Open Library", id_pattern=_RE_OPENLIBRARY, id_transform=_identity,
            request_url="https://openlibrary.org/authors/{id}.json",
            citation_url="https://openlibrary.org/authors/{id}",
            parser=parse_open_library, allowed_fields=_OPENLIBRARY_FIELDS,
        ),
        AuthoritySourceConfig(
            key="cbdb", id_key="cbdb", neo4j_property="idCBDB",
            node_types=frozenset({"Person"}), capability=CAPABILITY_FETCHABLE,
            label="CBDB", id_pattern=_RE_CBDB, id_transform=_identity,
            request_url="https://cbdb.fas.harvard.edu/cbdbapi/person.php?id={id}&o=json",
            citation_url="https://cbdb.fas.harvard.edu/cbdbapi/person.php?id={id}",
            parser=parse_cbdb, allowed_fields=_CBDB_FIELDS, timeout_sec=8,
        ),
        AuthoritySourceConfig(
            key="yale_lux", id_key="yale_lux", neo4j_property="idYaleLux",
            node_types=frozenset({"Person"}), capability=CAPABILITY_FETCHABLE,
            label="Yale LUX", id_pattern=_RE_YALE_LUX, id_transform=_identity,
            request_url="https://lux.collections.yale.edu/data/{id}",
            # data/{id} is the verified 200 URL; do not invent a /view/ path.
            citation_url="https://lux.collections.yale.edu/data/{id}",
            parser=parse_yale_lux, allowed_fields=_YALE_LUX_FIELDS, timeout_sec=8,
        ),

        # ── link_only (public URL verified; no usable API) ────────────
        AuthoritySourceConfig(
            key="aks_ency", id_key="aks_ency", neo4j_property="idAKSency",
            node_types=frozenset({"Person", "Place"}), capability=CAPABILITY_LINK_ONLY,
            label="AKS 한국민족문화대백과", id_pattern=_RE_AKS_ENCY,
            citation_url="https://encykorea.aks.ac.kr/Article/{id}",
        ),
        AuthoritySourceConfig(
            key="britannica", id_key="britannica", neo4j_property="idBritannica",
            node_types=frozenset({"Person"}), capability=CAPABILITY_LINK_ONLY,
            label="Britannica", id_pattern=_RE_BRITANNICA,
            citation_url="https://www.britannica.com/{id}",
        ),
        AuthoritySourceConfig(
            key="bnf", id_key="bnf", neo4j_property="idBNF",
            node_types=frozenset({"Person"}), capability=CAPABILITY_LINK_ONLY,
            label="BnF", id_pattern=_RE_BNF,
            citation_url="https://data.bnf.fr/ark:/12148/cb{id}",
        ),
        AuthoritySourceConfig(
            key="world_history", id_key="world_history", neo4j_property="idWorldHistory",
            node_types=frozenset({"Person"}), capability=CAPABILITY_LINK_ONLY,
            label="World History Encyclopedia", id_pattern=_RE_WORLD_HISTORY,
            citation_url="https://www.worldhistory.org/{id}/",
        ),

        # ── unsupported (verification failed — no fetch, no link) ─────
        AuthoritySourceConfig(
            key="nlk", id_key="nlk", neo4j_property="idNLK",
            node_types=frozenset({"Person"}), capability=CAPABILITY_UNSUPPORTED,
            label="국립중앙도서관", note="lod.nl.go.kr did not resolve; may need an Open API key.",
        ),
        AuthoritySourceConfig(
            key="ency_china", id_key="ency_china", neo4j_property="idEncyChina",
            node_types=frozenset({"Person"}), capability=CAPABILITY_UNSUPPORTED,
            label="中国大百科全书", note="zgbk.com returned an empty body; URL unverified.",
        ),
        AuthoritySourceConfig(
            key="academia_sinica", id_key="academia_sinica", neo4j_property="idAcademiaSinica",
            node_types=frozenset({"Person"}), capability=CAPABILITY_UNSUPPORTED,
            label="Academia Sinica", note="No documented API; ID resolution unconfirmed.",
        ),
        AuthoritySourceConfig(
            key="british_museum", id_key="british_museum", neo4j_property="idBritishMuseum",
            node_types=frozenset({"Person"}), capability=CAPABILITY_UNSUPPORTED,
            label="British Museum", note="HTTP 403 (bot-blocked); URL unverifiable.",
        ),
        AuthoritySourceConfig(
            key="aks_kdp", id_key="aks_kdp", neo4j_property="idAKSkdp",
            node_types=frozenset({"Person"}), capability=CAPABILITY_UNSUPPORTED,
            label="AKS KDP", note="people.aks.ac.kr pattern returned 404.",
        ),
        AuthoritySourceConfig(
            key="aks_sillok", id_key="aks_sillok", neo4j_property="idAKSsillok",
            node_types=frozenset({"Person"}), capability=CAPABILITY_UNSUPPORTED,
            label="조선왕조실록",
            note="Stored value is a person NAME (e.g. '송인(宋寅)'), not an id. Data fix needed.",
        ),
        AuthoritySourceConfig(
            key="aks_map", id_key="aks_map", neo4j_property="idAKSmap",
            node_types=frozenset({"Place"}), capability=CAPABILITY_UNSUPPORTED,
            label="AKS 동여도",
            note="kostma e-map unreachable; usable only via the AKS Place API's own Link.",
        ),
    ]
}

# Convenience views
FETCHABLE_SOURCES = tuple(
    c.key for c in AUTHORITY_REGISTRY.values() if c.capability == CAPABILITY_FETCHABLE
)
LINK_ONLY_SOURCES = {
    c.key: c.citation_url
    for c in AUTHORITY_REGISTRY.values()
    if c.capability == CAPABILITY_LINK_ONLY
}
# Neo4j property -> normalized id_key (used by the retrieval projections)
PROPERTY_TO_ID_KEY = {c.neo4j_property: c.id_key for c in AUTHORITY_REGISTRY.values()}


def sources_for_node_type(node_type: str, capability: Optional[str] = None) -> list:
    """Registry entries applicable to a node type, optionally filtered by
    capability. This is what drives orchestrator selection."""
    out = []
    for cfg in AUTHORITY_REGISTRY.values():
        if node_type and node_type not in cfg.node_types:
            continue
        if capability and cfg.capability != capability:
            continue
        out.append(cfg)
    return out


def resolve_source(source: str, node_type: str = "Person") -> Optional[AuthoritySourceConfig]:
    """Find the config for a source key or id_key, bound to node_type.

    Resolution order:
      1. exact registry key whose node_types allows `node_type`;
      2. any config whose id_key matches within the node type;
      3. legacy compatibility: an exact-key hit with the WRONG node type retries
         via the shared Neo4j property (so 'aks_digerati' + Place resolves to
         'aks_digerati_place', never to the Person endpoint);
      4. as a last resort the exact-key config is returned so the caller can
         produce a structured node-type error (its validate_id/node_types checks
         still block any request)."""
    if not source:
        return None
    source = source.strip().lower()
    cfg = AUTHORITY_REGISTRY.get(source)
    if cfg is not None and (not node_type or node_type in cfg.node_types):
        return cfg
    for candidate in AUTHORITY_REGISTRY.values():
        if candidate.id_key == source and node_type in candidate.node_types:
            return candidate
    if cfg is not None and node_type:
        for candidate in AUTHORITY_REGISTRY.values():
            if (candidate.neo4j_property == cfg.neo4j_property
                    and node_type in candidate.node_types):
                return candidate
    return cfg if cfg is not None else None


def link_only_reference(source: str, ext_id: str, node_type: str = "Person") -> Optional[dict]:
    """Build a citable, link-only reference. Returns None when the source is not
    link-only, the ID is missing/invalid, or no verified URL pattern exists —
    never fabricates a link."""
    cfg = resolve_source(source, node_type)
    if cfg is None or cfg.capability != CAPABILITY_LINK_ONLY:
        return None
    if not cfg.citation_url or not cfg.validate_id(ext_id):
        return None
    return {
        "source": cfg.key,
        "label": cfg.label,
        "fetchable": False,
        "status": "link_only",
        "url": cfg.citation_url.format(id=_url_safe(ext_id.strip())),
        "id": ext_id.strip(),
        "node_type": node_type,
    }


def _url_safe(value: str) -> str:
    """Percent-encode a path/query value while keeping already-safe path IDs
    (e.g. 'person/<uuid>', 'biography/Yi-Kyu-Bo') intact."""
    return quote(value, safe="/-_.:")


# ──────────────────────────────────────────────
# Structured programmatic entry point (used by the orchestrator)
# ──────────────────────────────────────────────
def fetch_authority(
    source: str,
    ext_id: str,
    *,
    node_type: str = "Person",
    language: Optional[str] = None,
    fetcher: Optional[Callable[[str], Optional[dict]]] = None,
) -> dict:
    """Fetch ONE authority record and return a STRUCTURED, non-fatal result.

    Shape:
        { source, key, id, node_type, capability, fetchable, status, url,
          data?, error?/note? }
    status ∈ {"ok", "unavailable", "error", "link_only", "unsupported"}

    Guarantees:
      * The ID must come from a graph node — never guessed from a name.
      * The ID is validated against the source's node-type-specific pattern
        BEFORE any URL is built, so an invalid/foreign ID makes no HTTP request.
      * Failures never raise; graph/vector evidence survives.
      * Successes cached by source|node_type|normalized_id; failures not cached.

    `fetcher` injects a fake HTTP layer for tests: callable url -> dict|None.
    """
    raw_source = (source or "").strip().lower()
    ext_id = (ext_id or "").strip()
    language = _effective_language(language)
    cfg = resolve_source(raw_source, node_type)

    if cfg is None:
        return {
            "source": raw_source, "id": ext_id, "node_type": node_type,
            "fetchable": False, "status": "error",
            "error": f"unknown authority source '{raw_source}'",
            "supported_sources": list(FETCHABLE_SOURCES),
        }

    base = {
        "source": cfg.id_key, "key": cfg.key, "label": cfg.label,
        "id": ext_id, "node_type": node_type, "capability": cfg.capability,
        "fetchable": cfg.capability == CAPABILITY_FETCHABLE,
    }

    if cfg.capability == CAPABILITY_UNSUPPORTED:
        return {**base, "status": "unsupported",
                "note": cfg.note or "source not supported; no data fetched and no link built"}

    if cfg.node_types and node_type not in cfg.node_types:
        return {**base, "status": "error",
                "error": f"source '{cfg.key}' does not apply to node type '{node_type}'"}

    if not ext_id:
        return {**base, "status": "error", "error": "empty id"}

    # Validate BEFORE constructing any URL — an invalid id must not cause a request.
    if not cfg.validate_id(ext_id):
        return {**base, "status": "error",
                "error": f"invalid id format for source '{cfg.key}'"}

    if cfg.capability == CAPABILITY_LINK_ONLY:
        ref = link_only_reference(cfg.key, ext_id, node_type)
        return {**base, "status": "link_only", "url": ref["url"] if ref else None,
                "note": "link-only source: no data fetched; cite as a reference link only"}

    request_id = cfg.request_id(ext_id)
    if not request_id:
        return {**base, "status": "error",
                "error": f"id transform failed for source '{cfg.key}'"}

    url = cfg.request_url.format(id=_url_safe(request_id))
    # Cache key uses the ORIGINAL authority id (prefix included), so
    # aks_digerati|Person|koreanPerson_7249 and
    # aks_digerati_place|Place|koreanPlace_7249 can never share an entry.
    cache_key = f"{cfg.key}|{node_type}|{ext_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    do_fetch = fetcher if fetcher is not None else (
        lambda u: _fetch(u, timeout=cfg.timeout_sec)
    )
    raw = do_fetch(url)
    if raw is None:
        # Not cached — retryable next turn.
        return {**base, "status": "unavailable", "url": url,
                "hint": "fetch failed/timeout/invalid payload; use graph-only info and note the gap."}

    # Response-side validation: a wrong-schema or wrong-record HTTP 200 is
    # rejected here, BEFORE parsing, and never becomes factual evidence.
    if cfg.response_validator is not None:
        validation_error = cfg.response_validator(raw, request_id)
        if validation_error:
            return {**base, "status": "error", "url": url,
                    "error": validation_error}

    parsed = cfg.parser(raw, request_id, language)
    if isinstance(parsed, dict) and parsed.get("error"):
        return {**base, "status": "unavailable", "url": url,
                "hint": f"authority returned no usable record ({parsed['error']})"}

    result = {
        **base,
        "status": "ok",
        # Prefer the API's own canonical link; else the verified citation URL.
        "url": (parsed.get("canonical_link") or parsed.get("url")
                or (cfg.citation_url.format(id=_url_safe(ext_id)) if cfg.citation_url else url)),
        "data": parsed,
    }
    _cache_set(cache_key, result, cfg.cache_ttl_sec)
    return result


# ──────────────────────────────────────────────
# String entry point (LangChain Tool.func — legacy ReAct fallback)
# ──────────────────────────────────────────────
def external_authority_lookup(query: str) -> str:
    """'source:id' → JSON string. Legacy input forms 'wikidata:<Q-id>' and
    'aks_digerati:<koreanPerson_id>' keep working (they resolve to the Person
    configs). Never raises."""
    if not isinstance(query, str) or ":" not in query:
        return json.dumps(
            {"error": "query must be 'source:id' form", "example": "wikidata:Q2913717",
             "supported_sources": list(FETCHABLE_SOURCES)},
            ensure_ascii=False,
        )

    source, ext_id = query.split(":", 1)
    node_type = "Place" if ext_id.strip().startswith("koreanPlace_") else "Person"
    result = fetch_authority(source, ext_id, node_type=node_type)

    if result.get("status") == "ok":
        payload = dict(result.get("data") or {})
        payload.setdefault("source", result.get("source"))
        payload["url"] = result.get("url")
    else:
        payload = {
            "error": result.get("error") or result.get("note")
            or "외부 정보 미조회 (fetch failed or timeout)",
            "source": result.get("source"), "id": result.get("id"),
            "status": result.get("status"),
        }
        if result.get("url"):
            payload["url"] = result["url"]
    return json.dumps(payload, ensure_ascii=False)

"""Authority source parsers — raw HTTP payload → allowlisted fields only.

책임: source별 응답 parser. raw payload를 그대로 반환하지 않고 명시적으로
추출한 필드만 돌려준다 (schema_hint / MUST_NOT_ADD guardrail 포함).
허용 의존성: 표준 라이브러리만. 외부 부작용: 없음 (HTTP는 client.py 몫).
기존 facade: tools/external_authority.py.

Moved verbatim from tools/external_authority.py (work order Phase 6.4).
"""

from __future__ import annotations

from typing import Any, Optional

# ──────────────────────────────────────────────
# Wikidata language preference
# ──────────────────────────────────────────────
_WIKIDATA_LANG_PRIORITY = {
    "ko": ["ko", "en", "zh", "ja"],
    "en": ["en", "ko", "zh"],
    "zh": ["zh", "en", "ko", "ja"],
}


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

"""Declarative authority source registry (single source of truth).

책임: `AUTHORITY_REGISTRY` — Person/Place authority source의 능력·ID 규칙·
요청/인용 URL·parser·allowlist 선언 — 와 source 해석/링크 전용 참조 생성.
허용 의존성: 표준 라이브러리 + chatbot.authority.{validators,parsers} (순수).
`requests`는 절대 여기서 import하지 않는다.
외부 부작용: 없음.
기존 facade: tools/external_authority.py (registry 사본 없이 re-export만).

Moved verbatim from tools/external_authority.py (work order Phase 6.1/6.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote

from chatbot.authority.parsers import (
    parse_aks_digerati,
    parse_aks_digerati_place,
    parse_cbdb,
    parse_loc,
    parse_open_library,
    parse_wikidata,
    parse_yale_lux,
)
from chatbot.authority.validators import (
    _RE_AKS_ENCY,
    _RE_AKS_PERSON,
    _RE_AKS_PLACE,
    _RE_BNF,
    _RE_BRITANNICA,
    _RE_CBDB,
    _RE_LOC,
    _RE_OPENLIBRARY,
    _RE_WIKIDATA,
    _RE_WORLD_HISTORY,
    _RE_YALE_LUX,
    _digits_from,
    _identity,
    validate_aks_person_response,
    validate_aks_place_response,
)

# HTTP policy defaults consumed by the registry's per-source overrides AND by
# chatbot.authority.client (which imports them from here so this module stays
# requests-free).
DEFAULT_TIMEOUT_SEC = 5

CAPABILITY_FETCHABLE = "fetchable"
CAPABILITY_LINK_ONLY = "link_only"
CAPABILITY_UNSUPPORTED = "unsupported"


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

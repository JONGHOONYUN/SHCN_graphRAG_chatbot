"""Compatibility facade — external authority enrichment.

구현은 large-module modularization work order에 따라 다음으로 이동했다:

    chatbot/authority/validators.py  ID 패턴·변환, AKS 응답 schema 검증
    chatbot/authority/parsers.py     source별 parser (allowlist 필드만 추출)
    chatbot/authority/registry.py    AUTHORITY_REGISTRY (단일 source of truth)
    chatbot/authority/cache.py       TTL/bounded cache (성공만 저장)
    chatbot/authority/client.py      HTTP fetch (requests는 그 모듈에서만)
    chatbot/authority/service.py     fetch_authority · external_authority_lookup

이 모듈은 기존 import 경로를 보존하는 re-export만 수행한다 — registry 사본
없음(§11.4), cache dict도 동일 객체 하나뿐이다. `requests`는 기존 테스트가
`ea.requests`로 접근하므로 계속 이 이름공간에 남긴다 (같은 모듈 객체).
"""

import requests  # noqa: F401  (legacy attribute: tests patch ea.requests.get)

from chatbot.authority.validators import (  # noqa: F401
    ID_TRANSFORMS,
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
    _PERSON_ID_FIELDS,
    _PLACE_ID_FIELDS,
    _digits_from,
    _first_record,
    _identity,
    _ids_match,
    _transform_aks_digerati_id,
    validate_aks_person_response,
    validate_aks_place_response,
)
from chatbot.authority.parsers import (  # noqa: F401
    _WIKIDATA_LANG_PRIORITY,
    _cbdb_list,
    _loc_values,
    _lux_timespan,
    parse_aks_digerati,
    parse_aks_digerati_place,
    parse_cbdb,
    parse_loc,
    parse_open_library,
    parse_wikidata,
    parse_yale_lux,
)
from chatbot.authority.registry import (  # noqa: F401
    AUTHORITY_REGISTRY,
    AuthoritySourceConfig,
    CAPABILITY_FETCHABLE,
    CAPABILITY_LINK_ONLY,
    CAPABILITY_UNSUPPORTED,
    DEFAULT_TIMEOUT_SEC,
    FETCHABLE_SOURCES,
    LINK_ONLY_SOURCES,
    PROPERTY_TO_ID_KEY,
    _AKS_PERSON_FIELDS,
    _AKS_PLACE_FIELDS,
    _CBDB_FIELDS,
    _LOC_FIELDS,
    _OPENLIBRARY_FIELDS,
    _WIKIDATA_FIELDS,
    _YALE_LUX_FIELDS,
    _url_safe,
    link_only_reference,
    resolve_source,
    sources_for_node_type,
)
from chatbot.authority.cache import (  # noqa: F401
    CACHE_KEY,
    CACHE_MAX_SIZE,
    _authority_cache,
    _cache_get,
    _cache_set,
    clear_authority_cache,
)
from chatbot.authority.client import (  # noqa: F401
    MAX_RESPONSE_BYTES,
    USER_AGENT,
    _JSON_CONTENT_TYPES,
    _fetch,
)
from chatbot.authority.service import (  # noqa: F401
    _effective_language,
    external_authority_lookup,
    fetch_authority,
)

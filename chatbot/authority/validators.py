"""Authority ID validators/transforms and response-side schema validators.

책임: source별 fullmatch ID 패턴, 요청용 ID 변환(숫자 추출 등), AKS
Person/Place 응답 schema 검증(교차 namespace 오염 차단).
허용 의존성: 표준 라이브러리만. 외부 부작용: 없음.
기존 facade: tools/external_authority.py.

Moved verbatim from tools/external_authority.py (work order Phase 6.2).
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

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

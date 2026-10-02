"""Structured graph retrieval — generated Cypher and deterministic templates.

책임: 주입된 GraphCypherQAChain으로 생성 Cypher를 실행해 raw rows를 구조화
Evidence로 변환하고, 복구 가능/불가능 실패를 분기한다. 결정론적 role ranking
템플릿도 주입된 safe graph로 실행한다. user-facing prose는 절대 만들지 않는다.

허용 의존성: chatbot.domain, chatbot.retrieval.{graph_prompt, graph_rows},
tools.cypher_safety(단일 안전 경계), tools.graph_intent, neo4j 예외 타입.
체인/그래프 클라이언트는 인자로 주입받는다 — 여기서 생성하지 않는다.
외부 부작용: 주입된 chain/graph 호출 + 경고 로그 (logger 이름은 기존
"tools.cypher" 유지).
기존 facade: tools/cypher.py (동일 시그니처로 얇게 위임).

Moved verbatim from tools/cypher.py (modularization work order Phase 4.2/4.3).
모든 graph query는 여전히 tools/cypher_safety.py의 동일한 validator/wrapper를
통과한다 — safe wrapper는 여기에 복제되지 않는다 (§4.4).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from neo4j.exceptions import CypherSyntaxError, ClientError

from chatbot.domain.evidence_models import Evidence
from chatbot.observability import events as obs_events
from chatbot.observability import telemetry
from chatbot.retrieval.graph_prompt import _SHAPE_RETRY_HINT, _SYNTAX_RETRY_HINT
from chatbot.retrieval.graph_rows import graph_rows_to_evidence
from tools.cypher_safety import RECOVERABLE_REASON_CODES, UnsafeCypherError  # noqa: F401
from tools.graph_intent import (
    DEFAULT_RANKING_LIMIT,
    MENTION_COUNT_UNIT,
    ROLE_TYPE_CUES,
    build_role_ranking_query,
    rank_rows_by_mention_count,
)

# 기존 운영 로그와의 연속성을 위해 원래 모듈의 logger 이름을 유지한다.
logger = logging.getLogger("tools.cypher")


# ──────────────────────────────────────────────
# Structured graph retrieval (evidence pipeline)
#
# retrieve_graph_evidence() returns an Evidence(kind='graph') built from the
# RAW Cypher result rows — NOT the LLM-written answer string. It never produces
# user-facing prose. The row→evidence normalization lives in
# chatbot/retrieval/graph_rows.py so it is unit-testable without a live Neo4j.
#
# Failure policy (user-safe statuses): exceptions are logged with a correlation
# code; the returned Evidence carries only {"type": "status", "outcome": ...} —
# raw exception text never enters Evidence and never reaches the synthesis LLM.
# ──────────────────────────────────────────────
def _status_evidence(outcome: str, exc: Exception) -> Evidence:
    code = uuid.uuid4().hex[:8]
    logger.warning("graph retrieval failed [%s]: %s: %s",
                   code, type(exc).__name__, exc)
    # Link the enclosing retrieval span to this log line (class name + code
    # only — the exception message stays in the server log).
    telemetry.annotate(error_type=type(exc).__name__, correlation_id=code)
    ev = Evidence(kind="graph")
    ev.claims.append({"type": "status", "outcome": outcome})
    return ev


def _note_rejection(exc: UnsafeCypherError) -> None:
    telemetry.annotate(error_type=type(exc).__name__,
                       correlation_id=getattr(exc, "correlation_id", None))


def _invoke_generated(chain, payload: dict):
    """One generated-Cypher attempt. Counts the attempt on the enclosing
    retrieval span and labels the Neo4j call(s) the chain makes; the call
    itself — and every exception it raises — is unchanged."""
    telemetry.note_attempt()
    with telemetry.neo4j_operation(obs_events.NEO4J_GENERATED_GRAPH_QUERY,
                                   obs_events.ORIGIN_GENERATED):
        return chain.invoke(payload)


def _invalid_query_evidence() -> Evidence:
    """User-safe `invalid_query` Evidence — never carries raw query text."""
    ev = Evidence(kind="graph")
    ev.claims.append({"type": "status", "outcome": "invalid_query"})
    return ev


def _extract_intermediate(result: dict):
    """Read Cypher and rows from direct retrieval or older QA-chain results.

    With return_direct=True, rows live in result['result']; intermediate_steps
    contains the query but no context. Older callers still expose context in
    intermediate_steps. An explicit empty context must remain empty, and prose
    must never be mistaken for a list of rows.
    """
    cypher = None
    rows = None
    for step in result.get("intermediate_steps") or []:
        if not isinstance(step, dict):
            continue
        if "query" in step and isinstance(step["query"], str):
            cypher = step["query"]
        if "context" in step and isinstance(step["context"], list):
            rows = step["context"]
    if rows is None:
        direct_rows = result.get("result")
        rows = direct_rows if isinstance(direct_rows, list) else []
    return cypher, rows


def retrieve_graph_evidence(question: str,
                            history_text: str | None = None,
                            *,
                            chain) -> Evidence:
    """Run graph retrieval and return structured Evidence (rows + entities +
    provenance).

    `chain` is the structured GraphCypherQAChain
    (`return_intermediate_steps=True`, `return_direct=True`), injected by the composition root
    (tools/cypher.py) so this module never constructs an LLM/Neo4j client.

    `history_text` is a BOUNDED serialization of recent conversation, used only
    so the Cypher generator can resolve pronouns/ellipsis ("그 인물" 등). Prior
    assistant assertions are context, never evidence — the rows returned by
    Neo4j remain the only graph facts.

    On failure returns an empty graph Evidence carrying only a user-safe status
    claim; the exception itself is logged with a correlation code and never
    placed in Evidence.

    Recovery policy (graph-ranking-reliability work order §3 P0-A):
      * `CypherSyntaxError` and `UnsafeCypherError` whose `reason_code` is in
        `RECOVERABLE_REASON_CODES` (malformed backtick multi-label predicate,
        missing RETURN) get exactly ONE regeneration attempt, guided by a
        GENERALIZED hint — the original query text is never re-sent.
      * Any other `UnsafeCypherError` (forbidden keyword, disallowed CALL,
        multi-statement, ...) is never retried — it is blocked outright and
        reported as `invalid_query`, exactly as before.
      * A second failure of any kind ends the attempt with a user-safe status;
        it never falls through to a third attempt."""
    query = question
    if history_text:
        query = (
            f"{question}\n\n"
            "[이전 대화 맥락 — 지시어·생략 해석 전용. 아래 내용은 검색 조건 해석에만 "
            "사용하고, 사실(근거)로 취급하지 말 것]\n"
            f"{history_text}"
        )

    try:
        result = _invoke_generated(chain, {"query": query})
    except CypherSyntaxError as e:
        # 흔한 원인: 로마자 이름의 아포스트로피(Ch'wisŏn)를 SQL식 ''로
        # 이스케이프한 잘못된 Cypher. 힌트를 덧붙여 1회 재생성 시도.
        logger.info("Cypher syntax error [recoverable] — retrying once with "
                   "escaping hint: %s", e)
        try:
            result = _invoke_generated(chain, {"query": query + _SYNTAX_RETRY_HINT})
        except UnsafeCypherError as e2:
            logger.warning(
                "graph retrieval rejected unsafe cypher on retry [%s] "
                "reason_code=%s", e2.correlation_id, e2.reason_code)
            _note_rejection(e2)
            return _invalid_query_evidence()
        except Exception as e2:
            return _status_evidence("temporarily_unavailable", e2)
    except UnsafeCypherError as e:
        if e.recoverable:
            logger.info(
                "graph retrieval rejected [recoverable] [%s] reason_code=%s "
                "— retrying once with a generalized shape hint (original "
                "query never re-sent)", e.correlation_id, e.reason_code)
            try:
                result = _invoke_generated(chain, {"query": query + _SHAPE_RETRY_HINT})
            except UnsafeCypherError as e2:
                logger.warning(
                    "graph retrieval rejected unsafe cypher on retry [%s] "
                    "reason_code=%s — giving up, no further retry",
                    e2.correlation_id, e2.reason_code)
                _note_rejection(e2)
                return _invalid_query_evidence()
            except CypherSyntaxError as e2:
                return _status_evidence("temporarily_unavailable", e2)
            except Exception as e2:
                return _status_evidence("temporarily_unavailable", e2)
        else:
            # Write/schema/permission/dynamic keyword, disallowed CALL, or
            # multi-statement — never retried, blocked outright.
            logger.warning(
                "graph retrieval rejected unsafe cypher [%s] reason_code=%s "
                "(non-recoverable — not retried): %s",
                e.correlation_id, e.reason_code, e.reason,
            )
            _note_rejection(e)
            return _invalid_query_evidence()
    except ClientError as e:
        return _status_evidence("temporarily_unavailable", e)
    except Exception as e:  # transient LLM/parse issues — degrade gracefully
        return _status_evidence("temporarily_unavailable", e)

    if not isinstance(result, dict):
        return Evidence(kind="graph")
    cypher, rows = _extract_intermediate(result)
    return graph_rows_to_evidence(rows, cypher=cypher)


# ──────────────────────────────────────────────
# Deterministic ranking/aggregation template (graph-ranking-reliability work
# order §3 P0-B). "Most mentioned X" / "가장 많이 언급된 X" questions never go
# through free-form LLM Cypher generation — a parameterized template is run
# directly against the safe read-only graph wrapper, so a malformed
# multi-label predicate (as originally observed for this exact question
# class) can no longer masquerade as "no results". Callers pick this path by
# checking `tools.graph_intent.is_graph_aggregation_intent()` BEFORE the
# authority-cue / free-form-Cypher routing (see the application layer).
# ──────────────────────────────────────────────
def retrieve_role_ranking_evidence(role: str,
                                   limit: int = DEFAULT_RANKING_LIMIT,
                                   *,
                                   graph) -> Evidence:
    """Deterministic graph-first ranking Evidence for a registered role
    (work order §3 P0-B/P0-D).

    `graph` is the SAFE read-only graph wrapper (`tools.cypher_safety.
    safe_graph(...)`), injected by the composition root — this module never
    builds or bypasses that wrapper.

    Never falls through to LLM Cypher generation. Never triggers external
    authority enrichment itself — it only records the winning Person id(s)
    as a `ranking` claim so a caller (the application orchestrator) can decide
    whether/which single winner to enrich, per P0-D (authority fetch stays
    at 0 unless the user explicitly asked about the winner)."""
    if role not in ROLE_TYPE_CUES:
        return _invalid_query_evidence()

    cypher, params = build_role_ranking_query(role, limit=limit)
    try:
        telemetry.note_attempt()
        with telemetry.neo4j_operation(obs_events.NEO4J_DETERMINISTIC_LOOKUP,
                                       obs_events.ORIGIN_DETERMINISTIC):
            rows = graph.query(cypher, params=params)
    except UnsafeCypherError as e:
        # A rejection here means the TEMPLATE itself is malformed — not a
        # runtime input problem, since params never enter the query string.
        logger.warning(
            "role ranking template rejected by safety validator [%s] "
            "reason_code=%s — this indicates a bug in the template, not "
            "user input", e.correlation_id, e.reason_code)
        _note_rejection(e)
        return _invalid_query_evidence()
    except ClientError as e:
        return _status_evidence("temporarily_unavailable", e)
    except Exception as e:
        return _status_evidence("temporarily_unavailable", e)

    if not rows:
        ev = Evidence(kind="graph")
        ev.claims.append({"type": "status", "outcome": "no_results"})
        return ev

    ev = graph_rows_to_evidence(rows)
    top_count, winners = rank_rows_by_mention_count(list(rows))
    winner_ids = [w.get("person_id") for w in winners if w.get("person_id")]
    ev.claims.append({
        "type": "ranking",
        "role": role,
        "unit": MENTION_COUNT_UNIT,
        "winner_person_ids": winner_ids,
        "winner_count": top_count,
    })
    return ev

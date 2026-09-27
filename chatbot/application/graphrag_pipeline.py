"""Normal graphRAG application pipeline — one request, one synthesis call.

단계(§3.2의 요청 처리 경계가 이 함수 안에서 순서대로 보인다):

    bounded history load
    -> gather evidence            (graph → vector → optional authority)
    -> total retrieval failure short circuit   (LLM 호출 없음)
    -> format evidence            (블록·예산)
    -> final synthesis LLM        (합성 단계의 유일한 LLM 호출 — 검색 단계의
                                   Cypher 생성·Graph QA 호출은 별도로 계측됨)
    -> deterministic citation assembly
    -> successful final answer persistence     (성공 시 정확히 1회)

허용 의존성: chatbot.domain/synthesis + tools.orchestrator(=application
orchestrator facade) + tools.answer_renderer + chatbot.observability.
레거시 ReAct 구현은 절대 import하지 않는다 (§3.3/§8.3).
주입: 합성 체인(`synthesis_chain`), 이력 핸들 팩토리(`history_factory`),
`session_id`. 따라서 이 모듈은 Streamlit session state를 직접 읽지 않고 LLM·
Neo4j 클라이언트를 생성하지도 않는다 — 둘 다 composition root(agent.py) 몫이다.
외부 부작용: 주입된 retriever/LLM 호출, 성공 시 이력 쓰기 1회.
기존 facade: agent.py의 `synthesize_answer` (동일 시그니처로 위임).

관측(Phase 1): 각 단계가 span 하나(`history.read` · `evidence.gather` ·
`evidence.format` · `synthesis` · `citations.build` · `answer.render` ·
`history.write`)를 발행하고, 전체는 `pipeline.graphrag.completed` 하나로 묶인다.
단계의 입력·출력·예외·호출 순서는 계측 전과 동일하다.

Moved verbatim from agent.py (modularization work order Phase 8.2).
"""

from __future__ import annotations

import logging
import uuid as _uuid

from chatbot.domain.evidence_merge import collect_node_references
from chatbot.observability import events as obs
from chatbot.observability import telemetry
from chatbot.synthesis.evidence_format import (
    both_retrievals_failed,
    format_evidence_for_prompt,
    retrieval_failure_message,
)
from chatbot.synthesis.citations import build_citations
from chatbot.synthesis.history_format import serialize_chat_history
from chatbot.synthesis.prompt import _build_language_directive
from tools.answer_renderer import (
    assemble_final_answer,
    derive_referenced_node_ids,
    strip_model_sources,
)
from tools.orchestrator import gather_graphrag_evidence

# 기존 운영 로그와의 연속성을 위해 원래 모듈의 logger 이름을 유지한다.
_logger = logging.getLogger("agent")


def _evidence_counts(evidence) -> dict:
    """Item counts per evidence source — sizes only, never content."""
    def _docs(key):
        return len(getattr(evidence.get(key), "documents", None) or [])
    external = getattr(evidence.get("external"), "claims", None) or []
    return {
        "evidence_graph_count": _docs("graph"),
        "evidence_vector_count": _docs("vector"),
        "evidence_external_count": len([
            c for c in external
            if not (isinstance(c, dict) and c.get("type") == "coverage")]),
    }


def _history_db_span(operation: str):
    # Neo4jChatMessageHistory runs its own driver calls: neither the row count
    # of a write nor any driver-internal retry is observable here -> null.
    return telemetry.span(obs.NEO4J_QUERY, operation=operation,
                          query_origin=obs.ORIGIN_FRAMEWORK,
                          safety_result=obs.SAFETY_NOT_APPLICABLE,
                          row_count=None, attempt_count=None)


def load_bounded_history(history_factory, session_id: str) -> str:
    """직전 대화를 bounded 직렬화. 이력 조회 실패는 답변을 막지 않는다(빈 이력)."""
    with telemetry.span(obs.HISTORY_READ) as read_span:
        try:
            # 이력 핸들 생성(세션 노드 준비)과 메시지 조회를 한 DB 구간으로 잰다.
            with _history_db_span(obs.NEO4J_HISTORY_READ) as db_span:
                messages = history_factory(session_id).messages
                count = len(messages) if isinstance(messages, list) else None
                db_span.set(row_count=count)
            read_span.set(history_message_count=count)
            return serialize_chat_history(messages)
        except Exception as exc:
            read_span.set(status=obs.STATUS_ERROR, error_type=type(exc).__name__)
            return ""


def synthesize_answer(user_input: str, response_language: str,
                      question_language: str,
                      *,
                      synthesis_chain,
                      history_factory,
                      session_id: str) -> str:
    """graphRAG 최종 합성. bounded 대화 이력 + 구조화된 근거로 단일 LLM 호출.

    work order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md §3:
    `question_language`(질문/검색 언어)와 `response_language`(최종 출력 언어)를
    분리한다. 하위 호환 기본값(`question_language or response_language`)은
    호출자인 agent.py facade가 적용한다.

    `user_input`은 이미 언어 제어 문구가 제거된 question_text여야 한다
    (bot.py의 `resolve_languages()`가 보장) — 이 함수는 별도로 번역하거나
    제어 문구를 다시 파싱하지 않는다.

    이력 정책 (persistence 소유권):
    - 정상 경로(이 함수)가 성공 답변을 반환하기 직전에 user/assistant 메시지를
      ::graphRAG 이력에 저장한다 (`history_factory`가 만든 핸들).
    - 레거시 ReAct 폴백 경로는 RunnableWithMessageHistory가 자체 저장하므로,
      이 함수가 빈 출력/예외로 폴백에 넘어간 경우 여기서는 저장하지 않는다
      (이중 저장 방지 — 경로당 소유자 1곳).

    관측: 요청 문맥이 이미 있으면(bot.py/agent.py가 연 경우) 그대로 쓰고,
    없으면(테스트·CLI) 이 호출 동안만 root 문맥을 연다.
    """
    with telemetry.request_scope(mode=obs.MODE_GRAPHRAG,
                                 question_language=question_language,
                                 response_language=response_language):
        with telemetry.span(obs.PIPELINE_GRAPHRAG) as pipeline_span:
            return _run_graphrag(
                user_input, response_language, question_language,
                synthesis_chain=synthesis_chain,
                history_factory=history_factory,
                session_id=session_id,
                pipeline_span=pipeline_span)


def _run_graphrag(user_input: str, response_language: str,
                  question_language: str, *, synthesis_chain, history_factory,
                  session_id: str, pipeline_span) -> str:
    """정상 graphRAG 본체. 각 단계가 자기 span 하나를 발행한다 — 단계의 입력·
    출력·예외는 계측 전과 동일하다."""
    history_text = load_bounded_history(history_factory, session_id)

    # vector retriever에는 question_language(검색 index 언어)가 전달되고,
    # external authority fetch/coverage 문구에는 response_language가 전달된다
    # (gather_graphrag_evidence의 `language`=question_language,
    # `response_language`=response_language 분리 — 하위 호환을 위해
    # `response_language`를 생략하면 내부적으로 `language`와 같다고 처리됨).
    gather_span = telemetry.start_span(obs.EVIDENCE_GATHER)
    evidence = gather_graphrag_evidence(
        user_input, question_language, history_text=history_text or None,
        response_language=response_language)
    counts = _evidence_counts(evidence)
    gather_span.finish(**counts)

    # 그래프·벡터 검색이 모두 일시 불가하고 외부 근거도 없으면, pretraining으로
    # 메우지 않고 즉시 락 언어 안내를 반환한다 (LLM 호출 없음).
    if both_retrievals_failed(evidence.get("statuses") or {}) \
            and not evidence["external"].claims:
        failure = retrieval_failure_message(response_language)
        telemetry.emit(obs.SYNTHESIS, status=obs.STATUS_SKIPPED,
                       synthesis_executed=False)
        telemetry.set_outcome(obs.OUTCOME_SHORT_CIRCUIT)
        telemetry.set_answer_stats(answer_chars=len(failure), citation_count=0)
        pipeline_span.set(outcome=obs.OUTCOME_SHORT_CIRCUIT, short_circuit=True,
                          synthesis_executed=False, answer_chars=len(failure),
                          citation_count=0, **counts)
        return failure

    with telemetry.span(obs.EVIDENCE_FORMAT) as format_span:
        evidence_blocks = format_evidence_for_prompt(evidence, response_language)
        format_span.set(evidence_chars=len(evidence_blocks))

    with telemetry.span(obs.SYNTHESIS, synthesis_executed=True) as synthesis_span:
        body = synthesis_chain.invoke(
            {
                "language_directive": _build_language_directive(response_language),
                "chat_history": history_text or "(이전 대화 없음)",
                "question": user_input,
                "evidence_blocks": evidence_blocks,
            }
        )
        synthesis_span.set(
            output_chars=len(body) if isinstance(body, str) else None)

    # ── Deterministic final assembly (Sources는 LLM이 아니라 코드가 조립) ──
    # LLM이 Sources를 생략·축약·URL 제거해도 최종 응답에는 build_citations()의
    # 검증된 bullet 전체가 그대로 붙는다. 본문 entity 링크도 코드 수준에서 보장하며,
    # Person/Place뿐 아니라 Work/Entry/Poem/Critique/Topic/Era/CriticalTerm까지
    # NodeReference로 커버한다.
    with telemetry.span(obs.CITATIONS_BUILD) as citations_span:
        correlation_id = _uuid.uuid4().hex[:8]
        all_node_refs = collect_node_references(evidence["graph"], evidence["vector"])
        node_ref_dicts = [r.to_dict() for r in all_node_refs]
        link_targets = [
            e.to_dict() if hasattr(e, "to_dict") else e
            for e in (evidence.get("entities") or [])
        ] + node_ref_dicts

        # referenced_node_ids: 모델이 실제로 언급/사용한 id만 "poetrytalks wikidata"
        # 그룹에 남긴다 (검색만 되고 답변에서 쓰이지 않은 node는 제외). 이력에 남을
        # 모델 Sources를 먼저 제거한 sanitized body를 기준으로 판단한다.
        sanitized_body, _ = strip_model_sources(body or "")
        referenced_ids = derive_referenced_node_ids(sanitized_body, node_ref_dicts)
        citations = build_citations(evidence, response_language,
                                    referenced_node_ids=referenced_ids)
        citations_span.set(citation_count=len(citations))

    with telemetry.span(obs.ANSWER_RENDER) as render_span:
        output = assemble_final_answer(
            body, citations, response_language,
            entities=link_targets, correlation_id=correlation_id,
        )
        render_span.set(
            answer_chars=len(output) if isinstance(output, str) else None)
    _logger.debug(
        "graphRAG synthesis [%s]: body_chars=%d citations=%d referenced_ids=%d",
        correlation_id, len(body or ""), len(citations), len(referenced_ids),
    )

    if output and output.strip():
        # 조립 완료된 최종 응답만 저장. 저장 실패는 답변을 막지 않는다.
        with telemetry.span(obs.HISTORY_WRITE) as write_span:
            try:
                with _history_db_span(obs.NEO4J_HISTORY_WRITE):
                    hist = history_factory(session_id)
                    hist.add_user_message(user_input)
                    hist.add_ai_message(output)
            except Exception as exc:
                write_span.set(status=obs.STATUS_ERROR,
                               error_type=type(exc).__name__)
    else:
        telemetry.emit(obs.HISTORY_WRITE, status=obs.STATUS_SKIPPED)

    answer_chars = len(output) if isinstance(output, str) else None
    telemetry.set_answer_stats(answer_chars=answer_chars,
                               citation_count=len(citations))
    pipeline_span.set(
        status=obs.STATUS_SUCCESS if output and output.strip() else obs.STATUS_EMPTY,
        outcome=obs.OUTCOME_SUCCESS, short_circuit=False,
        synthesis_executed=True, answer_chars=answer_chars,
        citation_count=len(citations), **counts)
    return output

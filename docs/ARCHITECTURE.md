# 시화총림 챗봇 — 패키지 구조와 의존성 방향

이 문서는 `CLAUDE_CODE_LARGE_MODULE_MODULARIZATION.md` 작업으로 정리된 계층
구조와, 각 계층이 무엇을 import해도 되고 무엇을 import하면 안 되는지를 기록한다.
사용자 동작·검색 결과·출처 정책·보안 정책은 이 작업으로 바뀌지 않았다.

---

## 1. 계층과 허용 의존성

```text
presentation / composition root
  bot.py · agent.py · text_rag.py · tools/*.py (facades)
  └ Streamlit session state를 읽고, LLM·Neo4j 클라이언트를 만들어 아래로 주입
        │
        ▼
application            chatbot/application/
  graphrag_pipeline · vectorrag_pipeline · evidence_orchestrator
  retrieval_policy · retriever_invocation · authority_enrichment
  └ retrieval · authority · synthesis · domain 을 import 가능
        │
        ├─────────────► retrieval        chatbot/retrieval/
        │                 graph_prompt · graph_query · graph_rows
        │                 vector_query · vector_retriever · vector_documents
        │                 └ domain, authority.registry, tools.cypher_safety,
        │                   tools.graph_intent 만 import (클라이언트는 주입)
        │
        ├─────────────► authority        chatbot/authority/
        │                 validators → parsers → registry → cache/client → service
        │                 └ registry·parsers·validators는 순수,
        │                   `requests`는 client.py 한 곳에서만
        ▼
synthesis              chatbot/synthesis/
  language_fields · source_policy · history_format
  evidence_format · citations · prompt
  └ domain 만 import (+ allowlist 조회용 authority.registry 지연 import)
        │
        ▼
domain                 chatbot/domain/
  node_identity · evidence_models · evidence_merge · retrieval_models
  └ 표준 라이브러리만

legacy                 chatbot/legacy/
  react_prompt · react_agent · react_vector_tool · graph_qa
  └ 정상 파이프라인은 이 계층을 import하지 않는다. 호출은 오직 agent.py의
    최상위 오류 정책 분기에서만.

observability          chatbot/observability/   (모든 계층이 사용할 수 있는 횡단 계층)
  events · sinks · context · emitter · spans · telemetry · usage · callbacks
  └ callbacks.py를 제외하면 표준 라이브러리만 쓴다. 다른 chatbot 계층을
    import하지 않으며, 계측 호출은 절대 예외를 전파하지 않는다.
    자세한 내용은 docs/OBSERVABILITY.md.
```

### 금지된 방향 (테스트로 강제됨 — `tests/test_modularization_contracts.py`)

| 금지 | 강제 방법 |
|---|---|
| domain → Streamlit / Neo4j / LangChain / requests | AST 검사 + 서브프로세스 import 검사 |
| synthesis → LLM client / Neo4j client / Streamlit | AST 검사 + 서브프로세스 import 검사 |
| retrieval · application → `st.session_state` | 소스 검사 |
| `requests` → authority의 client 이외 모듈 | AST 검사 |
| legacy → 정상 graphRAG 파이프라인 내부 | 소스 검사 (`chatbot.legacy` 문자열 부재) |
| facade ↔ 구현 순환 import | 전 모듈 일괄 import 검사 |

---

## 2. 요청 처리 경계 (정상 graphRAG)

`chatbot/application/graphrag_pipeline.py`의 `synthesize_answer()` 한 함수에서
아래 순서가 그대로 읽힌다.

```text
bounded history load            load_bounded_history()          — 실패해도 빈 이력
  -> gather evidence            tools.orchestrator              — graph → vector → authority
  -> total failure short circuit                                — LLM 호출 0회
  -> format evidence            synthesis.evidence_format       — 블록·예산
  -> final synthesis LLM        주입된 synthesis_chain          — 이 경로의 유일한 LLM 호출
  -> deterministic citations    synthesis.citations
  -> final answer assembly      tools.answer_renderer
  -> history persistence                                        — 성공 시 정확히 1회
```

textRAG는 `chatbot/application/vectorrag_pipeline.py`가 같은 결정론적 조립
경계(`build_citations` → `assemble_final_answer`)를 공유하되, 대화 이력
namespace(`::textRAG`)는 공유하지 않는다.

---

## 3. 클라이언트 소유권

| 리소스 | 생성 위치 | 주입 대상 |
|---|---|---|
| chat LLM · embeddings | `llm.py` | agent.py / text_rag.py / tools/vector.py / tools/cypher.py |
| Neo4j graph | `graph.py` | 위와 동일 |
| read-only safe graph wrapper | `tools/cypher.py` (`safe_graph(graph)`) | `chatbot.retrieval.graph_query` |
| GraphCypherQAChain (prose / structured) | `tools/cypher.py` | graph_query · legacy.graph_qa |
| 최종 합성 체인 | `agent.py` | graphrag_pipeline |
| Neo4j 대화 이력 핸들 | `agent.py` / `text_rag.py` | graphrag_pipeline / vectorrag_pipeline |
| 언어별 vector retriever 캐시 | `chatbot/retrieval/vector_retriever.py` (dict 1개씩) | tools/vector.py · text_rag.py가 동일 객체 재노출 |

`llm.py` / `graph.py`의 module-level 클라이언트는 이번 작업에서 유지했다. 이유는
(a) `bot.py`가 인증 성공 후에만 이 모듈을 import하는 lazy 정책이 이미 있고,
(b) 여러 테스트가 이 모듈 이름을 그대로 reload하기 때문이다. 제거 조건: 인증
게이트가 `bot.py` 밖으로 옮겨지거나, 프로세스당 1개가 아닌 요청별 클라이언트가
필요해질 때. 그때는 이 두 모듈을 factory 함수로 바꾸고 facade에서 호출하면 된다.

---

## 4. 단일 source of truth

| 항목 | 소유 모듈 |
|---|---|
| node ID prefix · Poetry Talks URL 규칙 | `chatbot/domain/node_identity.py` |
| Evidence / Entity / Provenance / NodeReference | `chatbot/domain/evidence_models.py` |
| 언어별 vector index 설정 | `rag_config.py` |
| authority registry | `chatbot/authority/registry.py` |
| source/conflict policy 텍스트 | `chatbot/synthesis/source_policy.py` |
| citation label | `chatbot/synthesis/citations.py` |
| retrieval cap · authority cap | `chatbot/application/retrieval_policy.py` |
| history budget | `chatbot/synthesis/history_format.py` |
| safe Cypher validator | `tools/cypher_safety.py` (이동하지 않음) |
| Cypher 생성 프롬프트 | `chatbot/retrieval/graph_prompt.py` |
| 레거시 ReAct 프롬프트 | `chatbot/legacy/react_prompt.py` |

---

## 5. 확장 지점

`chatbot/domain/retrieval_models.py`가 현재 구현이 이미 만족하는 최소 계약
(`RetrievalRequest`, `EvidenceRetriever`)을 선언한다. 새 근거 소스를 추가할 때:

1. **retriever 등록** — `gather_graphrag_evidence(..., graph_retriever=...)`처럼
   application layer 인자로 넘긴다. 기존 graph/vector 코드는 수정하지 않는다.
2. **formatter 등록** — `chatbot/synthesis/evidence_format.py`의
   `EVIDENCE_BLOCK_FORMATTERS` / `EVIDENCE_BLOCK_ORDER`에 항목을 추가한다.
   `format_evidence_for_prompt()` 본문은 건드리지 않는다.
3. **citation 전략 추가** — `chatbot/synthesis/citations.py`의 `build_citations()`
   에 소스별 분기를 더한다. graph citation 로직은 그대로 둔다.
4. **새 Neo4j label** — `chatbot/retrieval/graph_rows.py`의 mapper에서 다룬다.
   domain의 전역 registry는 실제 데이터에 그 prefix가 생길 때만 바꾼다.

현재 등록된 소스는 `graph`, `vector`, `external` 셋뿐이며, 확정되지 않은 미래
데이터 모델(용어집·연구논문)을 위한 노드·필드·Evidence kind는 추가하지 않았다.

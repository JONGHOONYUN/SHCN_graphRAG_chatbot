# Phase 1 관측성 구현 보고서

작업 지시서: `CLAUDE_CODE_PHASE1_OBSERVABILITY.md`
성격: **측정 전용**. 답변·검색·오류·폴백 동작, 모델·prompt·retry·timeout, 검색
순서·top-k, 라우팅, 인용 형식은 바꾸지 않았다. Graph QA LLM 호출도 그대로다.
운영 문서: `docs/OBSERVABILITY.md`

---

## 1. 요약

| 항목 | 결과 |
|---|---|
| 기준선 테스트 (작업 전) | `Ran 581 tests / OK` |
| 최종 테스트 | `Ran 681 tests / OK` (기존 581 + 신규 100) |
| compile 검사 | `python -m compileall …` exit 0 |
| 목적별 LLM 호출 분리 | 결정적 GraphRAG 테스트에서 `cypher_generation=1`, `graph_qa=1`, `final_synthesis=1` 검증 |
| 공개 API 방식 | GraphCypherQAChain의 공개 파라미터 `cypher_llm`/`qa_llm` + LangChain 공개 API `register_configure_hook`. private API·monkey patch 없음 |
| 새 외부 의존성 | 없음 (표준 라이브러리 + 기존 langchain_core) |
| 개인정보 | marker 11종을 심은 전체 실행에서 이벤트 누출 0건 |
| 동작 보존 | 관측 on / off / sink 실패 세 경우 답변 문자열 동일 (자동 테스트) |

작업 중지 조건(§18)에 해당하는 상황은 없었다. 가장 큰 위험이던 "GraphCypherQAChain
내부 두 LLM을 공개 API로 구분할 수 있는가"는 구현 전에 가짜 모델로 실증했다(§4).

---

## 2. 작업 지시서 기준선과 실제 코드의 차이

| 지시서 기술 | 실제 코드 | 처리 |
|---|---|---|
| route `general_chat` (§7.1) | 일반 대화는 독립 경로가 없고 ReAct의 tool로만 실행된다 | route 값은 예약. 목적은 `purpose=general_chat`으로 구분 |
| purpose `language_detection` (§8.2) | 언어 판별은 휴리스틱이며 LLM을 쓰지 않는다 | 예약값으로만 둠 |
| 목적값 목록 (§10.2) | ReAct의 "Sihwa Content Search" tool이 별도 LLM으로 prose를 만든다 | `legacy_vector_answer` 목적을 추가 |
| 파일 구성 (§4.2) | `telemetry.py`가 522줄이 되어 이전 작업의 "실행 로직 모듈 500행" 계약 테스트에 걸림 | sink·발행은 `emitter.py`, span은 `spans.py`로 분리. `telemetry.py`는 facade |
| 이벤트 목록 (§8.2) | 11.3의 "citation 생성"과 11.9의 "client HTTP 결과"에 해당하는 이벤트가 목록에 없음 | `citations.build.completed`, `authority.http.completed` 추가 |
| 예상 GraphRAG 호출 (§2.3) | 코드와 일치: Cypher 생성 LLM → Neo4j(생성 Cypher) → Graph QA LLM → embedding → Neo4j(vector) → 최종 합성 LLM, 그리고 이력 읽기/쓰기 DB 호출 | 전부 별도 이벤트로 기록 |
| 테스트 기준선 581 (§2.4) | 작업 시작 시 재측정 결과 동일 | — |

---

## 3. 변경 파일

### 새 파일

| 파일 | 줄 | 책임 |
|---|---:|---|
| `chatbot/observability/__init__.py` | 28 | 패키지 규칙 문서 |
| `chatbot/observability/events.py` | 378 | 이벤트 이름·허용 필드 allowlist·enum·검증·JSON |
| `chatbot/observability/sinks.py` | 97 | `EventSink` protocol, Null/Logging/Memory sink |
| `chatbot/observability/context.py` | 199 | `RequestContext`, ContextVar, 요청 누계 |
| `chatbot/observability/emitter.py` | 183 | sink 설정(환경변수/주입), 발행, 내부 오류 보고 |
| `chatbot/observability/spans.py` | 206 | perf_counter span, annotate, 시도 횟수, Neo4j operation 라벨 |
| `chatbot/observability/telemetry.py` | 213 | 공개 facade + `request_scope` + 누계 API |
| `chatbot/observability/usage.py` | 149 | provider usage 정규화 |
| `chatbot/observability/callbacks.py` | 181 | LangChain callback handler, `with_llm_purpose` |
| `chatbot/retrieval/graph_chain.py` | 39 | GraphCypherQAChain builder (내부 두 LLM에 목적 부여) |
| `docs/OBSERVABILITY.md` | — | 운영·해석 문서 |
| `tests/test_observability_{events,context,callbacks,pipelines,privacy}.py` | — | 신규 테스트 100개 |
| `tests/_observability_fixtures.py` | 290 | 결정적 test double (테스트 모듈 아님) |
| `tests/observability_baseline.py` | 146 | provider 없는 dry run (테스트 모듈 아님) |

### 수정한 파일 (모두 계측 훅만 추가)

| 파일 | 변경 |
|---|---|
| `bot.py` | `handle_submit`의 backend dispatch를 `request_scope`로 감쌈. catch-all 경로의 `[ref:]` correlation id를 요청 이벤트에 연결. UI·메시지·상태 처리 불변 |
| `agent.py` | 최종 합성·ReAct·General Chat LLM에 목적 부여. `generate_response`를 요청 문맥으로 감싸고 기존 정책 본체를 `_generate_response_policy`로 옮김(분기·catch 범위 불변). 폴백 시 `fallback.completed` span과 route 갱신 |
| `text_rag.py` | vectorRAG 답변 LLM 목적 부여, 요청 문맥 |
| `tools/cypher.py` | 두 GraphCypherQAChain을 builder로 생성 (같은 llm·prompt·flag) |
| `tools/cypher_safety.py` | 두 wrapper 변형이 공유하는 `_observed_query`로 `neo4j.query.completed` 발행. `_read_with_retry`에 시도 기록. 검증·재시도 정책 불변 |
| `tools/vector.py` | 레거시 벡터 tool LLM 목적 부여 |
| `llm.py` | `GoogleEmbeddings.embed_query/embed_documents`에 `embedding.completed`, 재시도 루프에 시도 기록 |
| `chatbot/application/graphrag_pipeline.py` | 단계별 span 7종 + 파이프라인 span, short circuit·답변 통계 |
| `chatbot/application/vectorrag_pipeline.py` | 파이프라인 span, retriever 호출 span |
| `chatbot/application/evidence_orchestrator.py` | graph·vector·authority 검색기별 span |
| `chatbot/application/retriever_invocation.py` | 흡수한 예외의 클래스 이름·로그 correlation id를 span에 annotate |
| `chatbot/retrieval/graph_query.py` | 생성 Cypher 호출마다 시도 기록 + Neo4j operation 라벨, 결정적 순위 질의 라벨 |
| `chatbot/retrieval/vector_retriever.py` | `InstrumentedNeo4jVector`(공개 메서드 override로 vector query를 embedding과 분리해 측정) |
| `chatbot/synthesis/prompt.py` | `build_synthesis_chain(llm, prompt)` — agent.py와 테스트가 같은 조립을 쓰도록 |
| `chatbot/authority/client.py` | `authority.http.completed` (상태 코드·크기만) |
| `chatbot/authority/service.py` | `retrieval.authority_source.completed` (cache hit/miss, HTTP 여부) |
| `docs/ARCHITECTURE.md` | 관측성 계층 위치 한 단락 |

기존 테스트 파일은 **하나도 수정하지 않았다.** `.streamlit/`, `neo4j_data_import/`는
변경 없음.

---

## 4. Graph QA 내부 호출 분리 방식

`langchain_neo4j 0.8.0`의 `GraphCypherQAChain.from_llm`은 `llm` 대신
`cypher_llm`과 `qa_llm`을 **공개 파라미터로 따로** 받는다. 기존 코드는
`from_llm(llm, ...)`이었고, 이 경우 내부에서 `cypher_llm = qa_llm = llm`이 된다.

새 builder(`chatbot/retrieval/graph_chain.py`)는 같은 `llm` 객체를 두 번 감싼다.

```python
cypher_llm = llm.with_config(metadata={"llm_purpose": "cypher_generation"})
qa_llm     = llm.with_config(metadata={"llm_purpose": "graph_qa"})
GraphCypherQAChain.from_llm(cypher_llm=cypher_llm, qa_llm=qa_llm, graph=..., ...)
```

`with_config`는 모델·temperature·retry를 건드리지 않고 callback metadata만 더하는
`RunnableBinding`이다. prompt, `return_intermediate_steps`,
`allow_dangerous_requests`, `verbose`, `return_direct=False`는 그대로이며 Graph QA
호출은 계속 일어난다. 구현 전 가짜 모델로 두 호출이 callback에서 서로 다른
`llm_purpose`로 보이는지 먼저 확인했다.

purpose를 **leaf LLM에만** 묶는 이유: LangChain은 부모 config의 metadata가 자식
바인딩 값을 덮어쓴다(`merge_configs`에서 호출 시점 config가 우선). chain에 purpose를
붙이면, 예컨대 ReAct 안에서 호출된 Graph QA도 `react_iteration`으로 보이게 된다.

---

## 5. callback과 token usage 처리

**handler 설치.** 요청 문맥이 열릴 때 요청 전용 `LLMTelemetryHandler`를 ContextVar에
넣는다. 이 ContextVar는 모듈 import 시 공개 API `register_configure_hook`로
등록되어 있어, 그 요청 안에서 구성되는 모든 callback manager에 handler가 자동으로
(포인터 비교로 중복 없이) 추가된다. 그래서 기존 invoke 경로에 `callbacks=` 인자를
하나도 추가하지 않았다. handler는 자기 `RequestContext` 참조를 쥐고 있어 callback이
다른 스레드에서 전달돼도 올바른 요청에 집계된다.

**무엇을 세는가.** `on_chat_model_start`/`on_llm_start` → `on_llm_end`/`on_llm_error`
한 쌍이 실제 provider 호출 1회다. chain callback은 구현하지 않으므로 세지 않는다.
run_id로 한 번만 종료 처리한다. callback 내부 오류는 `raise_error=False`와 자체
try/except로 흡수되어 모델 결과에 영향이 없다.

**usage.** `usage.extract_usage`가 `AIMessage.usage_metadata` →
`response_metadata`(Google 원형·OpenAI형) → `generation_info` → `llm_output` 순서로
**첫 번째 한 곳만** 읽는다. 키 이름이 겹치는 형식(`total_tokens`는 LangChain형과
OpenAI형에 모두 있음) 때문에, 한 위치 안에서는 가장 많은 키가 일치하는 형식을
고른다 — 이 규칙은 테스트가 실제 결함을 잡아 추가했다(첫 구현은 OpenAI형에서
`total_tokens`만 읽었다). 설치된 langchain-google-genai 4.2.1은 Gemini usage를 표준
`usage_metadata`로 옮겨 담는 것을 소스로 확인했다. streaming 호출은 최종 결과만
본다(테스트: stream 호출의 usage 11 토큰이 11로 한 번만 합산됨).

**`attempt_count`** 규칙은 "우리 코드가 수행한 시도 수, 보이지 않으면 null, 확실히
없으면 0"으로 전 계측에 일관 적용했다. LLM은 Gemini SDK 내부 재시도가 보이지 않아
항상 `null`이다.

---

## 6. 기존 동작을 보존한 방법

- **반환값 무변경**: span은 값을 감싸지 않는다. 필요한 크기는 이미 받은 값의
  `len()`으로만 잰다.
- **예외 무변경**: span과 `request_scope`는 원래 예외 객체를 그대로 재발생시킨다
  (테스트가 `assertIs`로 확인). 새 `try/except`는 추가하지 않았고, 기존에 흡수하던
  예외는 기존대로 흡수하며 클래스 이름만 annotate한다.
- **재실행 없음**: Neo4j·LLM·HTTP를 추가로 부르지 않는다(테스트: row source 호출
  1회, vector DB 호출 1회, embedding POST 1회).
- **정책 무변경**: 폴백 조건, 오류 분류, 검색 순서, 안전 검증, 재시도 정책을 바꾸지
  않았다. agent.py는 기존 정책 본체를 이름만 바꿔 감쌌다.
- **source 계약 보존**: 이전 작업들이 파일 내용으로 고정한 정적 테스트(예:
  `gather_graphrag_evidence(` 호출문의 정확한 들여쓰기, `assemble_final_answer(` →
  `hist.add_ai_message(output)` 순서)를 깨지 않도록 파이프라인 본체를 같은 들여쓰기의
  내부 함수로 옮기고, 근거 수집 구간은 수동 span으로 쟀다.
- **격리**: sink·callback·직렬화 실패는 내부 logger 경고(종류당 5회 제한)로만 남는다.

---

## 7. 실행한 검증과 결과

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'

# 작업 전 기준선
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools chatbot tests   # exit 0
python -m unittest discover -s tests -p 'test_*.py'                                                  # Ran 581 tests / OK

# 최종
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools chatbot tests   # exit 0
python -m unittest discover -s tests -p 'test_*.py'                                                  # Ran 681 tests / OK
python tests/observability_baseline.py                                                                # 기준선 시나리오 8종 재현
```

각 단계(core → context → callback → 목적 태깅 → 파이프라인·검색 → embedding·Neo4j·
authority → 요청 종료 이벤트)를 마칠 때마다 전체 스위트를 실행했고 매번 통과했다.

### 테스트 수 변경: 581 → 681 (+100)

| 파일 | 수 | 검증 내용 |
|---|---:|---|
| `test_observability_events.py` | 26 | 공통 필드·JSON, 알 수 없는 필드/객체 생략, 금지 필드 부재, enum 정규화, null과 0 구분, span 성공/오류(원래 예외 재발생), 시도 횟수, sink 실패 격리, NullSink, MemorySink 순서, LoggingSink가 root를 건드리지 않음, 환경변수 설정, core가 Streamlit·SDK를 import하지 않음 |
| `test_observability_context.py` | 13 | 요청별 새 request_id, 하위 함수가 같은 문맥, 종료·예외 후 reset, 중첩 소유권, outcome 매핑, route 갱신 시 request_id 유지, correlation id 연결, 요청 간·스레드 간 누계 격리 |
| `test_observability_callbacks.py` | 21 | 모델 1회 = 이벤트 1건, chain 미집계, purpose 정규화, **GraphCypherQAChain 두 LLM 분리**, 오류 기록과 전파, streaming 1회 집계, prompt/출력 비기록, 중복 종료 안전, callback 실패 격리, usage 4개 형식·부분값·부재·중복 방지·잘못된 값 |
| `test_observability_pipelines.py` | 35 | §14 기준선 시나리오 전부, Neo4j 안전 거부·전송 재시도 횟수·운영용 subclass 변형·순위 질의, embedding 재시도·거부·문서 인덱싱, authority cache hit/miss·실패 미캐시·HTTP 크기만 기록, **agent.py 운영 배선과 폴백**(서브프로세스) |
| `test_observability_privacy.py` | 5 | 질문·이력·Cypher·row·벡터 문서·LLM 출력·예외 메시지·URL·외부 ID·응답 body marker 누출 0, 금지 키 부재, 모든 키가 스키마 소속 |

**실제 API·인터넷·Neo4j 없이** 실행된다. 통합 테스트는 운영 코드 경로를 그대로
타고, 가장 바깥 I/O만 바꾼다: 대본을 따르는 가짜 chat model, 메모리 Neo4j 결과,
가짜 embedding HTTP session. `agent.py`는 import 시 실제 Gemini·Neo4j 클라이언트를
만들기 때문에, 폴백·배선 테스트는 가짜 `llm`/`graph` 모듈을 먼저 넣은 **별도
서브프로세스**에서 실제 `agent.py`·`tools/cypher.py`·`text_rag.py`를 import해 실행한다.

---

## 8. 기준선 시나리오 결과 (`python tests/observability_baseline.py`)

토큰 수는 fixture에 적어 둔 값이며 실제 사용량이 아니다(실서비스 수치는 만들지 않음).

| 시나리오 | 기대 | 실측 결과 |
|---|---|---|
| 정상 GraphRAG | Cypher 1, Graph QA 1, 합성 1 | outcome `success`, LLM 3회 = `cypher_generation 1 · graph_qa 1 · final_synthesis 1`, embedding 1, 인용 5 |
| graph empty + vector success | status 분리, 합성 실행 | graph `empty` / vector `success`, LLM 3회(Graph QA는 빈 context로도 호출됨), 합성 실행 |
| 모든 검색 실패 | short circuit, 합성 0 | outcome `short_circuit`, LLM 0회, embedding 0, 인용 0, 기존 안내문 그대로 |
| 정상 VectorRAG | embedding·vector query·답변 분리 | `text_rag_answer 1`, embedding 1, neo4j `vector_query` 1, 인용 5 |
| transient 오류 → ReAct 성공 | 동일 request_id, fallback success | outcome `fallback_success`, `react_iteration 1`, 요청 내 request_id 종류 1개, route `react_fallback` |
| provider usage 없음 | token null | LLM 3회, `usage_available_call_count=0`, 토큰 `null` |
| authority cache hit | HTTP 0, cache hit true | miss: `http_fetched=true, attempt 1` / hit: `cache_hit=true, http_fetched=false, attempt 0`, 실제 fetch 1회 |
| 관측 sink 실패 | 사용자 결과 불변 | 답변이 관측 off 실행과 동일 |

---

## 9. 발견했지만 범위 밖이라 수정하지 않은 것 (§1.3)

1. **운영 체인이 생성 Cypher와 DB 결과 원문을 stdout에 출력한다.**
   - 위치: `tools/cypher.py` → `build_graph_cypher_chain(..., verbose=True)`(이번에 값
     그대로 유지), `chatbot/legacy/react_agent.py`의 `AgentExecutor(verbose=True)`.
   - 현상: LangChain `verbose=True`가 stdout handler를 붙여 "Generated Cypher:"와
     "Full Context:"(Neo4j 행 전체)를 출력한다. 테스트로 재현했다(marker가 stdout에 출력됨).
   - 영향: 서버 콘솔·로그 수집기에 질문에서 파생된 Cypher와 DB 레코드가 남는다.
     사용자 화면 노출은 없다. 관측 이벤트와는 무관하다.
   - 권장: 별도 작업에서 `verbose=False`로 전환(stdout 출력량이 바뀌므로 이번
     범위에서 제외).
2. **기존 서버 로그가 예외 메시지 원문을 남긴다.**
   - 위치: `chatbot/retrieval/graph_query.py`(`graph retrieval failed … %s: %s`),
     `chatbot/application/retriever_invocation.py`, `tools/cypher_safety.py`.
   - 영향: provider 응답이나 query 조각이 예외 메시지에 들어 있으면 로그에 남는다.
     관측 이벤트에는 클래스 이름만 들어가도록 했다(§6.3).
   - 권장: 로그 정책 정비 시 메시지를 클래스 이름 + correlation id로 줄이는 방안 검토.
3. **기존 테스트 일부가 실제 secrets와 Neo4j 연결에 의존한다.**
   - 위치: `tests/test_phase3_fallback_policy.py`가 `agent`를 직접 import →
     `graph.py`가 `st.secrets`로 `Neo4jGraph`를 생성(생성자가 연결·스키마 조회).
   - 영향: secrets·Neo4j가 없는 CI에서는 실패할 수 있다. 이번 기준선은 로컬
     환경에서 통과했다.
   - 권장: 신규 `test_observability_pipelines.py`처럼 가짜 `llm`/`graph` 모듈을
     먼저 주입하는 방식으로 전환.
4. **langchain-google-genai가 빠진 토큰 수를 0으로 채운다** (`prompt_token_count or 0`).
   provider 통합의 동작이며 그대로 기록한다. 0과 "미보고"를 구분할 수 없는 경우가 있다.
5. **`agent.poetry_chat`은 여전히 어디서도 호출되지 않는다**(이전 보고서에서 기록).

---

## 10. 미해결 사항·알려진 제약

- LLM `attempt_count`는 항상 `null`(Gemini SDK 내부 재시도 비관찰).
- Neo4jVector와 대화 이력의 드라이버 수준 재시도는 비관찰(`null`).
- vectorRAG·ReAct 경로의 대화 이력 읽기/쓰기는 `RunnableWithMessageHistory` 내부라
  별도 이벤트가 없다. graphRAG 정상 경로의 `history_read`에는 이력 핸들 생성 시
  세션 노드 준비가 포함된다.
- 생성 Cypher의 `row_count`는 `top_k`(10)로 자르기 전의 DB 행 수다.
- `general_chat` route는 현재 도달하지 않는다(§2).
- 실제 provider·Neo4j 환경에서의 live 측정은 수행하지 않았다(자격 증명·네트워크를
  쓰는 검증은 이번 범위 밖). 구조와 결정적 테스트로 측정 가능성을 확인했다.
- 테스트 fixture는 `llm` 모듈을 reload한다(기존 `test_phase5_embeddings.py`와 같은
  방식). 모듈 상태에 결합된 기술 부채다.

---

## 11. 다음 단계(Graph QA 호출 제거)에서 쓸 핵심 지표

| 지표 | 출처 | 기대 변화 |
|---|---|---|
| `llm_calls_by_purpose.graph_qa` | `request.completed` | 요청당 1 → 0 |
| `llm_call_count` (graphrag) | `request.completed` | 3 → 2 |
| Graph QA 토큰 비중 | `llm.completed`(purpose=graph_qa)의 `total_tokens` ÷ 전체 | 제거 전 비용 근거 |
| 요청당 입력/출력 토큰 | `request.completed.input_tokens/output_tokens` | 감소 |
| graph 검색 지연 | `retrieval.graph.completed.duration_ms` p50/p95/p99 | Graph QA LLM 지연만큼 감소 |
| 전체 지연 | `request.completed.duration_ms` p50/p95/p99 | 감소 |
| 답변·인용 회귀 | `answer_chars`, `citation_count` + 기존 회귀 테스트 | 불변이어야 함 |
| 오류율 | `request.completed.outcome` | 불변이어야 함 |
| usage 제공률 | `usage_available_call_count ÷ llm_call_count` | 비교의 신뢰도 확인 |

집계 방법과 로컬 확인 명령은 `docs/OBSERVABILITY.md` §10–§11에 있다.

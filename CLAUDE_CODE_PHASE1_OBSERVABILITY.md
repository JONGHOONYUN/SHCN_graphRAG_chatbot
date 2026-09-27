# Claude Code 작업 지시서: Phase 1 관측성 도입

## 0. 문서의 목적

이 문서는 현재 시화총림 챗봇 코드에 **Phase 1 관측성(observability)** 을 도입하기 위한 Claude Code용 구현 지시서다.

이번 작업의 핵심 목적은 다음 단계인 **불필요한 Graph QA LLM 호출 제거** 전후를 객관적으로 비교할 수 있는 기준선을 만드는 것이다. 따라서 이번 단계에서는 시스템 동작을 최적화하거나 호출 수를 줄이지 않는다. 현재 동작을 보존한 채 요청 단위의 지연시간, LLM 호출 목적과 횟수, 토큰 사용량, 검색 결과, 폴백 여부를 안전하게 기록할 수 있어야 한다.

이 문서는 독립 실행 가능한 작업 지시서다. 구현자는 과거 대화나 별도 설명에 의존하지 말고, 이 문서와 저장소의 실제 코드를 함께 확인하여 작업한다.

---

## 1. 작업 전 반드시 지켜야 할 원칙

### 1.1 이번 작업에서 변경할 수 있는 것

- 요청 단위 실행 문맥과 `request_id`
- 구조화된 관측 이벤트 모델
- 실행 구간(span) 시간 측정
- LLM/임베딩/Neo4j/외부 전거 호출 계측
- LangChain callback 기반의 실제 모델 호출 계측
- 테스트용 인메모리 관측 sink
- JSON 구조화 로그 sink
- 관측성 관련 테스트와 문서
- 기존 코드에 동작 중립적인 계측 훅 추가

### 1.2 이번 작업에서 절대 변경하지 말아야 할 것

- `GraphCypherQAChain` 제거 또는 대체
- Graph QA LLM 호출 제거
- `return_direct` 전환
- 프롬프트의 내용 또는 역할
- LLM 모델, temperature, retry, timeout 설정
- 검색 순서, 검색 병렬화, 검색기 선택 규칙
- GraphRAG/VectorRAG 라우팅 정책
- 검색 결과 정렬, top-k, score 계산 방식
- evidence 합성 규칙
- 답변 형식, 인용 형식, 사용자 화면
- 일반 대화 및 ReAct 폴백 정책
- Neo4j 노드·관계·인덱스·데이터
- 용어집 또는 연구논문 데이터 모델
- AWS 배포 구조
- 외부 모니터링 서비스나 대시보드 구축
- 기존 공개 함수의 반환값과 예외 의미

이번 Phase 1은 **측정 단계**다. 관측 결과를 근거로 수행할 최적화는 별도 Phase에서 진행한다.

### 1.3 작업 중 발견한 별도 문제의 처리

작업 중 결함이나 개선점을 발견하더라도 이번 목적과 직접 관계가 없다면 수정하지 않는다. 다음 내용을 구현 보고서에 기록한다.

- 발견 위치
- 현상
- 영향
- 권장 후속 작업

보안상 즉시 조치가 필요하거나 관측성 구현 자체를 막는 문제라면 임의로 우회하지 말고 작업을 중지하고 보고한다.

---

## 2. 현재 코드 기준선

구현 전에 반드시 실제 파일을 다시 확인한다. 아래 내용은 작업 범위를 이해하기 위한 현재 구조의 기준이며, 저장소와 차이가 있다면 실제 코드를 우선하되 그 차이를 구현 보고서에 남긴다.

### 2.1 주요 애플리케이션 경로

- `bot.py`
  - Streamlit 요청 진입점과 `handle_submit`
  - 인증 이후 실제 챗봇 백엔드를 호출하는 경계
- `agent.py`
  - GraphRAG 정상 경로와 일반 대화/ReAct 폴백 조정
  - 최종 synthesis chain 구성
- `text_rag.py`
  - VectorRAG 애플리케이션 조립과 실행
- `chatbot/application/graphrag_pipeline.py`
  - 대화 이력 조회
  - evidence 수집
  - 최종 합성 LLM 호출
  - 인용과 렌더링
  - 대화 이력 저장
- `chatbot/application/evidence_orchestrator.py`
  - 그래프 검색, 벡터 검색, 외부 전거 검색 조정
- `chatbot/application/retriever_invocation.py`
  - 검색기 시그니처 호환과 오류 격리
- `chatbot/application/vectorrag_pipeline.py`
  - VectorRAG 실행 경로

### 2.2 주요 인프라 및 검색 경로

- `chatbot/retrieval/graph_query.py`
  - 구조화 Graph QA chain 호출과 그래프 evidence 변환
- `tools/cypher.py`
  - `GraphCypherQAChain`과 graph retrieval 의존성 조립
- `tools/vector.py`
  - 벡터 검색 조립 및 관련 검색 코드
- `llm.py`
  - 공용 Chat LLM과 Google embedding 구성
- `chatbot/authority/client.py`
  - 외부 전거 HTTP 호출
- `chatbot/authority/service.py`
  - 외부 전거 캐시 및 서비스 조정
- `graph.py`
  - Neo4j 연결 구성
- `errors.py`
  - 기존 예외 및 `correlation_id`

### 2.3 현재 예상되는 GraphRAG 모델 호출

일반적인 GraphRAG 요청은 구현 상태에 따라 최소 다음 호출을 포함할 수 있다.

1. Cypher 생성 LLM
2. 생성된 Cypher를 이용한 Neo4j 조회
3. `GraphCypherQAChain` 내부 Graph QA LLM
4. 벡터 검색용 embedding
5. Neo4j vector query
6. 최종 evidence synthesis LLM

특히 3번 Graph QA의 자연어 결과가 최종 답변에 실질적으로 사용되지 않는지 여부가 후속 최적화의 핵심 가설이다. 이번 단계에서는 그 호출을 제거하지 말고, **실제 호출 사실과 비용을 별도의 목적값으로 구분하여 측정**해야 한다.

### 2.4 테스트 기준선

현재 모듈 분리 구현 보고서에는 전체 테스트가 다음 상태로 기록되어 있다.

- 기존 테스트: 551개
- 모듈 분리 관련 신규 테스트: 30개
- 합계: 581개 통과

구현 시작 시 실제 전체 테스트 수와 결과를 다시 측정하고 기록한다. 숫자가 달라졌다면 현재 저장소 결과를 기준선으로 사용하되 원인을 추정하지 말고 사실만 보고한다.

---

## 3. Phase 1 목표

작업 완료 후 다음 질문에 로그와 테스트로 답할 수 있어야 한다.

1. 하나의 사용자 요청이 어느 실행 경로를 통과했는가?
2. 전체 응답 시간과 각 주요 단계의 지연시간은 얼마인가?
3. 실제 LLM provider 호출은 몇 번 발생했는가?
4. 각 LLM 호출의 목적은 무엇인가?
5. provider가 제공한 입력·출력·합계 토큰은 얼마인가?
6. Graph QA 호출이 정상 GraphRAG 요청마다 발생하는가?
7. 그래프, 벡터, 외부 전거 검색은 각각 성공·빈 결과·실패 중 무엇이었는가?
8. Neo4j와 외부 HTTP 호출의 지연 및 재시도 횟수는 얼마인가?
9. 최종 synthesis가 실행되었는가, 또는 evidence 부재로 생략되었는가?
10. 정상 GraphRAG가 실패하여 일반 대화/ReAct 폴백으로 전환되었는가?
11. 사용자 답변과 인용 수, evidence 크기는 어느 정도인가?
12. 관측 기능을 껐거나 관측 코드 자체가 실패해도 기존 답변 동작이 동일한가?

---

## 4. 범위와 완료 산출물

### 4.1 필수 구현 범위

- 관측성 전용 패키지 추가
- 요청 문맥과 `request_id` 전파
- 구조화 이벤트 및 span API
- `NullSink`, JSON logging sink, 테스트용 memory sink
- LLM callback 계측
- GraphRAG 및 VectorRAG 파이프라인 계측
- graph/vector/embedding/Neo4j/외부 전거 호출 계측
- ReAct 또는 일반 대화 폴백 계측
- 토큰 usage의 안전한 추출
- 민감정보 비기록 보장
- 단위·통합·회귀 테스트
- 운영 및 해석 문서
- 구현 결과 보고서

### 4.2 필수 파일 산출물

실제 구조에 맞춰 파일명은 소폭 조정할 수 있지만 책임은 아래처럼 분리한다.

```text
chatbot/
  observability/
    __init__.py
    context.py       # 요청 문맥과 ContextVar 수명주기
    events.py        # 이벤트 스키마, enum 또는 상수, 직렬화
    sinks.py         # Sink protocol, Null/Logging/Memory sink
    telemetry.py     # span/event 기록용 공개 facade
    callbacks.py     # LangChain LLM/ChatModel callback 계측
    usage.py         # provider usage metadata 정규화
```

추가 문서:

```text
docs/OBSERVABILITY.md
OBSERVABILITY_PHASE1_IMPLEMENTATION_NOTE.md
```

테스트는 기존 테스트 배치 규칙을 따른다. 최소한 다음 책임을 가진 테스트 파일이 있어야 한다.

```text
tests/test_observability_context.py
tests/test_observability_events.py
tests/test_observability_callbacks.py
tests/test_observability_pipelines.py
tests/test_observability_privacy.py
```

기존 저장소가 다른 테스트 디렉터리 규칙을 사용하면 그 규칙에 맞추되 동일한 검증 범위를 유지한다.

### 4.3 의존성 원칙

Phase 1에서는 새로운 외부 관측성 SDK를 필수 의존성으로 추가하지 않는다.

- Python 표준 라이브러리의 `logging`, `contextvars`, `dataclasses`, `time`, `datetime`, `uuid` 등을 우선 사용한다.
- OpenTelemetry, Prometheus client, CloudWatch SDK, Datadog, Sentry 등의 설치는 이번 범위가 아니다.
- 기존 LangChain callback API는 현재 설치된 버전의 공개 API만 사용한다.
- 새 라이브러리가 정말 필요하다고 판단되면 설치하지 말고 사유와 대안을 먼저 보고한다.

---

## 5. 설계 원칙

### 5.1 동작 중립성

관측 코드는 기존 실행 결과를 바꾸지 않아야 한다.

- 함수 반환값을 감싸거나 변환하지 않는다.
- 예외를 삼키거나 다른 예외로 바꾸지 않는다.
- 기존 retry와 fallback 조건을 변경하지 않는다.
- 계측을 위해 같은 작업을 한 번 더 실행하지 않는다.
- 토큰 측정을 위해 별도 LLM 호출을 하지 않는다.
- row count 측정을 위해 Neo4j 쿼리를 재실행하지 않는다.

### 5.2 관측 실패 격리

관측성은 답변 생성보다 우선할 수 없다.

- sink 오류는 사용자 요청을 실패시키지 않는다.
- callback의 usage 파싱 실패는 원래 LLM 결과를 손상시키지 않는다.
- 이벤트 직렬화 실패는 애플리케이션 예외로 전파하지 않는다.
- 관측 실패는 가능하면 최소한의 내부 logger 경고로 남기되 재귀 로그를 만들지 않는다.
- 모든 관측 함수는 비활성 상태에서 저비용 no-op으로 작동해야 한다.

### 5.3 정확한 시간 측정

- 구간 시간은 `time.perf_counter()`로 측정한다.
- 외부 표시용 시각은 timezone-aware UTC timestamp로 기록한다.
- `duration_ms`는 0 이상의 숫자여야 한다.
- wall clock 차이로 구간 시간을 계산하지 않는다.

### 5.4 낮은 결합도

- 도메인 및 application 모듈이 특정 로깅 서비스 SDK에 의존하지 않게 한다.
- 관측성 호출부는 작은 facade 또는 주입된 protocol에만 의존한다.
- Streamlit을 관측성 core에서 import하지 않는다.
- `request_id` 전파 때문에 모든 공개 함수 시그니처를 연쇄 변경하지 않는다.
- 동기 호출 경로에는 `ContextVar`를 우선 사용한다.

### 5.5 낮은 카디널리티

집계 가능한 필드는 제한된 값만 가져야 한다.

- 허용 예: `mode=graphrag|vectorrag`, `status=success|empty|error|skipped`
- 금지 예: 사용자 질문, URL, Neo4j node ID, 생성 Cypher를 metric label로 사용
- `request_id`는 로그 상관관계 확인용이며 metric label로 사용하지 않는다.

---

## 6. 개인정보 및 민감정보 정책

### 6.1 어떠한 이벤트에도 기록하지 말아야 할 데이터

- 사용자 질문 원문
- 사용자 답변 원문
- 대화 이력 원문
- system/user/assistant prompt 원문
- Neo4j query/Cypher 원문
- Neo4j record 전체 내용
- evidence 원문 또는 문서 문장
- 외부 전거 응답 body
- 전체 URL 또는 외부 식별자
- API key, access token, cookie, password
- 인증 사용자명과 비밀번호
- embedding vector 값
- 예외에 포함된 request payload 전문
- Streamlit session state 전문

### 6.2 기록할 수 있는 파생값

- 문자열 길이(`*_chars`)
- 항목 수(`*_count`)
- 제한된 enum 값
- provider가 직접 반환한 token usage
- 결과 row 수
- citation 수
- embedding 차원 수
- HTTP status class 또는 상태 enum
- 캐시 hit 여부
- 언어 코드
- 오류 class 이름
- 기존 예외의 독립적인 `correlation_id`

원문을 hash하면 안전하다고 가정하지 않는다. Phase 1에서는 질문, 세션 ID, Neo4j ID, URL의 hash도 기본적으로 기록하지 않는다.

### 6.3 오류 기록 제한

- `error_type`에는 예외 class 이름만 기록한다.
- `error_message`는 기본 이벤트 스키마에 포함하지 않는다.
- 기존 logger가 예외 메시지를 남기는 동작은 이번 작업에서 대규모 변경하지 않되, 새 관측 이벤트로 복제하지 않는다.
- stack trace는 기존 오류 로깅 정책을 따르며 구조화 metric event에는 넣지 않는다.

---

## 7. 요청 문맥 설계

### 7.1 RequestContext 필수 필드

요청 문맥은 최소 다음 값을 제공한다.

| 필드 | 설명 | 예시 |
|---|---|---|
| `request_id` | 요청마다 새로 생성되는 UUID 계열 문자열 | `c1...` |
| `mode` | 사용자 선택 실행 모드 | `graphrag`, `vectorrag` |
| `route` | 실제 실행 경로 | `graphrag`, `vectorrag`, `general_chat`, `react_fallback` |
| `question_language` | 감지된 질문 언어 | `ko`, `en`, `unknown` |
| `response_language` | 응답에 사용된 언어 | `ko`, `en`, `unknown` |
| `started_at` | UTC 요청 시작 시각 | ISO-8601 |

처음부터 알 수 없는 값은 `unknown`으로 두고 문맥 업데이트 API로 보완할 수 있게 한다.

### 7.2 생성 위치와 수명주기

- 인증이 완료되고 실제 챗봇 백엔드 호출을 시작하기 직전에 `request_id`를 생성한다.
- 권장 위치는 `bot.py`의 `handle_submit` 내부 백엔드 dispatch 직전이다.
- 하나의 사용자 질문에 GraphRAG에서 폴백까지 발생해도 같은 `request_id`를 유지한다.
- 요청 종료 시 `ContextVar` token을 반드시 reset한다.
- 연속 요청 사이에 문맥이 누출되지 않아야 한다.
- 테스트와 CLI처럼 Streamlit을 거치지 않는 진입점은 application pipeline에서 문맥이 없을 때 임시 root context를 생성할 수 있다.
- 중첩 진입 시 기존 문맥을 덮어쓰거나 조기 종료하지 않도록 ownership을 구분한다.

### 7.3 기존 correlation ID와의 관계

기존 오류의 `correlation_id`는 유지한다.

- `request_id`: 전체 사용자 요청을 묶는 식별자
- `correlation_id`: 특정 오류나 기존 오류 처리 흐름의 식별자

오류 이벤트에는 둘 다 존재할 수 있다. 기존 `correlation_id`를 `request_id`로 대체하지 않는다.

---

## 8. 이벤트 스키마

### 8.1 공통 필드

모든 이벤트는 JSON 직렬화가 가능해야 하며 최소 다음 공통 필드를 갖는다.

```json
{
  "schema_version": 1,
  "timestamp": "2026-09-27T12:34:56.789Z",
  "event_name": "llm.completed",
  "request_id": "...",
  "mode": "graphrag",
  "route": "graphrag",
  "status": "success",
  "duration_ms": 312.4
}
```

규칙:

- `schema_version`은 정수 `1`로 시작한다.
- 필드명은 snake_case를 사용한다.
- 존재하지 않는 값을 거짓 `0`으로 만들지 않는다.
- 알 수 없는 token usage는 `null`로 기록하고 `usage_available=false`로 표시한다.
- JSON에 객체의 `repr`, prompt, 메시지 객체를 넣지 않는다.

### 8.2 필수 이벤트 목록

아래 이벤트명은 안정된 공개 관측 계약으로 취급한다.

#### 요청

- `request.started`
- `request.completed`

`request.completed` 필수 추가 필드:

- `outcome`: `success`, `error`, `short_circuit`, `fallback_success`, `fallback_error`
- `duration_ms`
- `llm_call_count`
- `embedding_call_count`
- `fallback_used`
- `citation_count`
- `answer_chars`

요청이 예외로 종료되어도 가능한 한 정확히 한 번 기록한다.

#### 파이프라인 span

- `history.read.completed`
- `evidence.gather.completed`
- `evidence.format.completed`
- `synthesis.completed`
- `answer.render.completed`
- `history.write.completed`
- `pipeline.graphrag.completed`
- `pipeline.vectorrag.completed`
- `fallback.completed`

#### 검색

- `retrieval.graph.completed`
- `retrieval.vector.completed`
- `retrieval.authority.completed`
- `retrieval.authority_source.completed`

검색 이벤트 필수 필드:

- `retriever`
- `status`: `success`, `empty`, `error`, `skipped`
- `result_count`
- `attempt_count`
- `duration_ms`
- 오류 시 `error_type`

#### Neo4j

- `neo4j.query.completed`

필수 추가 필드:

- `operation`: `generated_graph_query`, `vector_query`, `history_read`, `history_write`, `deterministic_lookup`, `other`
- `query_origin`: `generated`, `deterministic`, `framework`, `unknown`
- `status`
- `row_count` 또는 알 수 없는 경우 `null`
- `attempt_count`
- `safety_result`: `passed`, `rejected`, `not_applicable`, `unknown`
- `duration_ms`

생성 Cypher 원문은 절대로 기록하지 않는다.

#### Embedding

- `embedding.completed`

필수 추가 필드:

- `purpose`: `vector_query`, `document_indexing`, `other`
- `model`
- `input_count`
- `input_chars`
- `dimension`
- `attempt_count`
- `status`
- `duration_ms`

vector 값과 입력 원문은 기록하지 않는다.

#### LLM

- `llm.completed`

필수 추가 필드:

- `purpose`
- `provider`
- `model`
- `status`
- `attempt_count`
- `duration_ms`
- `usage_available`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `input_chars` 또는 알 수 없는 경우 `null`
- `output_chars` 또는 알 수 없는 경우 `null`
- 오류 시 `error_type`

`purpose` 허용값은 최소 다음을 포함한다.

- `cypher_generation`
- `graph_qa`
- `final_synthesis`
- `text_rag_answer`
- `general_chat`
- `react_iteration`
- `language_detection`
- `other`

`input_chars`는 비용 계산용 토큰 추정값이 아니다. provider usage가 없을 때 문자 수를 토큰으로 환산하지 않는다.

#### 관측 내부 오류

- `observability.error`

이 이벤트는 재귀적으로 동일 sink 실패를 호출하면 안 된다. 안전한 fallback logger에서 제한적으로 사용한다.

### 8.3 카운터 집계 방식

요청 문맥 내부에 다음 누계를 둘 수 있다.

- LLM provider call count
- 목적별 LLM call count
- provider가 제공한 token 합계
- embedding call count
- retriever 결과 상태
- fallback 사용 여부
- citation 수

중요: chain 시작 이벤트를 LLM 호출로 세지 않는다. 실제 `ChatModel` 또는 `LLM` provider 종료 이벤트만 LLM 호출 수에 포함하여 중복 집계를 방지한다.

---

## 9. Sink와 설정

### 9.1 Sink protocol

관측 이벤트 출력부를 작은 protocol로 추상화한다.

개념적 형태:

```python
class EventSink(Protocol):
    def emit(self, event: Mapping[str, object]) -> None:
        ...
```

필수 구현:

- `NullSink`: 아무것도 기록하지 않는 기본 또는 비활성 sink
- `LoggingSink`: 표준 `logging`으로 한 줄 JSON 출력
- `MemorySink`: 테스트에서 이벤트 검증

### 9.2 설정 방식

저장소의 기존 환경설정 방식을 확인해 일관되게 구현한다. 최소 다음 설정을 지원한다.

- 관측성 활성/비활성
- sink 선택 또는 명시적 sink 주입
- 로그 레벨

요구사항:

- import 시 전역 logging handler를 강제로 재설정하지 않는다.
- 사용자 애플리케이션의 root logger 설정을 덮어쓰지 않는다.
- 기본값 때문에 테스트 출력이 대량 오염되지 않아야 한다.
- 테스트에서는 환경변수보다 명시적 sink 주입 또는 context override를 우선한다.

### 9.3 이벤트 크기 제한

- 동적 리스트나 전체 결과를 이벤트에 넣지 않는다.
- 목적별 호출 수는 제한된 known key만 사용한다.
- 임의 metadata를 그대로 병합하는 API를 만들지 않는다.
- 알 수 없는 객체는 `str()` 또는 `repr()`로 직렬화하지 말고 필드를 생략한다.

---

## 10. LangChain 및 LLM 계측

### 10.1 실제 provider 호출 기준

LLM 호출 측정은 단순히 `.invoke()`가 호출된 횟수로 추정하지 않는다. LangChain callback의 ChatModel/LLM lifecycle을 사용하여 실제 provider 호출을 기록한다.

다음 상황을 구분해야 한다.

- chain 실행 시작/종료
- 실제 ChatModel 또는 LLM 호출 시작/종료
- 내부 chain이 호출하는 하위 모델
- retry로 인한 provider 재시도
- 최종 parser 실행

`llm_call_count`에는 실제 모델 호출만 포함한다.

### 10.2 목적 분류

다음 호출을 반드시 구분한다.

| 실행 위치 | `purpose` |
|---|---|
| GraphCypherQAChain의 Cypher 생성 모델 | `cypher_generation` |
| GraphCypherQAChain의 그래프 답변 생성 모델 | `graph_qa` |
| GraphRAG 최종 evidence 합성 | `final_synthesis` |
| VectorRAG 최종 답변 생성 | `text_rag_answer` |
| 일반 대화 | `general_chat` |
| ReAct loop의 각 모델 호출 | `react_iteration` |

목적을 판별하기 위해 prompt 내용을 읽거나 저장하지 않는다.

권장 순서:

1. runnable `run_name`, tag 또는 metadata로 목적을 명시한다.
2. callback에서 제한된 목적값만 읽는다.
3. `GraphCypherQAChain` 내부의 generation chain과 QA chain에 서로 다른 안정된 run name/tag를 부여한다.
4. 현재 LangChain 버전에서 tag 전파가 예상과 다른지 테스트로 검증한다.

내부 chain을 구분하기 위해 private attribute에 의존하지 않는다. 공개 속성 또는 생성 시 주입 가능한 chain 구성을 사용한다. 불가피하다면 구현을 진행하지 말고 정확한 제약을 보고한다.

### 10.3 Graph QA 계측의 필수 결과

정상 GraphRAG 통합 테스트에서 다음 목적별 모델 이벤트가 각각 구분되어야 한다.

```text
cypher_generation = 1
graph_qa = 1
final_synthesis = 1
```

호출 수가 데이터나 분기 때문에 달라질 수 있는 테스트라면 그 이유가 명확한 fixture를 사용한다. 적어도 한 개의 결정적 테스트에서 위 세 호출이 분리되어야 한다.

이 요구사항을 충족하지 못한 채 전체 Graph QA chain을 하나의 `graph_retrieval` 호출로만 기록하면 Phase 1은 완료된 것이 아니다.

### 10.4 토큰 usage

provider가 반환하는 공식 metadata만 기록한다.

현재 설치된 LangChain 및 Google provider 객체에서 실제로 나타날 수 있는 다음 위치를 안전하게 확인한다.

- `AIMessage.usage_metadata`
- `AIMessage.response_metadata`
- `LLMResult.llm_output`
- generation message metadata

키 이름 차이를 정규화하되 다음 원칙을 지킨다.

- 입력, 출력, 합계 토큰을 가능한 경우 각각 기록한다.
- 일부 값만 제공되면 제공된 값만 기록한다.
- metadata가 없으면 모두 `null`, `usage_available=false`다.
- 문자 수로 토큰을 추정하지 않는다.
- 동일 usage를 parent/child callback에서 중복 합산하지 않는다.
- streaming chunk usage를 중복 합산하지 않는다.
- 테스트 fixture로 최소 2개 이상의 provider metadata 형태와 usage 부재를 검증한다.

### 10.5 retry 횟수

callback만으로 provider 내부 retry 횟수를 정확히 알 수 없으면 임의로 추정하지 않는다.

- 정확히 확인 가능한 경우에만 실제 횟수를 기록한다.
- 확인 불가능하면 `attempt_count=null` 또는 명시된 unknown 값을 사용한다.
- 단순 성공을 `attempt_count=1`로 둘지는 API 의미를 문서화하고 모든 계측에서 일관되게 적용한다.

---

## 11. 코드별 계측 지점

구현 전에 각 파일의 호출 흐름을 다시 추적한 후 최소 변경으로 아래 경계를 계측한다.

### 11.1 `bot.py`

`handle_submit`에서 실제 backend dispatch 직전에 root request context를 시작한다.

기록할 내용:

- `request.started`
- 선택된 mode
- backend route
- 최종 `request.completed`
- 전체 지연시간
- 성공/오류/폴백 outcome
- 누적 LLM/embedding 호출 수
- 최종 답변 문자 수와 citation 수

주의:

- 비밀번호 입력과 로그인 시도는 이번 챗봇 관측 범위가 아니다.
- 단순 언어 전환 control command가 LLM을 호출하지 않는 기존 동작은 유지한다.
- UI에 `request_id`를 새로 표시하지 않는다.
- 기존 오류 메시지와 Streamlit 상태 처리를 변경하지 않는다.

### 11.2 `agent.py`

기록할 내용:

- 정상 GraphRAG route
- 일반 대화 route
- transient provider 오류로 인한 ReAct 폴백 시작과 완료
- fallback 사용 여부
- fallback 결과와 원래 오류의 `correlation_id`
- synthesis LLM의 `purpose=final_synthesis`
- 일반 대화 LLM의 `purpose=general_chat`
- ReAct 내부 모델의 `purpose=react_iteration`

주의:

- 폴백 조건을 넓히거나 좁히지 않는다.
- 관측을 위해 예외 catch 범위를 변경하지 않는다.
- 기존 예외를 다른 예외로 포장하지 않는다.

### 11.3 `chatbot/application/graphrag_pipeline.py`

다음 구간을 각각 측정한다.

- history read
- evidence gather 전체
- evidence format
- 최종 synthesis
- citation 생성
- answer render
- history write
- pipeline 전체

추가 기록:

- graph/vector/authority evidence 항목 수
- evidence 포맷 결과 문자 수
- citation 수
- answer 문자 수
- 전체 retrieval 실패에 따른 short circuit 여부
- 최종 synthesis 실행 여부

전체 retrieval이 실패하여 기존 코드가 LLM 호출 없이 오류 응답을 반환하는 경우:

- `outcome=short_circuit`
- `final_synthesis` LLM 호출 수 0
- 기존 사용자 응답은 그대로 유지

### 11.4 `chatbot/application/evidence_orchestrator.py`

각 검색기를 독립적으로 측정한다.

- graph retrieval
- vector retrieval
- authority retrieval 전체
- authority source별 호출

기록할 내용:

- 검색기 이름
- 지연시간
- success/empty/error/skipped
- result count
- 기존 오류 객체의 correlation ID가 있으면 함께 연결

주의:

- 현재 graph → vector → authority 순서를 변경하지 않는다.
- 병렬 실행으로 바꾸지 않는다.
- 반환 dict key와 값 형식을 변경하지 않는다.
- `_safe_retrieve`의 오류 격리 의미를 유지한다.

### 11.5 `chatbot/application/retriever_invocation.py`

- 지원하는 검색기 시그니처 dispatch 결과를 바꾸지 않는다.
- 호출 실패 시 기존 status/error 결과를 유지한다.
- 새로운 요청 ID 때문에 기존 검색기 함수 시그니처를 강제 변경하지 않는다.
- 이 계층과 orchestrator에서 동일 retrieval을 중복 이벤트로 계산하지 않도록 책임을 한 곳에 명확히 둔다.

권장 책임:

- orchestrator: retrieval 전체 span 및 최종 status
- invocation adapter: 필요한 경우 세부 dispatch 정보만 기록하되 기본 event count에는 포함하지 않음

### 11.6 `chatbot/retrieval/graph_query.py` 및 `tools/cypher.py`

반드시 다음을 구분한다.

- Graph retrieval 전체 시간
- Cypher generation LLM
- Cypher safety 검증
- Neo4j generated query
- Graph QA LLM
- graph evidence 변환

중요:

- 현재 `GraphCypherQAChain`을 유지한다.
- `cypher_qa_structured.invoke()`의 결과 구조를 변경하지 않는다.
- Graph QA의 자연어 응답이 사용되지 않더라도 그대로 생성하게 둔다.
- raw Cypher, prompt, intermediate record 내용을 로그로 남기지 않는다.
- intermediate step에서 row count를 이미 알 수 있을 때만 기록하고, 이를 위해 쿼리를 재실행하지 않는다.

GraphCypherQAChain 전체 `.invoke()` 시간만 측정하는 것으로 끝내지 말고 내부 두 LLM 목적을 callback으로 분리한다.

### 11.7 vector retrieval 및 embedding

`tools/vector.py`, 관련 retrieval 모듈, `llm.py`의 실제 호출 경계를 확인한다.

구분할 내용:

- query embedding
- Neo4j vector query
- vector result mapping
- VectorRAG answer LLM

embedding 기록:

- model
- 입력 개수와 총 문자 수
- 결과 차원
- 지연시간
- retry/attempt 정보
- 성공/오류

주의:

- embedding vector는 로그에 넣지 않는다.
- query text를 로그에 넣지 않는다.
- dimension 확인 때문에 vector를 복사하거나 JSON 직렬화하지 않는다.
- 문서 인덱싱용 호출이 같은 wrapper를 사용한다면 `purpose=document_indexing`으로 구분한다.

### 11.8 Neo4j

가능하면 공용 안전 실행 경계에서 DB 호출 시간을 측정하되, 상위 호출부에서 `operation`을 지정할 수 있게 한다.

측정 대상:

- generated graph query
- vector query
- deterministic lookup
- history read/write

주의:

- query 문자열로 operation을 추론하거나 기록하지 않는다.
- 동일 DB 호출을 wrapper와 상위 span에서 모두 `neo4j.query.completed`로 발행하지 않는다.
- row count를 알 수 없는 lazy result는 소비 방식과 기존 반환 동작을 바꾸지 않는다.
- query safety 결과는 기존 검증 함수 결과를 관찰하되 정책을 변경하지 않는다.

### 11.9 외부 전거

`chatbot/authority/client.py`와 `chatbot/authority/service.py`를 나누어 계측한다.

기록할 내용:

- source의 제한된 enum 이름
- node type
- cache hit/miss
- 실제 HTTP fetch 여부
- status: success/empty/error/skipped
- HTTP status code 또는 status class
- response byte/char 크기
- 지연시간

주의:

- 전체 URL, 외부 식별자, response body는 기록하지 않는다.
- 캐시 hit에서 HTTP 호출이 발생하지 않았음을 구분한다.
- 기존 실패 시 `None` 반환 또는 error 격리 동작을 바꾸지 않는다.

### 11.10 `text_rag.py` 및 VectorRAG pipeline

GraphRAG만 계측하고 VectorRAG를 누락하지 않는다.

최소 측정:

- pipeline 전체 시간
- query embedding
- vector Neo4j query
- 검색 결과 수
- answer LLM `purpose=text_rag_answer`
- citation 수와 answer 문자 수
- 오류 결과

GraphRAG와 VectorRAG의 `request.completed` 스키마는 동일해야 한다.

---

## 12. 구현 단계

각 단계가 끝날 때 관련 테스트를 먼저 통과시킨 뒤 다음 단계로 진행한다.

### Step 0. 변경 전 기준선 확보

1. `git status --short`로 기존 변경을 확인한다.
2. 사용자가 만든 변경을 덮어쓰거나 되돌리지 않는다.
3. 현재 전체 테스트를 실행한다.
4. 현재 테스트 수, 성공/실패, 실행 명령을 기록한다.
5. GraphRAG와 VectorRAG 호출 구조를 코드로 재확인한다.
6. 현재 LangChain 및 provider 버전과 callback 공개 API를 확인한다.

이 단계에서는 코드를 수정하지 않는다.

### Step 1. 관측성 core 구축

1. `chatbot/observability/` 패키지를 만든다.
2. 공통 event schema와 제한된 enum 값을 정의한다.
3. `EventSink` protocol을 정의한다.
4. Null, Logging, Memory sink를 구현한다.
5. 안전한 `emit_event()` facade를 구현한다.
6. `perf_counter()` 기반 span context manager를 구현한다.
7. sink 오류가 원래 로직으로 전파되지 않게 한다.
8. core 단위 테스트를 작성한다.

### Step 2. 요청 context 구축

1. `ContextVar` 기반 RequestContext를 구현한다.
2. root context 생성/종료 context manager를 구현한다.
3. 누계 counter와 route 업데이트 API를 제공한다.
4. 문맥이 없는 테스트/CLI 경로의 동작을 정의한다.
5. 중첩 context와 reset을 테스트한다.
6. 병렬 thread 또는 async context에서 누출되지 않음을 가능한 범위에서 테스트한다.

### Step 3. LLM callback 구축

1. 설치된 LangChain callback 공개 API에 맞춘 handler를 구현한다.
2. 실제 ChatModel/LLM 시작과 종료를 추적한다.
3. run ID와 parent run ID를 사용해 중복 종료 이벤트를 방지한다.
4. tag/run name에서 제한된 `purpose`를 읽는다.
5. token usage 정규화기를 구현한다.
6. 성공, 오류, usage 없음, metadata 형태 차이를 테스트한다.
7. callback 자체 오류가 모델 결과에 영향을 주지 않음을 테스트한다.

### Step 4. LLM 목적 태깅

1. Cypher generation chain에 `cypher_generation` 목적을 부여한다.
2. Graph QA chain에 `graph_qa` 목적을 부여한다.
3. 최종 synthesis에 `final_synthesis`를 부여한다.
4. VectorRAG 답변 chain에 `text_rag_answer`를 부여한다.
5. 일반 대화와 ReAct 호출을 각각 구분한다.
6. prompt나 출력 내용이 event에 포함되지 않는지 확인한다.
7. graph chain 내부의 두 LLM 이벤트가 실제로 분리되는지 통합 테스트한다.

### Step 5. pipeline과 retrieval 계측

1. GraphRAG pipeline의 주요 span을 추가한다.
2. evidence orchestrator의 검색기별 span을 추가한다.
3. VectorRAG pipeline을 같은 수준으로 계측한다.
4. 검색 결과 수와 상태를 기록한다.
5. short circuit와 fallback outcome을 구분한다.
6. 반환값·키·호출 순서 회귀 테스트를 실행한다.

### Step 6. embedding, Neo4j, authority 계측

1. embedding public boundary를 계측한다.
2. Neo4j query의 실제 실행 경계를 계측한다.
3. operation을 상위 문맥에서 안전하게 지정한다.
4. authority service에서 cache hit/miss를 계측한다.
5. authority client에서 HTTP 결과를 계측한다.
6. 중복 이벤트가 없는지 검증한다.
7. raw 입력·query·응답이 이벤트에 들어가지 않는지 검사한다.

### Step 7. 요청 terminal event와 집계

1. 요청 성공 시 `request.completed`를 정확히 한 번 발행한다.
2. 오류, short circuit, fallback 성공/실패를 구분한다.
3. 목적별 LLM 호출 수와 token 합계를 집계한다.
4. embedding 호출 수, citation 수, answer chars를 집계한다.
5. request context를 반드시 reset한다.
6. 연속 요청 누출 테스트를 작성한다.

### Step 8. 기준선 검증과 문서화

1. 결정적인 test double 시나리오로 현재 호출 구조를 측정한다.
2. 가능한 경우 개발 환경에서 실제 provider를 호출하지 않는 dry-run 방법을 제공한다.
3. 실서비스 credential이나 외부 네트워크가 없으면 live 수치를 만들지 않는다.
4. `docs/OBSERVABILITY.md`를 작성한다.
5. `OBSERVABILITY_PHASE1_IMPLEMENTATION_NOTE.md`에 변경 및 검증 결과를 기록한다.
6. 전체 테스트와 compile/import 검증을 실행한다.

---

## 13. 테스트 요구사항

### 13.1 core 테스트

- 이벤트가 JSON 직렬화 가능하다.
- 공통 필드와 schema version이 존재한다.
- duration이 음수가 아니다.
- span 성공 시 success event가 발행된다.
- span 예외 시 error event가 발행되고 원래 예외가 그대로 재발생한다.
- sink 예외가 애플리케이션으로 전파되지 않는다.
- NullSink는 부작용이 없다.
- MemorySink는 이벤트 순서를 보존한다.

### 13.2 context 테스트

- 요청마다 서로 다른 `request_id`가 생성된다.
- 하위 함수에서 같은 request context를 읽는다.
- context 종료 후 값이 누출되지 않는다.
- 중첩 context 종료 시 부모가 복원된다.
- 예외 종료 시에도 reset된다.
- request counter가 요청 간 섞이지 않는다.

### 13.3 LLM callback 테스트

- 실제 model lifecycle 하나가 `llm.completed` 하나를 만든다.
- chain callback은 model call count를 증가시키지 않는다.
- purpose tag가 올바르게 정규화된다.
- 알 수 없는 purpose는 `other`다.
- usage metadata 변형이 표준 필드로 변환된다.
- usage가 없으면 `usage_available=false`이며 token은 `null`이다.
- 모델 오류 시 status와 error class를 기록한다.
- prompt와 message content가 event에 들어가지 않는다.
- 동일 run 종료 callback 중복에 안전하다.

### 13.4 GraphRAG 통합 테스트

정상 요청 fixture에서 최소 다음을 검증한다.

- `pipeline.graphrag.completed` 1개
- graph retrieval 이벤트 1개
- vector retrieval 이벤트 1개
- 목적별 LLM 호출:
  - `cypher_generation=1`
  - `graph_qa=1`
  - `final_synthesis=1`
- request의 전체 LLM call count가 위 model 호출과 일치
- evidence 수, citation 수, answer chars 집계
- 기존 최종 답변과 citations 반환 형식 불변
- 기존 검색 호출 순서 불변

전체 retrieval 실패 fixture:

- `outcome=short_circuit`
- `final_synthesis=0`
- 기존 오류 응답 불변

GraphRAG transient provider 오류 fixture:

- 기존 조건에서만 fallback 실행
- 같은 `request_id` 유지
- `fallback_used=true`
- fallback outcome 구분
- 원래 예외와 correlation ID 의미 불변

### 13.5 VectorRAG 통합 테스트

- `pipeline.vectorrag.completed` 1개
- embedding 호출과 vector query가 구분됨
- `text_rag_answer` 목적의 모델 호출 기록
- 검색 결과와 citation 수 기록
- 기존 답변/인용 형식 불변

### 13.6 Neo4j 및 authority 테스트

- query text가 event에 포함되지 않는다.
- operation별 event가 구분된다.
- row count 미확인 상태를 거짓 0으로 기록하지 않는다.
- safety reject 시 DB 호출이 없고 결과가 기록된다.
- authority cache hit는 HTTP 호출 0으로 기록된다.
- authority cache miss는 실제 fetch와 구분된다.
- 외부 URL, ID, body가 event에 포함되지 않는다.

### 13.7 개인정보 회귀 테스트

고유 marker 문자열을 질문, prompt, Cypher, evidence, URL, 외부 응답에 넣은 fixture를 만든 뒤 모든 MemorySink event를 JSON으로 직렬화하여 marker가 존재하지 않음을 검증한다.

다음 key 자체가 event schema에 들어가지 않는지도 검증한다.

```text
question
prompt
messages
answer
cypher
query_text
evidence_text
response_body
api_key
password
embedding_vector
```

`answer_chars`, `query_origin`처럼 안전한 파생 필드는 허용한다.

### 13.8 전체 회귀 테스트

- 변경 전 전체 테스트가 계속 통과한다.
- 새 관측성 테스트가 모두 통과한다.
- compile/import smoke test가 통과한다.
- 관측성 비활성 상태에서 기존 public API 반환값이 동일하다.
- 테스트가 실제 API, 인터넷, 실 Neo4j에 의존하지 않는다.

---

## 14. 기준선 시나리오

Phase 1 구현 완료 시 아래 시나리오를 test double로 재현하고 결과를 구현 보고서에 표로 정리한다.

| 시나리오 | 예상 핵심 결과 |
|---|---|
| 정상 GraphRAG | Cypher 생성 1, Graph QA 1, 최종 synthesis 1 |
| graph empty + vector success | 검색기 status 분리, 최종 synthesis 실행 |
| 모든 검색 실패 | short circuit, 최종 synthesis 0 |
| 정상 VectorRAG | embedding, vector query, text RAG answer 구분 |
| GraphRAG transient 오류 후 ReAct 성공 | 동일 request ID, fallback success |
| provider usage 없음 | token null, usage unavailable |
| authority cache hit | 외부 HTTP 0, cache hit true |
| 관측 sink 실패 | 사용자 결과 불변 |

실제 API token이나 외부 서버가 준비되지 않은 환경에서는 실사용량 숫자를 임의로 만들지 않는다. Phase 1 완료 기준은 **실제 환경에서 측정 가능한 구조와 결정적인 자동 테스트**다.

---

## 15. 문서 요구사항

### 15.1 `docs/OBSERVABILITY.md`

최소 다음 내용을 포함한다.

- 관측성의 목적과 범위
- 활성/비활성 방법
- sink 설정 방법
- event schema와 주요 event catalog
- 각 필드의 의미와 단위
- LLM purpose 정의
- token usage의 한계
- 개인정보 비기록 정책
- request ID와 correlation ID 차이
- 로컬에서 event를 확인하는 방법
- Phase 2 최적화 전후 비교 방법
- 알려진 제약

### 15.2 `OBSERVABILITY_PHASE1_IMPLEMENTATION_NOTE.md`

최소 다음 내용을 포함한다.

- 실제 변경 파일 목록
- 구현 구조와 설계 판단
- 기존 동작을 보존한 방법
- Graph QA 내부 호출 분리 방식
- callback 및 token usage 처리 방식
- 실행한 테스트 명령과 결과
- 기준선 시나리오 결과
- 테스트 수 변경
- 미해결 사항
- 다음 단계에서 사용할 핵심 지표

실패하거나 실행하지 못한 검증을 성공했다고 쓰지 않는다.

---

## 16. Phase 2를 위한 지표 정의

Phase 1 결과로 최소 다음 집계가 가능해야 한다. 이번 단계에서 dashboard를 만들 필요는 없다.

### 16.1 요청 지표

- 요청 수
- 성공률과 오류율
- 전체 latency p50/p95/p99 계산에 필요한 `duration_ms`
- mode 및 route별 요청 수
- short circuit 비율
- fallback 비율과 성공률

### 16.2 LLM 비용 지표

- 요청당 LLM 호출 수
- 목적별 LLM 호출 수
- 목적별 입력/출력/합계 token
- Graph QA 호출 비율
- Graph QA token이 전체에서 차지하는 비중
- final synthesis latency와 token
- usage metadata 제공률

### 16.3 검색 지표

- 검색기별 success/empty/error 비율
- 검색기별 latency p50/p95/p99 계산에 필요한 값
- 결과 수 분포
- graph/vector/authority가 최종 evidence에 기여한 항목 수
- Neo4j operation별 latency
- authority cache hit 비율

### 16.4 Phase 2 비교 기준

향후 Graph QA 호출 제거 작업에서는 최소 다음을 비교해야 한다.

- `graph_qa` 호출: 요청당 1회 → 0회
- GraphRAG 전체 LLM 호출 수 감소
- 요청당 입력/출력 token 감소
- Graph retrieval 및 전체 응답 latency 변화
- 최종 답변 및 citation 회귀 여부
- Graph QA 제거 전후 오류율

Phase 1 구현이 위 비교를 지원하지 못하면 완료로 처리하지 않는다.

---

## 17. 완료 기준(Acceptance Criteria)

아래 항목을 모두 만족해야 Phase 1이 완료된 것이다.

### 구조

- [ ] 관측성 전용 패키지가 애플리케이션 코드와 분리되어 있다.
- [ ] 특정 외부 관측 플랫폼 SDK에 결합되지 않았다.
- [ ] Streamlit이 관측성 core에 import되지 않는다.
- [ ] 요청 문맥이 `ContextVar` 등 안전한 방식으로 전파된다.
- [ ] 기존 public API를 불필요하게 변경하지 않았다.

### 정확성

- [ ] 실제 provider LLM 호출만 call count에 포함한다.
- [ ] Cypher 생성, Graph QA, 최종 synthesis를 구분한다.
- [ ] GraphRAG 결정적 테스트에서 목적별 호출 수가 각각 검증된다.
- [ ] VectorRAG LLM과 embedding 호출도 측정된다.
- [ ] provider usage가 있을 때 token을 기록한다.
- [ ] usage가 없을 때 값을 조작하거나 추정하지 않는다.
- [ ] 요청 완료 이벤트가 정상·오류 경로에서 정확히 한 번 발생한다.

### 안전성

- [ ] 질문, 답변, prompt, Cypher, evidence 원문이 기록되지 않는다.
- [ ] API key, 비밀번호, token, session state가 기록되지 않는다.
- [ ] embedding vector와 외부 response body가 기록되지 않는다.
- [ ] 관측 실패가 사용자 요청을 실패시키지 않는다.
- [ ] context가 다음 요청으로 누출되지 않는다.

### 동작 보존

- [ ] Graph QA LLM 호출을 제거하지 않았다.
- [ ] 모델과 prompt를 변경하지 않았다.
- [ ] 검색 순서와 라우팅을 변경하지 않았다.
- [ ] fallback 조건과 오류 의미를 변경하지 않았다.
- [ ] 최종 답변 및 citation 계약을 변경하지 않았다.
- [ ] 기존 전체 테스트가 통과한다.
- [ ] 신규 관측성 테스트가 통과한다.

### 문서

- [ ] `docs/OBSERVABILITY.md`가 작성되었다.
- [ ] 구현 보고서에 검증 명령과 실제 결과가 기록되었다.
- [ ] 기준선 시나리오 결과가 기록되었다.
- [ ] 알려진 제한과 미실행 검증이 정직하게 기록되었다.

---

## 18. 작업 중지 및 보고 조건

다음 상황에서는 억지로 구현하지 말고 변경 범위와 함께 보고한다.

- 현재 LangChain 공개 API로 GraphCypherQAChain 내부 generation/QA 호출을 안정적으로 구분할 수 없는 경우
- 구분을 위해 private API 수정이나 monkey patch가 필요한 경우
- 실제 provider 객체가 callback 또는 usage metadata를 제공하지 않는 경우
- 관측 훅 추가가 chain의 반환값이나 retry 의미를 바꾸는 경우
- Neo4j row count 측정을 위해 결과 소비 방식 변경이나 쿼리 재실행이 필요한 경우
- 기존 테스트가 변경 전부터 실패하는 경우
- 사용자의 기존 변경과 충돌하는 경우
- 새로운 외부 패키지 설치가 필수라고 판단되는 경우

보고에는 다음을 포함한다.

- 막힌 정확한 파일과 API
- 확인한 대안
- 각 대안의 위험
- 가장 작은 권장 변경

---

## 19. Claude Code 최종 작업 순서 요약

1. 이 문서를 끝까지 읽는다.
2. `git status`와 현재 테스트 기준선을 확인한다.
3. 실제 GraphRAG/VectorRAG/GraphCypherQAChain 호출 구조를 재검증한다.
4. 관측성 core와 테스트를 먼저 만든다.
5. 요청 context를 연결한다.
6. LangChain callback과 purpose 태깅을 구현한다.
7. Graph QA 내부의 두 LLM 호출이 분리되는 테스트를 먼저 통과시킨다.
8. pipeline/retrieval/embedding/Neo4j/authority 계측을 순차 연결한다.
9. 민감정보 비기록 테스트를 통과시킨다.
10. 전체 회귀 테스트를 실행한다.
11. 관측성 문서와 구현 보고서를 작성한다.
12. 변경 파일, 실제 테스트 결과, 알려진 한계를 최종 보고한다.

---

## 20. 최종 원칙

이번 작업의 성공 기준은 로그가 많이 생기는 것이 아니다.

성공적인 Phase 1 관측성은 다음 세 조건을 동시에 만족해야 한다.

1. **정확성**: 실제 호출, 지연, token, 분기를 중복 없이 구분한다.
2. **안전성**: 질문과 자료 원문, 인증정보를 기록하지 않는다.
3. **동작 보존**: 현재 챗봇의 답변, 검색, 오류, 폴백 동작을 바꾸지 않는다.

특히 이번 단계에서는 Graph QA 호출을 없애지 않는다. `cypher_generation`, `graph_qa`, `final_synthesis`를 구분해 현재 비용을 먼저 증명하고, 그 측정 결과를 다음 최적화 단계의 비교 기준으로 남기는 것이 목적이다.

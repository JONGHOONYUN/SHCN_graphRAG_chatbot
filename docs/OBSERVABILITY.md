# 관측성 (Phase 1)

시화총림 챗봇의 요청 단위 관측 이벤트를 설명한다. 대상 독자는 이 이벤트로
지연시간·LLM 비용·검색 품질을 분석하거나, 다음 단계(Phase 2: Graph QA LLM 호출
제거)의 전후를 비교할 개발자·운영자다.

## 1. 목적과 범위

Phase 1은 **측정 단계**다. 챗봇의 답변·검색·오류·폴백 동작은 바꾸지 않고, 한
사용자 요청이 무엇을 했는지 구조화 이벤트로 기록한다.

- 어느 경로(graphRAG / vectorRAG / ReAct 폴백)를 거쳤는가
- 전체 및 단계별 지연시간
- 실제 LLM provider 호출 수와 **목적**(Cypher 생성 · Graph QA · 최종 합성 …)
- provider가 보고한 입력·출력·합계 토큰
- 그래프 · 벡터 · 외부 전거 검색의 성공 / 빈 결과 / 실패
- Neo4j · embedding · 외부 HTTP 호출의 지연과 시도 횟수
- 최종 합성 실행 여부, 폴백 여부, 답변 길이, 인용 수

이번 범위가 **아닌** 것: 외부 모니터링 서비스·대시보드, 새 SDK(OpenTelemetry·
Prometheus·Sentry 등), 호출 최적화. Graph QA 호출은 그대로 발생하며, 측정만 한다.

## 2. 켜고 끄기, sink 설정

설정은 환경변수로 한다. 관측성 core는 Streamlit을 import하지 않으므로
`st.secrets`는 읽지 않는다.

| 환경변수 | 값 | 기본값 | 의미 |
|---|---|---|---|
| `CHATBOT_OBSERVABILITY` | `log` / `off` | `log` | `off`면 모든 관측이 no-op (이벤트 0건, callback 미설치) |
| `CHATBOT_OBSERVABILITY_LOG_LEVEL` | `INFO`, `DEBUG`, `WARNING` … | `INFO` | 이벤트를 기록할 로그 레벨 |
| `CHATBOT_OBSERVABILITY_CONSOLE` | `1` | (없음) | 이벤트 logger에만 stderr handler를 붙인다 |

이벤트는 logger `chatbot.observability.events`로 **한 줄에 JSON 하나**씩 나간다.
관측성은 root logger나 전역 handler를 건드리지 않는다. 따라서 기본값에서는
애플리케이션의 logging 설정이 INFO를 받지 않는 한 화면에 아무것도 나오지 않는다
(테스트 출력도 오염되지 않는다). 콘솔에서 보려면 `CHATBOT_OBSERVABILITY_CONSOLE=1`을
쓴다.

코드에서 sink를 직접 주입할 수도 있다.

```python
from chatbot.observability import telemetry
from chatbot.observability.sinks import MemorySink, NullSink, LoggingSink

telemetry.configure(sink=LoggingSink())      # 프로세스 전역
with telemetry.use_sink(MemorySink()) as sink:   # 현재 문맥만 (테스트 권장)
    ...
```

sink는 `emit(event: Mapping) -> None` 하나만 구현하면 된다. sink가 예외를 던져도
사용자 요청은 영향을 받지 않는다(§8).

## 3. 이벤트 공통 필드

```json
{"schema_version": 1, "timestamp": "2026-09-27T12:34:56.789Z",
 "event_name": "llm.completed", "request_id": "c1f0…(32 hex)",
 "mode": "graphrag", "route": "graphrag",
 "question_language": "ko", "response_language": "ko",
 "status": "success", "duration_ms": 312.4}
```

| 필드 | 의미 |
|---|---|
| `schema_version` | 정수 `1`. 스키마가 호환되지 않게 바뀌면 올린다 |
| `timestamp` | 이벤트 발행 시각, UTC ISO-8601 (ms, `Z`) |
| `request_id` | 사용자 요청 1건의 식별자 (요청 밖에서는 `null`) |
| `mode` | 사용자가 고른 모드: `graphrag` · `vectorrag` · `unknown` |
| `route` | 실제 실행 경로: `graphrag` · `vectorrag` · `react_fallback` · `general_chat` · `unknown` |
| `status` | `success` · `empty` · `error` · `skipped` |
| `duration_ms` | `time.perf_counter()`로 잰 구간 길이(ms, 0 이상). 벽시계 차이가 아니다 |

규칙: **모르는 값은 `null`**(거짓 0을 만들지 않는다), 확실한 0은 `0`이다.
스키마에 없는 필드와 검증에 실패한 값은 조용히 생략된다. 알 수 없는 객체를
`str()`/`repr()`로 직렬화하지 않는다.

### `attempt_count`의 의미 (모든 이벤트 공통)

"**우리 코드가 직접 수행한** 시도 횟수"다.

- 우리 코드가 재시도를 수행하는 곳은 정확한 값: Neo4j 읽기 재시도(`_read_with_retry`),
  embedding HTTP 재시도, 생성 Cypher 재생성(최대 2회), 외부 전거 HTTP(1회)
- 재시도가 우리 코드 밖에서 일어나 보이지 않으면 `null`: LLM(Gemini SDK 내부),
  Neo4jVector 드라이버 재시도, 대화 이력 드라이버
- 시도가 없었음이 확실하면 `0`: 캐시 hit, 안전 검증 거부, 링크 전용 소스

## 4. 이벤트 카탈로그

### 요청

| 이벤트 | 추가 필드 |
|---|---|
| `request.started` | `started_at` |
| `request.completed` | `outcome`, `llm_call_count`, `llm_calls_by_purpose`, `usage_available_call_count`, `input_tokens`, `output_tokens`, `total_tokens`, `embedding_call_count`, `fallback_used`, `citation_count`, `answer_chars`, `started_at`, (오류 시) `error_type`, `correlation_id` |

`request.completed`는 요청마다 **정확히 한 번**, 예외로 끝나도 발행된다.
graphRAG와 vectorRAG의 스키마는 같다.

`outcome`:

| 값 | 의미 |
|---|---|
| `success` | 정상 답변 |
| `short_circuit` | 그래프·벡터 검색이 모두 일시 불가하여 LLM 호출 없이 안내문 반환 |
| `error` | 정상 답변을 만들지 못함 — 예외가 전파됐거나, 오류 정책이 지역화된 안전 메시지로 바꿔 반환한 경우 모두 |
| `fallback_success` | ReAct 폴백으로 답변 |
| `fallback_error` | 폴백까지 실패 |

`status`는 `outcome`을 거칠게 줄인 값이다(`success`/`fallback_success` → `success`,
나머지 → `error`). 오류율은 `outcome`으로 계산한다. 빈 합성 결과(no_results)는
기존 정책대로 오류가 아니며 `outcome=success`, 파이프라인 span의 `status=empty`로
구분된다.

`llm_calls_by_purpose`는 모든 목적 키를 항상 포함한다(없으면 0). 토큰 합계는
**provider가 usage를 보고한 호출만** 더한 값이며, 한 번도 보고되지 않았으면
`null`이다. 부분 합계인지 여부는 `usage_available_call_count`와
`llm_call_count`를 비교해 판단한다.

### 파이프라인 구간

| 이벤트 | 추가 필드 |
|---|---|
| `history.read.completed` | `history_message_count` |
| `evidence.gather.completed` | `evidence_graph_count`, `evidence_vector_count`, `evidence_external_count` |
| `evidence.format.completed` | `evidence_chars` |
| `synthesis.completed` | `synthesis_executed`, `output_chars` (short circuit이면 `status=skipped`, `synthesis_executed=false`) |
| `citations.build.completed` | `citation_count` |
| `answer.render.completed` | `answer_chars` |
| `history.write.completed` | (저장 생략 시 `status=skipped`) |
| `pipeline.graphrag.completed` | `outcome`, `short_circuit`, `synthesis_executed`, `answer_chars`, `citation_count`, evidence 개수 |
| `pipeline.vectorrag.completed` | `outcome`, `evidence_vector_count`, `answer_chars`, `citation_count` |
| `fallback.completed` | `trigger_error_type`(폴백을 일으킨 원래 예외), `correlation_id`(원래 예외의 것), 실패 시 `error_type` |

### 검색

| 이벤트 | 추가 필드 |
|---|---|
| `retrieval.graph.completed` | `retriever` (`graph` 또는 결정적 순위 질의면 `role_ranking`) |
| `retrieval.vector.completed` | `retriever=vector` |
| `retrieval.authority.completed` | `retriever=authority` (전거 보강 전체) |
| `retrieval.authority_source.completed` | `source`(registry 키), `node_type`, `cache_hit`, `http_fetched` |
| `authority.http.completed` | `http_status_code`, `http_status_class`, `response_bytes` |

검색 이벤트 공통: `status`, `result_count`, `attempt_count`, `duration_ms`, 오류 시
`error_type`과 서버 로그 줄의 `correlation_id`. 검색기가 오류를 흡수해도(기존
동작) 이벤트에는 `status=error`와 예외 클래스 이름이 남는다.

### Neo4j · embedding · LLM

| 이벤트 | 추가 필드 |
|---|---|
| `neo4j.query.completed` | `operation`, `query_origin`, `row_count`, `attempt_count`, `safety_result` |
| `embedding.completed` | `purpose`(`vector_query`/`document_indexing`), `model`, `input_count`, `input_chars`, `dimension`, `attempt_count` |
| `llm.completed` | `purpose`, `provider`, `model`, `attempt_count`, `usage_available`, `input_tokens`, `output_tokens`, `total_tokens`, `input_chars`, `output_chars`, 오류 시 `error_type` |

`operation`: `generated_graph_query`(LLM이 만든 Cypher), `deterministic_lookup`(순위
템플릿), `vector_query`, `history_read`, `history_write`, `other`.
`safety_result`: 생성·결정적 Cypher는 `passed`/`rejected`, 그 외는 `not_applicable`.
안전 검증이 거부하면 DB 호출 없이 `status=skipped`, `attempt_count=0`으로 기록된다.
operation은 호출자가 라벨링하며, query 문자열을 보고 추론하지 않는다.

### 관측 내부 오류

`observability.error`(`component`, `error_type`)는 sink가 아니라 내부 logger
`chatbot.observability.internal`에 WARNING으로만 남는다(재귀 방지). 같은 종류는
프로세스당 5회까지만 경고한다.

## 5. LLM 목적(`purpose`)

| 값 | 호출 위치 |
|---|---|
| `cypher_generation` | GraphCypherQAChain 내부 Cypher 생성 LLM |
| `graph_qa` | GraphCypherQAChain 내부 그래프 답변(prose) 생성 LLM |
| `final_synthesis` | graphRAG 최종 evidence 합성 |
| `text_rag_answer` | vectorRAG 답변 생성 |
| `react_iteration` | ReAct 폴백의 매 reasoning 호출 |
| `general_chat` | ReAct의 General Chat tool |
| `legacy_vector_answer` | ReAct의 Sihwa Content Search tool(검색 후 prose 생성) |
| `language_detection` | 예약값 — 현재 언어 판별은 LLM을 쓰지 않는다 |
| `other` | 라벨이 없거나 알 수 없는 값 |

**어떻게 구분하는가.** prompt를 읽지 않는다. 각 **leaf LLM**에 생성 시점에
`with_llm_purpose(llm, purpose)`로 `metadata={"llm_purpose": ...}`를 묶고, callback은
그 제한된 값만 읽는다. GraphCypherQAChain은 공개 파라미터 `cypher_llm`/`qa_llm`을
따로 받으므로(`chatbot/retrieval/graph_chain.py`), 두 내부 LLM을 private API 없이
구분한다. 두 파라미터에는 같은 모델 객체가 들어가므로 동작은 이전과 같다.

purpose는 chain이 아니라 leaf LLM에만 묶어야 한다. LangChain은 부모 config의
metadata가 자식 바인딩을 덮어쓰므로, chain에 purpose를 붙이면 하위 LLM의 목적이
가려진다.

**무엇을 세는가.** LangChain의 ChatModel/LLM lifecycle(시작 → 종료/오류) 한 쌍이
provider 호출 1회다. chain·parser callback은 세지 않고, 같은 run의 종료가 두 번
와도 한 번만 센다. handler는 LangChain 공개 API `register_configure_hook`로 요청
문맥에만 설치되므로 요청 밖의 LLM 호출은 기록되지 않는다.

## 6. 토큰 usage의 한계

- provider가 보고한 공식 metadata만 기록한다. 문자 수로 추정하지 않는다
  (`input_chars`/`output_chars`는 크기 지표일 뿐 토큰이 아니다).
- 읽는 위치(첫 번째로 값이 있는 한 곳만): `AIMessage.usage_metadata` →
  `response_metadata`(Google 원형 `prompt_token_count`…, OpenAI형 `prompt_tokens`…)
  → `generation_info` → `LLMResult.llm_output`. 여러 위치를 합산하지 않는다.
- 일부 값만 오면 그 값만 기록한다(합계를 계산해 채우지 않는다).
- usage가 없으면 세 값 모두 `null`, `usage_available=false`.
- streaming 호출은 최종 결과 1개만 본다(chunk를 따로 더하지 않는다). 값의
  정확성은 provider 통합이 chunk usage를 어떻게 합치는지에 달려 있다.
- langchain-google-genai 4.2.1은 Gemini가 개수를 빼먹으면 해당 값을 `0`으로 채워
  돌려준다. 이것은 provider 통합의 값이며 우리가 만든 0이 아니다.

## 7. 개인정보 비기록 정책

이벤트에는 다음이 **절대 들어가지 않는다**: 질문·답변·대화 이력 원문, prompt,
Cypher, Neo4j record, evidence 문장, 외부 응답 body, URL·외부 식별자, API key·
토큰·쿠키·비밀번호, embedding 벡터, 예외 메시지·stack trace, Streamlit session
state.

이는 관례가 아니라 구조로 강제된다. 이벤트 필드는 `chatbot/observability/events.py`의
allowlist에 선언된 것만 허용되고, 원문을 담을 수 있는 필드 이름은 스키마에 존재하지
않는다(`question`, `prompt`, `cypher`, `url`, `error_message` 등은 금지 목록).
기록하는 것은 파생값뿐이다: 길이(`*_chars`), 개수(`*_count`), 제한된 enum, provider
token, 차원 수, HTTP 상태 코드, 캐시 hit 여부, 언어 코드, 예외 클래스 이름,
기존 `correlation_id`. 원문 hash도 기록하지 않는다.

`tests/test_observability_privacy.py`가 질문·이력·Cypher·graph row·벡터 문서·
LLM 출력·예외 메시지·URL·외부 ID·응답 body에 marker를 심고 전체 이벤트를 검사한다.

주의: 기존 서버 로그(`tools.cypher`, `tools.orchestrator` 등)는 이번 작업 전부터
예외 메시지를 남긴다. 그 동작은 바꾸지 않았고, 관측 이벤트로 복제하지도 않는다.

## 8. 동작 보존과 실패 격리

- 계측은 반환값을 감싸거나 바꾸지 않는다. span은 감싼 코드의 예외를 **그대로**
  재발생시킨다(삼키거나 다른 예외로 바꾸지 않는다).
- 측정을 위해 작업을 다시 실행하지 않는다: row 수는 이미 받은 리스트의 `len()`,
  토큰은 이미 받은 응답의 metadata다. 추가 LLM·Neo4j·HTTP 호출은 0회다.
- sink·직렬화·callback의 실패는 내부 logger 경고로만 남고 사용자 요청으로
  전파되지 않는다.
- 관측을 끄면(`off`) 모든 span이 공유 no-op이 되고 callback이 설치되지 않는다.
- 자동 테스트가 관측 on / off / sink 실패 세 경우의 답변 문자열이 동일함을 확인한다.

## 9. request ID와 correlation ID

| | `request_id` | `correlation_id` |
|---|---|---|
| 단위 | 사용자 요청 1건 전체 | 특정 오류·로그 줄 1건 |
| 생성 | `bot.py handle_submit`이 backend 호출 직전에 | 기존 오류 처리 코드(`errors.py`, 로그 줄) |
| 수명 | graphRAG → ReAct 폴백까지 같은 값 유지 | 오류마다 새 값 |
| 용도 | 한 요청의 이벤트를 묶는다 | 이벤트에서 서버 로그 줄로 건너간다 |

`correlation_id`는 `request_id`로 대체하지 않는다. 오류 이벤트에는 둘 다 있을 수
있다. 예를 들어 `bot.py`의 catch-all이 사용자에게 보여 주는 `[ref: 1a2b3c4d]`는
`request.completed.correlation_id`에 연결된다. `request_id`는 UI에 표시하지 않으며,
metric label로 쓰지 않는다(로그 상관관계 전용).

요청 문맥은 `ContextVar`로 전파되므로 공개 함수 시그니처는 바뀌지 않았다.
Streamlit 없이 `agent.generate_response`나 파이프라인을 직접 호출하면(테스트·CLI)
그 호출이 스스로 root 문맥을 연다. 문맥은 정상 종료와 예외 종료 모두에서 reset되어
다음 요청으로 새지 않는다.

## 10. 로컬에서 이벤트 보기

**실제 앱에서**

```powershell
$env:CHATBOT_OBSERVABILITY_CONSOLE = "1"
streamlit run bot.py 2> events.log
```

이벤트 줄만 골라 보기(Streamlit 자체 로그 줄은 JSON이 아니므로 걸러진다):

```powershell
python -c "import json,sys; [print(l.strip()) for l in open('events.log', encoding='utf-8') if l.startswith('{')]"
```

요청별 목적 호출 수와 토큰 합계:

```python
import json, collections
events = [json.loads(l) for l in open("events.log", encoding="utf-8") if l.startswith("{")]
for e in events:
    if e["event_name"] == "request.completed":
        print(e["request_id"][:8], e["outcome"], e["duration_ms"],
              {k: v for k, v in e["llm_calls_by_purpose"].items() if v},
              e["total_tokens"])
```

**provider 없이 (dry run)**

```powershell
python tests/observability_baseline.py           # 기준선 시나리오 요약
python tests/observability_baseline.py --jsonl   # 모든 이벤트
```

실제 API 키·인터넷·Neo4j 없이, 자동 테스트와 같은 test double로 실제 코드 경로를
실행한다. 출력 토큰 수는 fixture에 적어 둔 값이며 실제 사용량이 아니다.

## 11. Phase 2(Graph QA 호출 제거) 전후 비교 방법

같은 질문 집합을 제거 전·후에 실행해 이벤트를 모은 뒤 다음을 비교한다.

| 지표 | 계산 |
|---|---|
| Graph QA 호출 | `request.completed.llm_calls_by_purpose.graph_qa`: 요청당 1 → 0 |
| 요청당 LLM 호출 수 | `request.completed.llm_call_count` (mode=graphrag) |
| Graph QA 토큰 비중 | `llm.completed`에서 `purpose=graph_qa`의 `total_tokens` 합 ÷ 전체 합 |
| 요청당 토큰 | `request.completed.input_tokens` / `output_tokens` (`usage_available_call_count`로 완전성 확인) |
| graph 검색 지연 | `retrieval.graph.completed.duration_ms`의 p50/p95/p99 |
| 전체 지연 | `request.completed.duration_ms`의 p50/p95/p99 |
| 답변·인용 회귀 | `answer_chars`, `citation_count` 분포 + 기존 회귀 테스트 |
| 오류율 | `request.completed.outcome` 비율 (`error`, `short_circuit`, `fallback_*`) |
| usage 제공률 | `usage_available_call_count ÷ llm_call_count` |

그 밖에 이번 이벤트로 계산할 수 있는 것: mode·route별 요청 수, 검색기별
success/empty/error 비율과 지연 분포, 결과 수 분포, Neo4j operation별 지연,
authority cache hit 비율, 폴백 비율과 성공률.

Graph QA 호출은 `retrieval.graph.completed` span 안에서 일어나므로, 제거하면
`retrieval.graph` 지연이 `graph_qa` LLM 지연만큼 줄어드는지 직접 확인할 수 있다.

## 12. 알려진 제약

- **LLM 재시도 횟수는 모른다.** Gemini SDK 내부 재시도는 callback에 보이지 않아
  `attempt_count=null`이다.
- **Neo4jVector 조회의 재시도·이력 드라이버 호출**도 관찰할 수 없어 `null`이다.
- **대화 이력 계측 범위.** graphRAG 정상 경로의 이력 읽기·쓰기만 따로 잰다.
  `history_read`에는 이력 핸들 생성 시 세션 노드 준비가 포함된다. vectorRAG와 ReAct
  경로의 이력은 `RunnableWithMessageHistory`가 내부에서 처리하므로 따로 재지 않는다.
- **`row_count`의 기준.** 생성 Cypher의 `row_count`는 DB가 돌려준 행 수이고,
  GraphCypherQAChain은 그 뒤 `top_k`(10)로 자른다. 자른 뒤의 개수는
  `retrieval.graph.completed.result_count`로 본다.
- **`general_chat` route는 현재 도달하지 않는다.** 일반 대화는 독립 경로가 아니라
  ReAct의 tool로만 실행되므로 route는 `react_fallback`이고, 목적은 `general_chat`으로
  구분된다. route 값은 예약해 두었다.
- **요청 밖의 호출은 기록되지 않는다.** 요청 문맥이 없는 곳(예: 임포트 시점)의 LLM
  호출은 callback이 설치되지 않아 이벤트가 없다. 파이프라인과 `agent`/`text_rag`
  진입점은 문맥이 없으면 스스로 연다.

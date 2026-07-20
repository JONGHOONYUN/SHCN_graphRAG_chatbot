# Claude Code 작업 지시서: 보안·초기화·신뢰성 하드닝 및 구조 정리

## 1. 작업 목적

현재의 evidence-first graphRAG 구조와 외부 authority 안전장치를 유지하면서 다음 문제를 우선순위대로 수정한다.

1. LLM이 생성한 Cypher가 Neo4j에 쓰기 작업을 수행하지 못하도록 다층 방어한다.
2. 사용자 인증 전에 Gemini API 탐색이나 Neo4j 연결이 발생하지 않도록 초기화 순서를 바꾼다.
3. 신규 graphRAG 파이프라인의 장애가 무조건 레거시 ReAct 폴백으로 은폐되지 않도록 예외 정책과 관측성을 개선한다.
4. 호환성 처리를 위해 `TypeError`를 잡는 현재 방식이 실제 내부 결함을 오판하거나 외부 호출을 중복 실행하지 않도록 고친다.
5. 임베딩 재시도, 응답 검증, 모델/벡터 인덱스 호환성을 강화한다.
6. `text_rag.py`의 중복·사장 코드를 제거하고 리소스·설정의 소유권을 명확히 한다.
7. 운영 문서와 테스트를 현재 코드 구성에 맞게 보완한다.

단순 조사나 TODO 주석 추가로 끝내지 말고 코드, 테스트, 문서를 실제로 수정한다.

---

## 2. 현재 기준선

작업 시작 시 아래를 기준으로 삼는다.

- 애플리케이션 진입점: `bot.py`
- graphRAG 정상 경로:

```text
bot.py
  -> agent.generate_response()
  -> agent.synthesize_answer()
  -> tools.orchestrator.gather_graphrag_evidence()
  -> graph + vector + optional authority evidence
  -> one final synthesis LLM call
```

- textRAG 경로: `text_rag.generate_text_rag_response()`
- 공통 리소스: `llm.py`, `graph.py`
- 기존 테스트 명령:

```powershell
python -m unittest tests.test_pipeline -v
```

- 작업 지시서 작성 시점 기준 `tests.test_pipeline`의 79개 테스트가 통과하며 Python 정적 컴파일도 통과한다.
- `IMPLEMENTATION_NOTE.md`에 기록된 evidence schema, Person/Place 분리, authority registry, 호출 cap, 안전한 retrieval status, bounded history 정책은 회귀시키지 않는다.

작업 전 `git status --short`를 확인하고 사용자의 기존 변경을 보존한다. 이 지시서와 무관한 파일을 포맷팅하거나 되돌리지 않는다.

---

## 3. 필수 제약사항

1. Neo4j 원천 데이터와 임베딩 데이터를 삭제하거나 재수집하지 않는다.
2. `.streamlit/secrets.toml`의 실제 값을 출력, 복사, 커밋하지 않는다.
3. `.streamlit/secrets.toml`을 수정하지 않는다. 필요한 키는 별도의 `secrets.toml.example`에 placeholder로만 문서화한다.
4. `tools/evidence.py`의 구조화된 evidence 계약과 source-separated citation 정책을 유지한다.
5. Person ID를 Place endpoint로, Place ID를 Person endpoint로 보낼 수 없다는 기존 불변식을 유지한다.
6. 검색 실패 시 LLM 사전학습 지식으로 corpus/authority 사실을 채우지 않는다.
7. graphRAG와 textRAG의 `::graphRAG`, `::textRAG` 대화 이력 namespace를 유지한다.
8. Streamlit UI를 전면 재설계하지 않는다. 인증 이전 리소스 호출 차단과 사용자 친화 오류 표시에 필요한 변경만 허용한다.
9. 레거시 ReAct 경로는 이번 작업에서 즉시 삭제하지 않는다. 단, 정상 파이프라인 장애를 숨기는 무조건 폴백은 제거한다.
10. 새 의존성은 표준 라이브러리나 현재 의존성으로 해결할 수 없는 경우에만 추가하고, 추가 이유를 완료 보고에 기록한다.

---

## 4. 권장 처리 순서

아래 Phase 순서대로 작업한다. 각 Phase가 끝날 때 관련 테스트를 먼저 통과시킨 후 다음 Phase로 진행한다.

## Phase 0 — 기준선 재확인

다음을 실행하고 결과를 기록한다.

```powershell
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest tests.test_pipeline -v
```

환경에 `pytest`가 없더라도 설치를 작업 선행조건으로 만들지 않는다. 현재 테스트는 stdlib `unittest`로 실행 가능해야 한다.

## Phase 1 — Cypher 읽기 전용 보장

### 현재 문제

`tools/cypher.py`의 두 `GraphCypherQAChain`이 `allow_dangerous_requests=True`로 생성된다. 이 옵션은 라이브러리 사용에 대한 명시적 승인일 뿐 쿼리를 읽기 전용으로 만드는 보안 장치가 아니다. 현재는 생성된 Cypher를 실행 전에 강제 검증하지 않는다.

### 구현 요구사항

1. `validate_read_only_cypher(query: str)` 또는 동일 역할의 순수 함수를 추가한다.
2. 주석과 문자열 리터럴을 고려한 뒤, 최소한 다음 쓰기/관리 동작을 차단한다.

```text
CREATE, MERGE, DELETE, DETACH DELETE, SET, REMOVE, DROP,
ALTER, RENAME, GRANT, DENY, REVOKE, LOAD CSV, FOREACH,
CREATE INDEX/CONSTRAINT, DROP INDEX/CONSTRAINT
```

3. `CALL`은 기본 차단한다. 운영에 꼭 필요한 읽기 전용 프로시저가 존재할 때만 정확한 allowlist를 코드에 명시한다. 임의의 `CALL`, APOC 쓰기 프로시저, 동적 Cypher 실행은 허용하지 않는다.
4. 다중 statement, 세미콜론 뒤 추가 statement, 주석을 이용한 우회, 대소문자/공백 변형을 차단한다.
5. 읽기 절은 명시적인 allowlist 방식으로 허용한다. 예: `MATCH`, `OPTIONAL MATCH`, `WITH`, `WHERE`, `UNWIND`, `RETURN`, `ORDER BY`, `SKIP`, `LIMIT`, 읽기 전용 subquery. 허용 여부가 불명확하면 fail closed 한다.
6. 검증기는 LLM이 생성한 쿼리가 `graph.query()`로 전달되기 전에 반드시 실행되어야 한다. 실행 후 intermediate result를 검사하는 방식은 수용하지 않는다.
7. `GraphCypherQAChain`의 내부 실행 전에 검증을 끼워 넣기 어렵다면 다음 중 안전한 구조를 선택한다.
   - 쿼리 실행을 가로채는 read-only graph adapter/proxy를 주입한다.
   - Cypher 생성 단계와 실행 단계를 분리하고, 생성 → 검증 → `graph.query()` 순서로 명시적으로 실행한다.
8. 두 경로 모두 같은 검증기를 사용해야 한다.
   - 구조화 evidence 경로 `retrieve_graph_evidence()`
   - 레거시 `cypher_qa_safe()` 경로
9. 차단된 쿼리의 원문 전체를 사용자나 synthesis prompt로 보내지 않는다. 서버에는 correlation ID와 차단 사유만 구조화 로그로 남기고 사용자 evidence에는 `invalid_query` 상태만 전달한다.
10. 애플리케이션 방어와 별도로 Neo4j 계정이 읽기 전용이어야 함을 README에 명시한다. 코드가 DB 권한을 생성한다고 가정하지 않는다.
11. 결과 행 수를 제한한다. 생성 query에 `LIMIT`이 없거나 지나치게 큰 경우 안전한 상한을 적용하거나 실행을 거부한다. 권장 기본 상한은 20~50이며 설정 가능하되 무제한은 금지한다.
12. 가능한 범위에서 query timeout도 적용한다. 현재 Neo4j/LangChain API가 지원하지 않으면 그 사실과 배포 계층 대안을 문서화한다.

### 필수 테스트

- 정상 `MATCH ... RETURN ... LIMIT 20` 허용
- 문자열 안의 `"SET"`, `"DELETE"`는 오탐하지 않음
- `CREATE`, `MERGE`, `SET`, `DELETE`, `DETACH DELETE`, `DROP`, `LOAD CSV` 차단
- 소문자, 혼합 대소문자, 줄바꿈, 주석 삽입 우회 차단
- `MATCH ... RETURN ...; MATCH ...` 다중 statement 차단
- 임의 `CALL`과 동적 APOC 실행 차단
- 차단된 쿼리가 mock `graph.query()`에 단 한 번도 전달되지 않음
- 차단 원문/기술 예외가 evidence 및 synthesis prompt에 들어가지 않음

## Phase 2 — 인증 후 지연 초기화와 리소스 수명주기

### 현재 문제

`bot.py`는 인증 전에 `agent`와 `text_rag`를 import한다. import 연쇄 과정에서 Gemini 객체, 임베딩 모델 탐색, Neo4j 연결이 생성된다. 미인증 요청도 외부 API/DB 연결을 유발하고, 외부 서비스 장애가 로그인 화면 표시를 막을 수 있다.

### 구현 요구사항

1. `bot.py`에서 인증 성공 전에는 다음이 import 또는 실행되지 않도록 한다.
   - `agent.generate_response`
   - `text_rag.generate_text_rag_response`
   - `llm`/`embeddings` 생성
   - Neo4j 연결
   - embedding model availability probe
2. 최소한 backend import를 인증 성공 이후로 이동한다. 가능하면 `get_llm()`, `get_embeddings()`, `get_graph()` 같은 명시적 팩토리와 `st.cache_resource`를 사용한다.
3. import 시점의 네트워크 I/O를 제거한다. 모듈 import는 설정 클래스, 함수, 상수 정의만 수행해야 한다.
4. 리소스 초기화 실패를 다음 범주로 구분한다.
   - 설정 누락
   - 인증/권한 오류
   - 네트워크/timeout
   - 모델 또는 인덱스 호환성 오류
5. 사용자에게는 비밀값이나 raw stack trace 없이 지역화된 안전 메시지를 보여준다. 서버 로그에는 correlation ID와 예외 유형을 남긴다.
6. 테스트와 CLI에서 Streamlit `ScriptRunContext`가 없을 수 있다. `utils.get_session_id()`가 `None.session_id`로 실패하지 않도록 명시적 fallback 또는 주입 가능한 session ID를 제공한다.
7. 리소스 캐시는 다음 조건을 만족해야 한다.
   - 동일 설정에서 재사용
   - 초기화 실패 객체는 영구 캐시하지 않음
   - 테스트에서 cache clear 또는 dependency injection 가능
   - 모델명, Neo4j database, index 관련 설정이 바뀌면 잘못된 객체를 재사용하지 않음
8. 공유 비밀번호 비교는 `hmac.compare_digest()` 같은 constant-time 비교를 사용한다. 무차별 대입 방지 자체는 배포 프록시/rate limit 영역임을 문서화한다.

### 필수 테스트

- 인증 실패/미완료 상태에서는 Gemini HTTP 및 Neo4j 생성 mock 호출 0회
- 인증 성공 후 선택한 모드에서 필요한 리소스만 초기화
- 초기화 오류가 Streamlit raw exception으로 노출되지 않음
- `get_script_run_ctx() is None`인 테스트/CLI에서도 session ID 생성 가능
- 동일 설정의 리소스가 재사용되고 실패한 초기화는 재시도 가능

## Phase 3 — 예외 분류, 폴백 정책, 관측성

### 현재 문제

`agent.generate_response()`가 신규 evidence 파이프라인에서 발생한 광범위한 예외를 무시한 후 ReAct 경로로 폴백한다. 설정 오류나 프로그래밍 결함까지 가려지고 동일 질문에 LLM/DB 호출이 중복될 수 있다. `bot.handle_submit()`에는 전체 사용자 요청을 감싸는 안전한 최상위 오류 경계가 없다.

### 구현 요구사항

1. 프로젝트 공통 예외 taxonomy를 작게 정의한다. 예:
   - `ConfigurationError`
   - `TransientProviderError`
   - `UnsafeQueryError`
   - `RetrievalError`
   - `ModelResponseError`
2. 모든 `except Exception: pass`를 기계적으로 없애지는 말고, 정상적으로 degrade해야 하는 위치에는 다음을 명시한다.
   - 잡는 예외 범위
   - 로그 수준
   - correlation ID
   - 사용자에게 반환할 안정된 상태
3. ReAct 폴백은 명시적으로 허용된 일시적/호환성 오류에만 실행한다. 설정 누락, unsafe query, 인증 실패, 코드 결함은 ReAct로 재시도하지 않는다.
4. 신규 파이프라인이 빈 결과를 반환한 경우와 실패한 경우를 구분한다. `no_results`는 오류가 아니며 ReAct 재시도 근거가 아니다.
5. graphRAG와 textRAG 모두에 최상위 요청 오류 경계를 추가한다. 사용자가 보는 메시지는 한국어/영어/중국어의 현재 `effective_language`에 맞춘다.
6. 오류 응답을 Neo4j 대화 이력과 Streamlit 표시 이력에 저장할지 정책을 한 곳에 정의한다. 기술 오류 문자열이나 raw payload는 저장하지 않는다.
7. 로그에 API key, 비밀번호, Neo4j password, 전체 authority payload, 전체 사용자 대화 이력을 남기지 않는다.
8. 폴백 실행 여부와 원인을 테스트 가능한 구조화 필드 또는 로그 이벤트로 남긴다.

### 필수 테스트

- `TransientProviderError`만 정책에 따라 폴백
- 설정 오류, unsafe query, 내부 `TypeError`는 ReAct 재호출하지 않음
- `no_results`는 폴백하지 않고 기존 안전 응답 정책을 따름
- 오류 1건당 correlation ID 1개 생성
- 사용자 메시지와 synthesis prompt에 raw 예외/secret이 포함되지 않음
- textRAG 예외도 Streamlit 에러 페이지 대신 안전 메시지로 변환

## Phase 4 — `TypeError` 기반 호환성 재호출 제거

### 현재 문제

`tools/orchestrator.py`의 `_safe_retrieve()`와 `_call_fetcher()`는 호출 중 발생한 모든 `TypeError`를 함수 인자 수 불일치로 해석해 다른 signature로 다시 호출한다. 함수 내부의 실제 결함을 숨기거나 외부 요청을 중복 실행할 수 있다.

### 구현 요구사항

1. 실행 후 `TypeError`를 잡아 재호출하지 않는다.
2. `inspect.signature()` 또는 등록 시점 adapter를 이용해 2/3-arg retriever와 3/4-arg fetcher 호환성을 호출 전에 결정한다.
3. 기본 production dependency는 하나의 canonical signature로 통일한다.
4. legacy test double 호환 adapter가 필요하면 별도 함수로 제한하고 deprecation 주석을 추가한다.
5. 함수 내부에서 발생한 `TypeError`는 실제 retrieval/fetch 실패로 단 한 번 처리한다.

### 필수 테스트

- legacy signature 함수가 정확히 1회 호출됨
- canonical signature 함수가 정확히 1회 호출됨
- 함수 내부에서 `TypeError` 발생 시 재호출되지 않음
- side-effect가 있는 mock fetcher도 중복 호출되지 않음

## Phase 5 — 임베딩 호출 안정성 및 인덱스 호환성

### 현재 문제

`llm.py`는 앱 시작 시 embedding 후보 모델을 HTTP로 탐색하고, 429에서 고정 60초 sleep을 최대 두 번 수행하며, JSON 구조와 벡터 차원을 충분히 검증하지 않는다. 후보 모델 자동 전환은 기존 Neo4j 임베딩 인덱스와 다른 벡터 공간을 사용할 위험이 있다.

### 구현 요구사항

1. 운영 임베딩 모델은 `GOOGLE_EMBEDDING_MODEL` 같은 명시적 설정으로 고정한다. 자동 후보 전환을 기본 동작으로 두지 않는다.
2. 기존 자동 탐색을 유지해야 한다면 최초 구축/관리자 도구에서만 opt-in으로 사용하고, 챗봇 요청 경로에서는 사용하지 않는다.
3. 임베딩 인덱스와 함께 다음 메타데이터를 검증할 수 있는 구조를 마련한다.
   - embedding model name
   - vector dimension
   - 생성/마이그레이션 버전
4. query embedding 차원이 Neo4j vector index 차원과 다르면 검색 전에 명확한 설정 오류로 중단한다. 자동 재색인하지 않는다.
5. HTTP 호출은 `requests.Session` 또는 주입 가능한 client를 사용하고 다음을 적용한다.
   - connect/read timeout 분리
   - `Retry-After` 우선
   - bounded exponential backoff + jitter
   - 전체 요청 시간 예산
   - 429/5xx만 제한적으로 재시도
   - 4xx 인증/요청 오류는 무의미하게 재시도하지 않음
6. Streamlit 대화 요청을 60초 고정 sleep으로 장시간 차단하지 않는다. 배치 임베딩 생성과 실시간 query embedding의 재시도 정책을 분리한다.
7. 응답 검증:
   - HTTP status 및 JSON content type
   - JSON schema/key 존재
   - 숫자 벡터인지 확인
   - 빈 벡터 거부
   - batch 요청 수와 반환 수 일치
   - 기대 차원 일치
8. `embed_documents([])`는 네트워크 호출 없이 빈 리스트를 반환하거나 명시적 validation error를 내도록 일관된 계약을 정한다.
9. `create_embeddings.py`에서 빈 정제 텍스트, 부분 batch 실패, 재실행 안전성을 처리한다. 모델/차원 메타가 다른 기존 임베딩 위에 조용히 이어 쓰지 않는다.

### 필수 테스트

- 429 `Retry-After` 처리 및 최대 재시도/시간 예산 준수
- 400/401/403은 재시도하지 않음
- malformed JSON, 잘못된 content type, 누락 key, 비숫자/빈 vector 차단
- batch 입력/출력 개수 불일치 차단
- 벡터 차원 불일치가 Neo4j 검색 전에 검출됨
- 설정된 모델과 인덱스 모델 불일치 시 명확한 `ConfigurationError`
- 빈 입력의 네트워크 호출 0회

## Phase 6 — `text_rag.py` 중복 제거 및 설정 통합

### 현재 문제

`text_rag.py`와 `tools/vector.py`에 언어별 인덱스 설정, retriever cache, retrieval query 계열이 중복되어 있다. `text_rag.py` 내부에도 동일 이름의 설정/캐시가 중복 정의되어 변경 시 어느 코드가 실행되는지 불명확해질 수 있다.

### 구현 요구사항

1. 언어별 `index_name`, `text_property`, `embedding_property`를 단일 설정 모듈 또는 명확한 공통 상수로 통합한다.
2. textRAG의 lightweight retrieval query와 graphRAG의 enriched retrieval query는 목적이 다르므로 함수 자체는 분리하되 이름과 소유 모듈을 명확히 한다.
3. 사용되지 않는 중복 `INDEX_BY_LANG`, `_retrievers`, dead query builder를 제거한다.
4. textRAG는 계속 다음 범위를 지킨다.
   - Entry 본문의 의미 검색
   - Entry–Work 관계는 출처 메타데이터에만 사용
   - graph relationship reasoning을 수행한다고 주장하지 않음
5. retriever 캐시가 전역 dict로 남는다면 thread-safety와 cache invalidation 이유를 문서화한다. 가능하면 Phase 2의 리소스 팩토리/cache 정책에 통합한다.
6. 반환 타입을 일관되게 한다. 레거시 `get_poetry_plot()`이 dict를 반환하고 tool이 문자열을 기대하는 등 경계가 불명확한 부분을 테스트와 type hint로 정리한다.

### 필수 테스트

- ko/en/zh가 정확한 index/property 조합으로 라우팅
- textRAG는 lightweight metadata query 사용
- graphRAG vector evidence는 enriched metadata query 사용
- 각 언어 retriever가 동일 설정에서 한 번만 생성
- 기존 textRAG wording 및 history namespace 테스트 통과

## Phase 7 — 문서, 설정 예제, 전체 검증

### 문서 보완

1. `README.adoc` 또는 별도의 운영 문서에 다음을 추가한다.
   - 실제 아키텍처와 graphRAG/textRAG 차이
   - 설치 및 `unittest` 실행법
   - Streamlit 실행법
   - Neo4j 읽기 전용 계정 요구사항
   - 필수 vector index 이름과 embedding model/dimension 계약
   - authority 외부 호출과 timeout/cap 개요
   - 대표 장애 진단 방법
2. 현재 README가 언급하지만 저장소에 없는 `.streamlit/secrets.toml.example`을 생성한다. 실제 값 없이 다음 placeholder만 포함한다.

```toml
APP_PASSWORD = "replace-me"
GOOGLE_API_KEY = "replace-me"
GOOGLE_MODEL = "replace-me"
GOOGLE_EMBEDDING_MODEL = "replace-me"
NEO4J_URI = "neo4j+s://replace-me"
NEO4J_USERNAME = "read-only-user"
NEO4J_PASSWORD = "replace-me"
NEO4J_DATABASE = "neo4j"
```

필요한 신규 timeout/cap/index 설정이 있으면 placeholder와 설명을 추가한다. 실제 `.streamlit/secrets.toml`은 읽거나 변경하지 않는다.

3. `requirements.txt`의 모든 직접 dependency에 재현 가능한 버전 정책을 적용한다. 최소한 현재 unpinned `requests`를 검토한다. `pytest`를 필수 dependency로 추가할 필요는 없으며, 추가한다면 개발용 requirements로 분리한다.
4. `IMPLEMENTATION_NOTE.md`에 이번 변경 사항을 새 섹션으로 추가한다.

### 최종 검증

다음을 모두 실행한다.

```powershell
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest discover -s tests -v
```

가능한 환경에서는 다음 smoke test도 수행한다.

```powershell
streamlit run bot.py
```

라이브 Gemini/Neo4j가 필요한 검증을 실행할 수 없으면 실패로 위장하지 말고, 실행하지 못한 항목과 필요한 환경을 완료 보고에 정확히 기록한다.

---

## 5. 전체 수용 기준

아래 조건이 모두 충족되어야 작업 완료로 본다.

1. 생성된 Cypher는 실행 전에 read-only validator를 통과해야 하며 쓰기/관리/임의 procedure query는 DB에 도달하지 않는다.
2. Neo4j 계정이 실수로 과도한 권한을 가져도 애플리케이션 계층 검증이 동작하며, 문서는 별도의 읽기 전용 DB 권한도 요구한다.
3. 인증 전 Gemini/embedding/Neo4j 네트워크 호출이 0회다.
4. import만으로 네트워크 요청이나 DB 연결이 생성되지 않는다.
5. 신규 graphRAG 오류가 `except Exception: pass`로 사라지지 않는다.
6. ReAct 폴백은 분류된 허용 오류에만 실행되며 중복 외부 호출을 만들지 않는다.
7. signature 호환성 때문에 `TypeError`를 실행 후 잡아 재호출하는 코드가 없다.
8. 실시간 embedding 요청이 고정 60초 sleep으로 Streamlit 세션을 장시간 차단하지 않는다.
9. embedding model/dimension과 Neo4j index 호환성이 검색 전에 검증된다.
10. `text_rag.py`의 중복 설정과 사장 코드가 제거된다.
11. 오류 메시지, 로그, 대화 이력에 secret이나 raw 외부 payload가 노출되지 않는다.
12. 기존 79개 테스트와 새 테스트가 모두 통과한다. 테스트 수가 늘어야 하며 기존 테스트 삭제로 통과시키지 않는다.
13. 실제 라이브 서비스가 없어도 핵심 보안·초기화·재시도 로직은 mock 기반으로 검증된다.
14. 문서와 secrets example이 실제 코드가 요구하는 설정과 일치한다.

---

## 6. 권장 파일 범위

주요 수정 대상:

- `bot.py`
- `agent.py`
- `llm.py`
- `graph.py`
- `utils.py`
- `text_rag.py`
- `tools/cypher.py`
- `tools/vector.py`
- `tools/orchestrator.py`
- `tools/synthesis.py` (오류 상태/메시지 변경이 필요한 경우)
- `create_embeddings.py`
- `tests/test_pipeline.py`
- 신규 focused test 파일
- `README.adoc`
- `.streamlit/secrets.toml.example`
- `IMPLEMENTATION_NOTE.md`
- `requirements.txt` 또는 별도 개발 requirements

필요하면 다음과 같은 작은 모듈을 추가할 수 있다.

- `tools/cypher_safety.py`
- `resources.py` 또는 `services.py`
- `errors.py`
- `rag_config.py`

모듈을 추가할 때 순환 import를 만들지 말고, Streamlit 의존성이 domain/retrieval 계층으로 확산되지 않도록 한다.

---

## 7. 완료 보고 형식

작업 완료 시 다음 형식으로 보고한다.

1. 변경 파일 목록과 파일별 핵심 변경
2. Cypher read-only 보장 방식과 차단 범위
3. 인증 전/후 리소스 초기화 흐름
4. 오류 분류와 ReAct 폴백 조건
5. embedding 모델/차원 계약 및 migration 필요 여부
6. 추가·변경한 설정 키
7. 실행한 테스트 명령, 총 테스트 수, 결과
8. 실행하지 못한 라이브 검증과 이유
9. 남은 운영 작업
   - Neo4j read-only role 적용
   - reverse proxy rate limit
   - 배포 환경 secret 등록
   - 기존 인덱스 model/dimension 확인 또는 재색인 필요성

완료 보고에서 “모두 안전함”처럼 포괄적으로 단정하지 말고, 코드 계층에서 보장한 것과 배포 계층에서 별도로 해야 할 것을 구분한다.

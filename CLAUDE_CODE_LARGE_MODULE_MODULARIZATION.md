# Claude Code 작업 지시서: 시화총림 챗봇 대형 모듈 분리 및 확장 경계 정비

## 0. 이 지시서의 목적

현재 시화총림 챗봇의 사용자 동작, 검색 결과, 출처 정책, 보안 정책을 유지하면서 대형 모듈에 혼재된 책임을 분리한다.

이번 작업은 후속 기능을 구현하는 작업이 아니다. 특히 향후 검토 중인 다음 두 데이터 보완 작업은 아직 상세 요구사항과 Neo4j 데이터 모델이 확정되지 않은 상태라고 가정한다.

- 시화총림 도메인 용어집 보완
- 관련 연구·논문 데이터 보완

따라서 이번 작업에서는 미래 데이터 모델을 추측하여 `Glossary`, `ResearchAuthor`, `ResearchWork`, `ResearchPassage` 같은 노드·관계·필드·Evidence 종류를 미리 추가하지 않는다. 대신 현재 구현만으로 검증할 수 있는 공통 실행 경계와 확장 가능한 인터페이스를 만든다.

단순 조사, 설계 문서 작성, TODO 주석 추가로 끝내지 말고 실제 코드 분리, 호환 facade, 테스트, 문서화를 완료한다.

---

## 1. 작업 전 필수 확인

### 1.1 작업 트리 보호

작업 시작 전에 다음을 실행한다.

```powershell
git status --short
```

- 기존 변경사항은 사용자의 작업으로 간주하고 보존한다.
- 이 작업과 무관한 파일을 포맷팅하거나 되돌리지 않는다.
- `git reset --hard`, `git checkout --`, 광범위한 파일 삭제를 사용하지 않는다.
- 사용자가 명시적으로 요청하지 않은 commit, push, branch 생성은 하지 않는다.

### 1.2 비밀정보 보호

- `.streamlit/secrets.toml`을 열어 실제 값을 출력하지 않는다.
- `.streamlit/secrets.toml`을 수정하거나 커밋하지 않는다.
- 설정 문서가 필요하면 `.streamlit/secrets.toml.example`의 placeholder만 사용한다.
- 로그, 테스트 출력, 완료 보고에 API key, Neo4j password, 원문 connection URI를 포함하지 않는다.

### 1.3 기준선 검증

현재 프로젝트는 이 지시서 작성 시점에 다음 테스트가 통과한다.

```text
Ran 551 tests
OK
```

작업 시작 시 실제 기준선을 다시 확인한다.

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest discover -s tests -p 'test_*.py'
```

`pytest` 설치를 작업 선행조건으로 만들지 않는다. 기존 stdlib `unittest` 실행을 반드시 유지한다.

기준선 테스트가 작업 시작 전부터 실패한다면:

1. 실패 목록과 재현 명령을 기록한다.
2. 이 작업과 무관한 실패를 임의로 고치지 않는다.
3. 기존 실패와 이번 변경으로 발생한 실패를 완료 보고에서 명확히 구분한다.

---

## 2. 현재 아키텍처 기준선

### 2.1 사용자 요청 경로

```text
bot.py
  -> 언어 해석 및 모드 선택
  -> agent.generate_response()                    [graphRAG]
       -> agent.synthesize_answer()
       -> tools.orchestrator.gather_graphrag_evidence()
            -> tools.cypher.retrieve_graph_evidence()
            -> tools.vector.retrieve_sihwa_evidence()
            -> optional external authority enrichment
       -> tools.synthesis.format_evidence_for_prompt()
       -> final synthesis LLM
       -> tools.synthesis.build_citations()
       -> tools.answer_renderer.assemble_final_answer()

  -> text_rag.generate_text_rag_response()         [textRAG]
       -> language-specific Entry vector retrieval
       -> answer LLM
       -> shared deterministic citation/answer assembly
```

### 2.2 현재 책임 집중도

이 지시서 작성 시점의 대략적인 파일 규모는 다음과 같다. 줄 수 자체가 결함은 아니지만, 아래 파일들은 서로 다른 변경 이유를 가진 코드가 한 모듈에 섞여 있다.

| 파일 | 대략적 줄 수 | 현재 혼재된 책임 |
|---|---:|---|
| `agent.py` | 871 | 일반 대화, 정상 graphRAG, 합성 호출, 이력, 예외 정책, ReAct 폴백 |
| `tools/evidence.py` | 1,154 | 모델, ID 검증, 병합, vector 변환, graph row 변환 |
| `tools/synthesis.py` | 1,072 | 정책, 이력 직렬화, 근거 포맷, 예산, 인용 생성 |
| `tools/cypher.py` | 1,049 | 거대 프롬프트, 체인 구성, 실행, 재시도, evidence 변환, 순위 질의 |
| `tools/external_authority.py` | 1,054 | source registry, ID 검증, HTTP, cache, source별 parser, 출력 변환 |
| `tools/orchestrator.py` | 659 | intent, 설정, retriever 호출, 상태 정규화, authority 선별/실행 |
| `tools/vector.py` | 373 | retrieval Cypher, prompt, retriever cache, 레거시 답변, evidence 검색 |
| `text_rag.py` | 272 | retriever, prompt, history, chain, citation 조립 |

### 2.3 이미 책임이 비교적 잘 분리된 모듈

다음 모듈은 현재도 비교적 단일 책임에 가깝다. 명확한 이유 없이 다시 합치거나 불필요하게 이동하지 않는다.

- `tools/language_policy.py`
- `tools/cypher_safety.py`
- `tools/graph_intent.py`
- `tools/vectorrag_prompt.py`
- `tools/answer_renderer.py`
- `rag_config.py`
- `errors.py`
- `mode_labels.py`

필요하면 새 계층에서 import할 수 있으나 기존 import 경로 호환성을 유지한다.

---

## 3. 핵심 목표

### 3.1 기능 보존형 리팩터링

이번 작업의 원칙은 다음과 같다.

```text
동작 변경 없음
데이터 변경 없음
프롬프트 의미 변경 없음
호출 수 변경 없음
검색 순서 변경 없음
출처 정책 변경 없음
모듈 책임과 의존성 방향만 변경
```

코드 이동 과정에서 발견한 결함이나 최적화 후보는 구현하지 말고 완료 보고의 `후속 작업 후보`에 기록한다. 단, 코드가 이동되지 않으면 테스트가 불가능한 명백한 import 결함처럼 리팩터링 자체를 막는 문제는 최소 범위로 수정하고 이유를 기록한다.

### 3.2 명시적인 요청 처리 단계

리팩터링 후 정상 graphRAG의 단계가 코드 구조에서 다음처럼 식별 가능해야 한다.

```text
request context
  -> retrieval orchestration
  -> graph/vector/authority evidence
  -> evidence formatting and budgeting
  -> one final synthesis boundary
  -> deterministic citation building
  -> deterministic final answer assembly
  -> history persistence
```

각 단계의 구현 위치, 입력, 출력, 외부 부작용이 명확해야 한다.

### 3.3 단방향 의존성

권장 의존성 방향은 다음과 같다.

```text
presentation/UI
       |
       v
application pipelines
       |
       +------> retrieval/enrichment
       |              |
       v              v
synthesis/rendering   domain contracts
       |              ^
       +--------------+

infrastructure clients are injected into the application/retrieval layer.
domain contracts never import infrastructure or Streamlit.
```

다음 방향의 의존성은 금지한다.

- domain model -> Streamlit
- domain model -> Neo4j/LangChain/Gemini/requests
- retrieval -> Streamlit UI rendering
- citation renderer -> LLM invocation
- infrastructure -> application pipeline
- legacy ReAct implementation -> 정상 graphRAG pipeline 내부

### 3.4 확장 가능하되 미래 스키마를 추측하지 않는 구조

미래의 근거 소스를 쉽게 추가할 수 있는 작은 인터페이스는 허용한다. 예:

```python
class EvidenceRetriever(Protocol):
    source_key: str

    def retrieve(self, request: RetrievalRequest) -> Evidence:
        ...
```

하지만 이번 작업에서 다음을 하지 않는다.

- 아직 확정되지 않은 신규 `Evidence.kind` 값 등록
- 미래 Neo4j label/relationship/property 상수 추가
- 빈 `glossary.py`, `research.py` placeholder 모듈 추가
- 용어집·논문을 가정한 필드가 포함된 dataclass 추가
- 현재 사용되지 않는 범용 plugin framework나 DI framework 추가

확장성은 현재 graph/vector/external 구현을 같은 인터페이스로 표현할 수 있는 수준까지만 만든다.

---

## 4. 절대 보존해야 할 불변식

### 4.1 사용자 기능

1. Streamlit 진입점은 계속 `bot.py`이며 다음 명령이 유효해야 한다.

```powershell
streamlit run bot.py
```

2. `graphRAG`, `textRAG` 모드 이름과 UI 선택 동작을 바꾸지 않는다.
3. 인증 성공 전에는 Gemini/Neo4j backend가 초기화되지 않는 현재 lazy import 정책을 유지한다.
4. 사용자 화면에는 원문 질문을 표시하고, backend에는 언어 제어 문구가 제거된 `question_text`를 전달한다.
5. 한국어·영어·중국어 질문 언어와 응답 언어 분리 정책을 유지한다.
6. control-only 언어 변경 요청은 RAG backend를 호출하지 않는다.
7. `::graphRAG`, `::textRAG` 대화 이력 namespace를 유지한다.
8. 정상 경로와 ReAct 폴백이 같은 메시지를 이중 저장하지 않도록 현재 persistence ownership을 유지한다.

### 4.2 근거와 출처

1. `Evidence`, `Entity`, `Provenance`, `NodeReference`의 현재 직렬화 의미를 유지한다.
2. 모든 내부 node ID는 기존 prefix registry와 shape validation을 거친다.
3. 이름만으로 서로 다른 node를 병합하지 않는다.
4. Person과 Place external authority namespace를 섞지 않는다.
5. `textChi`, `textKor`, `textEng`, `descEng` 값은 변형하지 않는다.
6. graph/vector/external 근거를 서로 다른 source로 유지한다.
7. 모델이 쓴 Sources 섹션은 제거하고 코드가 검증된 Sources를 한 번만 조립한다.
8. 본문에서 실제로 사용한 graph node만 현재 정책대로 `poetrytalks wikidata` 그룹에 포함한다.
9. `https://poetrytalks.org/<valid-node-id>` 외의 내부 링크를 추측하지 않는다.
10. 외부 authority link는 registry에서 검증된 URL만 사용한다.
11. link-only source를 fetched fact로 표현하지 않는다.
12. graph와 external 정보가 충돌할 때 조용히 병합하지 않는다.

### 4.3 검색과 보안

1. 모든 LLM 생성 Cypher는 기존 `tools/cypher_safety.py`의 read-only 검증과 안전 graph wrapper를 반드시 거친다.
2. `allow_dangerous_requests=True`를 읽기 전용 보장으로 오해하지 않는다.
3. forbidden keyword, disallowed CALL, multi-statement 차단 정책을 약화하지 않는다.
4. 현재 query row cap과 read retry 정책을 유지한다.
5. 검색 실패 시 사용자에게 raw Cypher, stack trace, secret, 외부 응답 본문을 노출하지 않는다.
6. 검색 실패를 모델의 사전학습 지식으로 메우지 않는다.
7. graph/vector가 모두 일시적으로 실패하고 외부 근거도 없을 때 LLM을 호출하지 않는 현재 정책을 유지한다.

### 4.4 호출 행위

이번 작업에서는 현재 호출 행위를 의도적으로 유지한다.

- graph와 vector 검색의 현재 순서를 바꾸지 않는다.
- 검색을 병렬화하지 않는다.
- top-k, cap, timeout, retry 횟수를 바꾸지 않는다.
- LLM 모델명, embedding 모델명, temperature를 바꾸지 않는다.
- LLM 입력/출력 token limit을 새로 정하지 않는다.
- cache key, TTL, cache 성공/실패 정책을 바꾸지 않는다.
- ReAct iteration 수를 바꾸지 않는다.

현재 구조화 graph retrieval의 `GraphCypherQAChain`이 최종적으로 사용하지 않는 QA prose까지 생성하는 것은 확인된 최적화 후보이다. 그러나 호출 제거는 비용과 결과 timing을 바꾸므로 이번 구조 분리와 동시에 수행하지 않는다. 별도 후속 작업으로 기록한다.

---

## 5. 호환성 요구사항

### 5.1 기존 공개 진입점 유지

최종 구현 위치가 바뀌더라도 다음 import와 함수 호출은 유지한다.

```python
from agent import generate_response, synthesize_answer
from text_rag import generate_text_rag_response
from tools.orchestrator import gather_graphrag_evidence
from tools.cypher import (
    cypher_qa_safe,
    retrieve_graph_evidence,
    retrieve_role_ranking_evidence,
)
from tools.vector import get_poetry_plot, retrieve_sihwa_evidence
from tools.synthesis import format_evidence_for_prompt, build_citations
from tools.answer_renderer import assemble_final_answer
```

위 목록은 최소 목록이다. 작업 시작 시 `tests/`, 루트 모듈, `solutions/`를 제외한 현재 애플리케이션 코드에서 import되는 이름을 `rg` 또는 AST로 조사하고 호환 목록을 완성한다.

기존 모듈은 새 구현을 re-export하거나 얇게 위임하는 compatibility facade로 남길 수 있다.

### 5.2 함수 시그니처와 반환 형태

- 기존 positional/keyword 호출을 깨지 않는다.
- `gather_graphrag_evidence()`가 반환하는 기존 dict key를 유지한다.
- 기존 `Evidence.to_dict()` 결과의 key와 값 의미를 유지한다.
- `generate_response()`와 `generate_text_rag_response()`는 계속 최종 문자열을 반환한다.
- 테스트가 의도적으로 import하는 내부 helper도 조사하여, 이동이 필요한 경우 기존 위치에서 re-export한다.

호환 shim은 부작용이나 별도 상태를 가지면 안 된다. 새 구현으로 단순 위임하거나 re-export만 해야 한다.

---

## 6. 권장 목표 구조

정확한 디렉터리명은 기존 import 충돌과 작업 결과에 따라 조정할 수 있다. 다만 책임 경계는 아래와 동등해야 한다.

```text
chatbot/
  domain/
    evidence_models.py
    node_identity.py
    evidence_merge.py
    retrieval_models.py

  retrieval/
    graph_prompt.py
    graph_generated_query.py
    graph_deterministic_queries.py
    graph_evidence_mapper.py
    vector_query.py
    vector_retriever.py

  authority/
    models.py
    registry.py
    validators.py
    parsers.py               # 필요하면 source군별 추가 분리
    client.py
    cache.py
    enrichment.py

  application/
    evidence_orchestrator.py
    retrieval_policy.py
    graphrag_pipeline.py
    vectorrag_pipeline.py
    history_service.py

  synthesis/
    source_policy.py
    history_format.py
    evidence_format.py
    prompt_budget.py
    prompt.py
    citations.py

  infrastructure/
    settings.py
    llm_provider.py
    graph_provider.py

  legacy/
    react_agent.py
    react_prompt.py
    react_tools.py
```

기존 루트와 `tools/` 모듈은 점진적 호환 facade로 유지한다.

```text
agent.py                    -> 정상 pipeline/legacy facade
text_rag.py                 -> vectorRAG pipeline facade
tools/evidence.py           -> domain evidence re-export facade
tools/cypher.py             -> graph retrieval re-export facade
tools/vector.py             -> vector retrieval re-export facade
tools/orchestrator.py       -> application orchestrator re-export facade
tools/synthesis.py          -> synthesis helpers re-export facade
tools/external_authority.py -> authority package re-export facade
```

모든 제안 파일을 기계적으로 만들 필요는 없다. 두 모듈이 짧고 동일한 변경 이유를 갖는다면 합칠 수 있다. 반대로 source parser가 너무 커지면 여러 파일로 분리할 수 있다.

피해야 할 결과:

- `common.py`, `helpers.py`, `utils2.py` 같은 새로운 대형 잡동사니 모듈
- 순환 import를 해결하기 위한 함수 내부 무분별한 lazy import
- facade와 새 구현에 동일 상수/registry가 복제되는 구조
- 파일만 이동하고 전역 객체·session state 결합은 그대로인 구조
- 사용되지 않는 추상 클래스 계층

---

## 7. 단계별 구현 작업

각 Phase가 끝날 때 관련 단위 테스트와 전체 테스트를 실행한다. 여러 대형 모듈을 한 번에 이동하지 않는다.

## Phase 0 — API 및 동작 인벤토리

코드 수정 전에 다음을 조사하여 작업 메모에 기록한다.

1. 각 대상 모듈이 export하는 함수, 클래스, 상수, module-level 객체
2. 애플리케이션과 테스트가 import하는 이름
3. module import 시 생성되는 LLM/Neo4j/HTTP/cache 객체
4. `st.session_state`를 직접 읽는 위치
5. graphRAG와 textRAG의 history 읽기/쓰기 소유자
6. LLM invocation 경계와 현재 호출 순서
7. Neo4j invocation 경계와 safe wrapper 적용 위치
8. source/citation 생성 경계

다음과 같은 표를 작업 메모에 만든다.

| 현재 symbol | 현재 위치 | 호출자 | 부작용 | 목표 위치 | facade 필요 여부 |
|---|---|---|---|---|---|

이 표를 만들지 않고 바로 대규모 파일 이동을 시작하지 않는다.

## Phase 1 — package skeleton과 의존성 규칙

1. 선택한 신규 package에 필요한 `__init__.py`를 추가한다.
2. package 계층과 각 계층의 허용 dependency를 짧은 `README.md` 또는 module docstring에 기록한다.
3. 새 runtime dependency나 DI framework를 추가하지 않는다.
4. Python 버전과 현재 type annotation 스타일을 유지한다.
5. import cycle을 피하기 위해 domain contract가 최하위 계층이 되도록 한다.

이 Phase에서는 대규모 코드를 아직 이동하지 않아도 된다.

## Phase 2 — `tools/evidence.py` 분리

다음 책임을 분리한다.

### 2.1 모델

- `Entity`
- `Provenance`
- `NodeReference`
- `Evidence`

dataclass field, 기본값, `to_dict()` 결과를 변경하지 않는다.

### 2.2 내부 node identity

- `POETRYTALKS_BASE_URL`
- node ID prefix registry
- `split_node_id()`
- `is_valid_node_id()`
- `node_type_for_id()`
- `poetrytalks_url()`
- `make_node_reference()`

prefix, 정규식, fail-closed 동작을 변경하지 않는다.

### 2.3 병합과 수집

- entity de-duplication
- node reference de-duplication
- `collect_entities()`
- `collect_node_references()`

이름만으로 병합하지 않는 정책과 internal ID 우선순위를 유지한다.

### 2.4 vector document 변환

- vector metadata -> Entity
- vector metadata -> NodeReference
- document -> document/evidence/provenance
- `docs_to_evidence()`

### 2.5 graph row 변환

- alias 해석
- recursive row walk
- graph row -> Entity/NodeReference/Provenance/Evidence
- walk depth/item cap

`tools/evidence.py`에는 기존 symbol re-export facade를 남긴다. 현재 테스트가 underscore helper를 import한다면 호환이 필요한지 확인하고, 테스트만을 위해 로직을 복제하지 말고 단일 구현을 re-export한다.

Phase 완료 조건:

- domain evidence 하위 모듈은 `streamlit`, `langchain`, `neo4j`, `requests`를 import하지 않는다.
- 기존 evidence 관련 테스트가 그대로 통과한다.
- `Evidence.to_dict()` snapshot이 이동 전후 동일하다.

## Phase 3 — `tools/synthesis.py` 분리

다음 책임을 분리한다.

### 3.1 source/conflict policy

- `SYNTHESIS_SYSTEM_RULES`
- source 권위와 conflict 규칙
- 원문 무변형 규칙
- 출처는 system-owned라는 규칙

이번 작업에서는 규칙 문구의 의미를 다시 작성하거나 축약하지 않는다. 프롬프트 token 최적화는 후속 작업이다.

### 3.2 대화 이력 직렬화

- history message 필터
- message/total character cap
- `serialize_chat_history()`

### 3.3 evidence formatting

- graph block
- vector block
- external block
- retrieval status
- authority coverage
- 언어별 source text field 순서

현재 block 순서, per-block cap, total cap을 유지한다.

### 3.4 citation building

- graph provenance citation
- named Poetry Talks node citation
- external fetched/link-only citation
- citation de-duplication
- 언어별 label

현재 `tools/answer_renderer.py`와 책임이 중복되는 부분을 점검하되, 본문 링크 삽입 및 최종 조립의 단일 소유권은 `answer_renderer` 측에 유지한다.

### 3.5 확장 경계

새로운 source를 나중에 formatter/citation builder에 등록할 수 있는 작은 dispatch 구조를 허용한다. 단, 현재 `graph`, `vector`, `external`만 등록하고 미래 source 이름은 추가하지 않는다.

Phase 완료 조건:

- 신규 synthesis 하위 모듈은 `streamlit`, Neo4j client, LLM client를 import하지 않는다.
- 동일 evidence 입력에 대해 formatted evidence와 citation 결과가 이동 전후 동일하다.
- deterministic source 관련 기존 테스트가 모두 통과한다.

## Phase 4 — `tools/cypher.py` 분리

다음 책임을 분리한다.

### 4.1 Cypher prompt 및 schema 규칙

- `CYPHER_GENERATION_TEMPLATE`
- Cypher schema 설명
- 예시와 query generation 규칙
- prompt construction

프롬프트의 literal brace escaping을 유지한다. PromptTemplate이 실제 변수로 인식해야 하는 placeholder와 Cypher map literal을 혼동하지 않는다.

### 4.2 generated graph retrieval

- GraphCypherQAChain construction
- generated query invocation
- intermediate step extraction
- graph row -> Evidence 호출
- recoverable/non-recoverable retry 분기

### 4.3 deterministic graph query

- role ranking query builder와 실행
- 현재 등록된 role cue
- parameter validation

### 4.4 safe execution boundary

- 모든 graph query가 `tools/cypher_safety.py`의 동일한 validator/wrapper를 통과하게 유지한다.
- safe wrapper를 새 모듈에 복제하지 않는다.
- unsafe query를 재시도하지 않는 현재 분류를 유지한다.

### 4.5 레거시 graph QA

- `cypher_qa_safe()`는 정상 evidence retrieval과 구분하되 삭제하지 않는다.
- 정상 경로와 레거시 경로가 공유할 수 있는 것은 prompt/schema/safety까지이며, user-facing prose 생성 책임은 섞지 않는다.

Phase 완료 조건:

- 기존 Cypher safety 테스트가 모두 통과한다.
- mocked graph 기준으로 query 호출 횟수와 순서가 이동 전후 동일하다.
- graph evidence의 row/provenance 결과가 동일하다.
- `tools/cypher.py`의 기존 import 경로가 계속 동작한다.

## Phase 5 — `tools/vector.py`와 `text_rag.py` 경계 분리

### 5.1 공통 Entry vector retrieval

다음을 공통 retrieval service로 이동한다.

- 언어별 index config 조회
- retrieval query 생성
- Neo4jVector 생성과 retriever cache
- document retrieval
- retrieved document -> Evidence 변환

`rag_config.py`를 index 설정의 단일 source of truth로 유지한다.

### 5.2 정상 graphRAG용 vector retriever

- `retrieve_sihwa_evidence()`는 구조화 Evidence만 반환한다.
- 최종 user-facing prose를 생성하지 않는다.

### 5.3 textRAG pipeline

- textRAG의 direct retrieval-and-answer 동작은 유지한다.
- prompt 작성, history wrapper, final citation assembly를 application pipeline으로 구분한다.
- graphRAG와 textRAG가 공유하는 것은 retriever와 evidence/citation helper이며 history namespace는 공유하지 않는다.

### 5.4 레거시 ReAct vector tool

- `get_poetry_plot()`은 호환을 위해 유지한다.
- 정상 graphRAG가 이 함수를 직접 사용하지 않는 현재 원칙을 유지한다.
- 레거시의 user-facing answer 생성과 정상 retrieval-only 경계를 명확히 분리한다.

Phase 완료 조건:

- 질문 언어가 index 선택에 사용되고 응답 언어가 출력/인용 순서에 사용되는 분리가 유지된다.
- 언어별 retriever cache key와 top-k가 바뀌지 않는다.
- graphRAG용 vector retrieval은 LLM 답변을 생성하지 않는다.
- textRAG의 현재 LLM 호출 수와 history 저장 동작이 유지된다.

## Phase 6 — `tools/external_authority.py` 분리

다음 책임을 분리한다.

### 6.1 source model과 registry

- `AuthoritySourceConfig`
- capability 상수
- `AUTHORITY_REGISTRY`
- property/id key mapping
- source resolution

registry는 하나의 source of truth만 갖는다. 기존 facade에 registry 사본을 만들지 않는다.

### 6.2 ID validation과 URL construction

- source별 fullmatch validator
- Person/Place namespace 구분
- request/citation URL의 안전한 ID encoding

### 6.3 HTTP client와 cache

- timeout/content-type/status/response-size 검증
- retry 정책
- 성공만 저장하는 TTL/bounded cache
- cache clear test helper

정책 값과 cache key를 변경하지 않는다.

### 6.4 source parser

- raw HTTP payload에서 allowlisted field만 추출
- source별 parser
- response normalization

parser가 많아 한 파일이 다시 커지면 source군별 모듈로 나눈다. raw payload를 Evidence나 synthesis prompt로 전달하지 않는다.

### 6.5 enrichment adapter

- fetched result -> external Evidence
- link-only reference
- unavailable/unsupported 상태

Phase 완료 조건:

- 외부 HTTP 없이 parser를 독립 테스트할 수 있다.
- source별 node type과 ID validator가 이동 전후 동일하다.
- 성공 cache, 실패 미cache 정책이 유지된다.
- 실제 HTTP 응답 본문을 로그에 남기지 않는다.
- 기존 external authority 관련 테스트가 모두 통과한다.

## Phase 7 — `tools/orchestrator.py` 분리

다음 책임을 분리한다.

### 7.1 retrieval/authority intent policy

- authority intent 판별
- ranking authority intent
- response/question language 사용 위치

현재 heuristic을 다시 설계하지 않고 그대로 이동한다.

### 7.2 retriever invocation adapter

- signature introspection
- 2/3 argument 하위 호환 dispatch
- retrieval error -> user-safe status
- `TypeError` body failure와 arity mismatch 구분

호출을 실패 후 다른 arity로 재시도하는 과거 동작을 되살리지 않는다.

### 7.3 authority enrichment service

- Entity 수집
- Person/Place 분리
- entity/source cap
- fetch/link-only 처리
- coverage 기록

### 7.4 orchestration

- graph retrieval
- vector retrieval
- Entity 병합
- optional authority enrichment
- 최종 evidence bundle 조립

orchestrator는 다음을 하지 않아야 한다.

- Streamlit UI 접근
- user-facing answer 작성
- citation markdown 생성
- source별 HTTP payload parsing
- graph row alias 해석
- 미래 source에 대한 hard-coded 분기

Phase 완료 조건:

- `gather_graphrag_evidence()` 반환 key와 값 의미가 동일하다.
- 현재 graph -> vector -> authority 순서를 유지한다.
- injection을 사용하는 기존 orchestration 테스트가 모두 통과한다.
- retrieval 실패가 다른 근거의 사용 가능성을 제거하지 않는다.

## Phase 8 — `agent.py` 정상 pipeline과 legacy 분리

### 8.1 일반 대화

- `general_chat()`과 관련 prompt/chain을 정상 graphRAG orchestration과 분리한다.
- 사용자 언어 directive 동작은 유지한다.

### 8.2 정상 graphRAG application pipeline

다음을 한 application service에 모은다.

```text
bounded history load
-> gather evidence
-> total retrieval failure short circuit
-> format evidence
-> final synthesis LLM
-> deterministic citation assembly
-> successful final answer persistence
```

이 service는 `st.session_state`를 직접 읽지 않고 호출자로부터 현재 request context를 받는 방향을 우선한다. 단, 기존 facade의 하위 호환 fallback은 유지할 수 있다.

### 8.3 ReAct legacy 격리

- tool definitions
- ReAct prompt
- AgentExecutor
- RunnableWithMessageHistory
- iteration-limit fallback

을 `legacy` 영역으로 이동한다.

정상 pipeline은 legacy 구현을 import하지 않는다. 최상위 `generate_response()` 오류 정책에서 명시적으로 허용된 경우에만 legacy facade를 호출한다.

### 8.4 예외 정책

기존 `ConfigurationError`, `TransientProviderError`, `UnsafeCypherError`, unexpected programming error 분류를 유지한다.

- transient provider 오류만 현재 정책에 따라 ReAct fallback 가능
- configuration 오류는 fallback 금지
- unsafe query는 fallback 금지
- programming error는 fallback 금지

Phase 완료 조건:

- 정상 graphRAG와 ReAct 경로의 코드 소유권이 분리된다.
- 정상 pipeline 성공 시 history가 정확히 한 번 저장된다.
- fallback path에서도 history가 정확히 한 경로에서만 저장된다.
- 예외별 fallback 여부와 사용자 메시지가 이동 전후 동일하다.
- `agent.py`는 public facade와 composition root 역할 중심으로 축소된다.

## Phase 9 — 리소스와 설정 경계

이 Phase는 import-time 동작을 무리하게 바꾸기 위한 것이 아니라 외부 리소스 소유권을 명확히 하기 위한 것이다.

1. chat LLM, embeddings, Neo4j graph를 생성하는 위치를 infrastructure/composition root로 명확히 한다.
2. application/retrieval service가 테스트에서 fake client를 받을 수 있도록 callable 또는 constructor injection을 제공한다.
3. 새 DI framework를 도입하지 않는다.
4. 인증 이전 backend lazy import 보장을 유지한다.
5. 테스트와 CLI에서 pure domain/synthesis 모듈을 import할 때 Streamlit secrets나 Neo4j 연결이 필요하지 않아야 한다.
6. 현재 module-level resource를 호환 때문에 남긴다면 facade에 그 이유와 제거 조건을 명시한다.
7. graph read client와 history write client 분리는 이번 작업에서 실제 credential/data 변경 없이 인터페이스 경계만 만들 수 있다. 실제 계정 분리는 후속 배포 작업으로 남긴다.

Phase 완료 조건:

- pure module import가 외부 연결을 발생시키지 않는다.
- 인증 전 외부 client 초기화 0회 테스트가 유지된다.
- 같은 resource가 예기치 않게 중복 생성되지 않는다.

## Phase 10 — facade 정리와 문서화

1. 모든 기존 import 경로가 동작하는지 검사한다.
2. facade에는 로직 복제 없이 re-export/위임만 남긴다.
3. deprecated 경로를 이번 작업에서 삭제하지 않는다.
4. 새 package 구조와 의존성 방향을 README 또는 별도 architecture 문서에 기록한다.
5. `IMPLEMENTATION_NOTE.md`를 계속 비대하게 만들지 말고 이번 작업 전용 구현 보고서를 작성한다.

권장 파일명:

```text
MODULARIZATION_IMPLEMENTATION_NOTE.md
```

---

## 8. 이번 작업의 명시적 Non-Goals

다음은 발견하더라도 이번 작업에서 구현하지 않는다.

### 8.1 데이터 및 스키마

- Neo4j node/relationship/property 추가·삭제·변경
- JSONL/CSV 원천 데이터 수정
- 데이터 재수집·재적재·정규화
- 기존 vector index 생성·삭제·재임베딩
- CriticalTerm 중복 병합 또는 정의 추가
- 용어집용 신규 label/relationship/index 설계
- 연구자·논문·구절용 신규 label/relationship/index 설계

### 8.2 신규 기능

- 질문 해석 결과 UI
- CriticalTerm entity resolution
- 원문+확장어 multi-query 검색
- 연구논문 ingestion, OCR, chunking, embedding
- Wiki -> chatbot query parameter 문맥 전달
- 새로운 외부 authority source
- 새로운 사용자 인증/SSO

### 8.3 비용·성능 변경

- 사용하지 않는 Graph QA LLM 호출 제거
- graph/vector 병렬화
- streaming 출력
- context caching
- Redis/shared cache
- model 변경
- top-k 조정
- evidence budget 변경
- prompt 축약
- EC2/ALB/CloudFront 배포
- rate limiting 또는 token quota

### 8.4 정책 변경

- graph/vector/external 간 권위 순서 변경
- 출처 표시 형식 변경
- 언어 판별 heuristic 변경
- 로마자 표기 정책 변경
- 실패 시 사전학습 지식 사용
- ReAct fallback 제거

---

## 9. 미래 작업을 위한 확장성 요구사항

후행 데이터 모델은 미정이므로 구체 필드를 만들지 않는다. 대신 다음 질문에 `예`라고 답할 수 있는 구조를 만든다.

1. 새 retriever를 기존 graph/vector 코드를 수정하지 않고 application layer에 등록할 수 있는가?
2. 새 Evidence source formatter를 기존 formatter의 대규모 조건문 수정 없이 추가할 수 있는가?
3. 새 citation strategy를 기존 graph citation 로직을 변경하지 않고 추가할 수 있는가?
4. 질문 전처리 결과를 raw question과 별도 객체로 pipeline에 전달할 수 있는가?
5. 특정 retriever에만 별도 timeout/top-k/budget을 줄 수 있는가?
6. 미래 Neo4j label을 domain core의 전역 enum 수정 없이 mapper에서 다룰 수 있는가?
7. Streamlit 없이 pipeline 단위 테스트를 실행할 수 있는가?

구현되지 않은 미래 기능을 위한 빈 클래스나 speculative field보다 다음과 같은 현재형 인터페이스를 선호한다.

```python
@dataclass(frozen=True)
class RetrievalRequest:
    question: str
    question_language: str
    response_language: str
    history_text: str | None = None


class EvidenceRetriever(Protocol):
    source_key: str

    def retrieve(self, request: RetrievalRequest) -> Evidence:
        ...
```

필드명은 달라질 수 있으나 현재 실제로 존재하는 정보만 포함한다.

---

## 10. 필수 테스트

### 10.1 전체 회귀

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools chatbot tests
python -m unittest discover -s tests -p 'test_*.py'
```

신규 package 이름이 `chatbot`이 아니라면 compile 대상 명령을 실제 이름에 맞춘다.

### 10.2 import 호환성

최소한 다음을 테스트한다.

- 기존 public import 성공
- facade symbol과 실제 구현 symbol이 동일하거나 동일 동작으로 위임
- pure domain/evidence/synthesis import 시 Streamlit/Neo4j/Gemini client 생성 0회
- 인증 전 `agent`, `text_rag`, `llm`, `graph` backend import/초기화 0회
- module import cycle 없음

### 10.3 데이터 계약 parity

대표 fixture에 대해 이동 전후 다음 결과가 동일해야 한다.

- `Evidence.to_dict()`
- Entity merge 결과와 순서
- NodeReference merge 결과와 순서
- vector document -> Evidence
- graph rows -> Evidence
- `format_evidence_for_prompt()`
- `build_citations()`
- `assemble_final_answer()`

### 10.4 호출 parity

mock을 사용해 다음을 검증한다.

- 정상 graphRAG의 graph/vector/authority/LLM 호출 횟수
- graph -> vector -> authority의 현재 순서
- authority cap과 source cap
- external success cache와 failure 미cache
- textRAG의 embedding/retrieval/answer 호출 횟수
- transient 오류에서만 ReAct fallback
- configuration/unsafe/programming 오류에서는 fallback 0회
- 정상/fallback 경로별 history write 정확히 1회

### 10.5 보안 회귀

- LLM 생성 Cypher가 safe graph wrapper를 우회하지 않음
- forbidden keyword와 disallowed CALL 차단
- raw query/exception/secret가 Evidence와 사용자 응답에 포함되지 않음
- 외부 parser가 allowlisted field만 반환
- Person/Place authority namespace 불변

### 10.6 언어·출처 회귀

- question language와 response language 분리
- 언어별 vector index 선택
- 언어별 원문 필드 표시 순서
- 원문 값 byte-for-byte 보존
- Sources header와 label 언어
- 실제 본문 사용 node만 내부 source group에 포함
- 동명이인 node를 이름만으로 병합하지 않음

---

## 11. 품질 기준

### 11.1 모듈 크기

줄 수를 기계적인 성공 지표로 사용하지 않는다. 다만 다음 신호는 재검토한다.

- 새 실행 로직 모듈이 다시 500행 이상이 됨
- 서로 다른 변경 이유를 가진 함수가 한 파일에 남음
- 한 모듈이 UI, DB, LLM, citation 중 세 가지 이상을 직접 다룸
- 한 함수가 retrieval, synthesis, persistence를 동시에 수행

긴 정적 prompt나 registry는 실행 로직과 분리되어 있고 단일 source of truth라면 길이가 길 수 있다.

### 11.2 문서화

각 새 모듈의 docstring은 다음을 짧게 설명한다.

- 책임
- 입력/출력
- 허용된 dependency
- 외부 부작용 여부
- 기존 facade 위치

코드를 그대로 반복 설명하는 장황한 주석은 추가하지 않는다.

### 11.3 타입과 오류

- 기존 dataclass 기반 모델을 우선 재사용한다.
- 새 Pydantic/DI/config dependency를 추가하지 않는다.
- `except Exception: pass`를 새로 만들지 않는다.
- degrade가 필요한 경계에서는 안정된 status와 correlation ID 정책을 유지한다.
- 타입 호환을 위해 실제 함수 body의 `TypeError`를 arity mismatch로 오판하지 않는다.

### 11.4 중복 금지

다음 항목은 정확히 하나의 source of truth만 가져야 한다.

- node ID prefix와 URL 규칙
- 언어별 vector index 설정
- Evidence dataclass
- authority registry
- source/conflict policy
- citation label
- retrieval cap
- history budget
- safe Cypher validator

---

## 12. 진행 중 중단하고 보고해야 하는 조건

다음 상황에서는 추측으로 밀어붙이지 말고 현황과 선택지를 보고한다.

1. 기존 테스트가 리팩터링 전부터 재현 불가능하게 실패하는 경우
2. 사용자의 미커밋 변경과 동일 코드 영역에서 충돌하는 경우
3. 호환 facade로 해결할 수 없고 public 함수 시그니처 변경이 필요한 경우
4. 기존 호출 수·검색 순서·출처 결과를 바꾸지 않으면 분리가 불가능한 경우
5. 새 runtime dependency가 반드시 필요한 경우
6. Neo4j schema 또는 데이터 변경 없이는 테스트를 통과할 수 없는 경우
7. 미래 용어집/논문 모델을 결정해야만 진행할 수 있는 것처럼 보이는 경우

7번 상황에서는 미래 모델을 만들지 말고 현재 source를 일반적인 retriever/formatter/citation interface로 감싸는 최소 범위까지 진행한다.

---

## 13. 완료 조건

다음을 모두 만족해야 완료로 간주한다.

1. 대형 모듈의 책임이 목표 계층으로 실제 이동되어 있다.
2. 기존 import 경로는 compatibility facade를 통해 유지된다.
3. 정상 graphRAG, textRAG, ReAct legacy 경로가 구조적으로 구분된다.
4. domain/evidence/synthesis의 순수 로직은 Streamlit/Neo4j/Gemini 없이 import하고 테스트할 수 있다.
5. 모든 graph query가 기존 read-only safety boundary를 유지한다.
6. 현재 LLM/Neo4j/HTTP 호출 횟수와 순서가 의도적으로 변경되지 않았다.
7. 현재 evidence, citation, 언어, history, fallback 정책이 유지된다.
8. 전체 기존 테스트와 신규 테스트가 통과한다.
9. compile 검사가 통과한다.
10. `.streamlit/secrets.toml`과 `neo4j_data_import/`가 변경되지 않았다.
11. 미래 용어집·논문 node/relationship/property/Evidence kind가 추가되지 않았다.
12. 신규 거대 `utils` 모듈이나 순환 import가 생기지 않았다.
13. 구현 보고서가 작성되어 있다.

---

## 14. Deliverables

1. 책임별로 분리된 application/domain/retrieval/synthesis/authority/infrastructure/legacy 코드
2. 기존 import를 보존하는 얇은 facade
3. import side-effect, contract parity, call parity, history ownership을 검증하는 테스트
4. 갱신된 architecture 문서 또는 README 섹션
5. `MODULARIZATION_IMPLEMENTATION_NOTE.md`

구현 보고서에는 다음을 포함한다.

```text
- 변경한 파일과 새 파일
- 현재 -> 신규 symbol 이동표
- 최종 package dependency 방향
- 유지한 compatibility facade 목록
- 제거하거나 변경하지 않은 기존 동작
- baseline/최종 테스트 수와 실행 결과
- compile 결과
- LLM/Neo4j/HTTP call parity 확인 결과
- secrets/data 파일 무변경 확인
- 발견했지만 범위 밖이라 구현하지 않은 결함과 최적화 후보
- 후행 용어집/논문 작업이 사용할 수 있는 확장 지점
- 아직 남아 있는 기술 부채
```

---

## 15. 최종 작업 원칙 요약

```text
이번 작업은 현재 동작을 보존하는 구조 리팩터링이다.

미래 용어집과 연구논문 모델을 추측하지 않는다.
현재 데이터와 Neo4j schema를 변경하지 않는다.
기능 개선과 비용 최적화를 동시에 수행하지 않는다.
기존 안전성·언어·출처·이력 계약을 약화하지 않는다.
한 번에 한 책임을 이동하고 매 단계 테스트한다.
기존 import 경로는 facade로 보존한다.
확장성은 현재 구현으로 검증 가능한 작은 인터페이스까지만 만든다.
```

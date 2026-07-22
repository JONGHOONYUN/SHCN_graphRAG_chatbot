# Claude Code 작업 지시서: 결정론적 Sources 조립과 Provenance 정합성 수정

## 1. 작업 목적

graphRAG 챗봇의 실제 테스트에서 확인된 citation 누락과 잘못된 provenance 표기를 수정한다.

테스트 질문과 관찰된 응답:

```text
Input:
Which woman is mentioned the most in Sihwa ch'ongnim?

Observed answer:
Hŏ Ch'ohŭi (허초희) is the woman mentioned most frequently in Sihwa
Ch'ongnim, with 10 mentions. Hŏ Nansŏrhŏn (허난설헌), who shares the
same Wikidata ID (Q464558) as Hŏ Ch'ohŭi, is mentioned 8 times.

Sources:
Sihwa Ch'ongnim Graph: A Storyteller’s Miscellany (패관잡기) > Entry 31 (?)
Sihwa Ch'ongnim Graph: Compendium of Remarks on Poetry (시화총림) > Entry 28 (?)
Sihwa Ch'ongnim Graph: Compendium of Remarks on Poetry (시화총림) > Entry 0 (?)
Sihwa Ch'ongnim Graph: Topical Discourses of Chibong (지봉유설) > Entry 94 (?)
```

수정 목표:

1. `build_citations()`가 만든 citation을 LLM이 생략·축약·변형할 수 없도록 Sources 섹션을 코드에서 결정론적으로 조립한다.
2. `entry_id=None`을 `(?)`로 출력하지 않는다.
3. 유효하지 않은 `Entry 0` 또는 누락된 position을 정상 provenance처럼 노출하지 않는다.
4. Work/Entry/node 링크 포맷을 읽기 쉽게 정리한다.
5. 동일 provenance와 동일 node citation을 안정적인 내부 ID 기준으로 중복 제거한다.
6. 본문에서 언급한 graph entity에 대한 Poetry Talks 링크도 프롬프트 순응성에만 의존하지 않도록 보장한다.
7. Neo4j 내부 Person ID가 다른 인물을 외부 식별자 공유만으로 병합하지 않는다.

단순히 synthesis prompt의 명령을 더 강하게 만드는 것으로 완료하지 않는다. 이번 결함은 프롬프트만으로 출력 구조를 강제할 수 없다는 점이 실제 응답으로 확인된 사례다.

---

## 2. 반드시 유지할 식별·계산 정책

### 2.1 Neo4j 내부 ID가 개체 식별의 기준

이 프로젝트에서는 Neo4j source data의 내부 node ID를 canonical identity로 사용한다.

- `P553` 허초희와 `P1227` 허난설헌은 서로 다른 `Person` 노드다.
- 두 노드가 `idWikidata=Q464558` 등 일부 외부 식별자를 공유하더라도 자동으로 동일인 처리하거나 집계값을 합산하지 않는다.
- 외부 ID는 authority 조회와 참고용 속성이지 내부 Person 병합 키가 아니다.
- 서로 다른 내부 ID가 같은 외부 ID를 가질 때는 필요하면 “두 graph record가 같은 외부 식별자를 공유한다”고 설명할 수 있지만, 이를 동일 인물의 증거라고 단정하지 않는다.

현재 `tools.evidence._should_merge()`가 두 Entity 모두 `node_id`를 가지고 있을 때 내부 ID가 같아야 병합하는 불변식을 유지하고 명시적 회귀 테스트를 추가한다.

필수 회귀 사례:

```python
Entity(node_id="P553", authority_ids={"wikidata": "Q464558"})
Entity(node_id="P1227", authority_ids={"wikidata": "Q464558"})
```

위 두 Entity는 `merge_entities()`와 `collect_entities()` 결과에서 반드시 2개로 유지되어야 한다.

### 2.2 질문별 계산식을 하드코딩하지 않음

`Which woman is mentioned the most ...?` 전용 계산 함수나 고정 Cypher template을 추가하지 않는다.

- 질문 해석, `count`, `DISTINCT`, `ORDER BY`, `LIMIT` 조합은 기존처럼 LLM의 동적 Cypher 생성 역할로 유지한다.
- `mentioned`의 계산 공식을 애플리케이션 코드에 고정하지 않는다.
- 단, LLM이 생성한 집계 결과에는 답변과 citation에 필요한 Neo4j 내부 ID가 함께 반환되도록 기존 Cypher prompt의 일반 규칙을 보완할 수 있다.
- 외부 ID가 같다는 이유로 `GROUP BY idWikidata`, 합산, alias canonicalization을 자동 적용하지 않는다.
- 생성 Cypher의 read-only 검증, LIMIT 강제, 안전 상태 처리 등 기존 보안 하드닝은 유지한다.

이 작업의 핵심은 계산 방식을 바꾸는 것이 아니라, 이미 검색된 evidence를 정확하고 재현 가능한 citation으로 출력하는 것이다.

---

## 3. 현재 코드에서 확인된 원인

### 3.1 Sources가 LLM 출력에 종속됨

현재 흐름:

```text
build_citations(evidence)
  -> suggested_citations 문자열 생성
  -> synthesis prompt에 전달
  -> Gemini에게 그대로 복사하라고 지시
  -> Gemini가 작성한 output을 그대로 반환
```

`build_citations()` 자체가 완전한 Markdown URL을 생성해도 최종 Gemini가 bullet을 생략하거나 URL을 제거할 수 있다. 기존 테스트는 citation builder와 prompt 문구를 검증하지만 실제 최종 조립 경계를 보장하지 않는다.

### 3.2 `(?)` 생성 경로

`tools.evidence._linked_id(None)`이 `"?"`를 반환하고 vector provenance 생성과 언어별 재렌더링에서 이를 괄호로 감싼다.

```text
Work name > Entry 31 (?)
```

관찰된 friendly breadcrumb 형식은 `provenance_from_graph_row()`의 `Graph: ...` 형식보다 vector provenance 경로와 일치한다. 따라서 graph provenance만 수정해서는 안 된다.

확인 대상:

- `tools/evidence.py::document_to_parts()`
- `tools/evidence.py::_linked_id()`
- `tools/synthesis.py::_rebuild_vector_prov_label()`
- `tools/synthesis.py::build_citations()`
- `tools/vector.py`의 retrieval metadata contract

### 3.3 `Entry 0`의 원인은 아직 확정되지 않음

현재 코드에서 `None`을 숫자 0으로 바꾸는 명시적 변환은 확인되지 않았다. 로컬 JSONL source data에서도 정상 Entry position 0을 전제로 하지 않는다.

가능성:

1. 라이브 Neo4j와 로컬 export의 차이
2. vector retriever가 실제 `entry_position=0`을 반환
3. LLM이 pre-built citation을 변형

원인을 추정으로 고정하지 말고 LLM 호출 직전의 normalized evidence/citation과 LLM raw output을 분리해 테스트·진단할 수 있게 한다.

---

## 4. 작업 범위

주요 파일:

- `agent.py`
- `tools/synthesis.py`
- `tools/evidence.py`
- `tools/vector.py`
- `tools/cypher.py` — 집계 결과 ID 반환에 필요한 일반 prompt 보완만 허용
- `tests/test_sources_language_and_urls.py`
- `tests/test_poetrytalks_wikidata_group.py`
- 신규 focused test 파일
- `IMPLEMENTATION_NOTE.md`

필요하면 작은 전용 모듈을 추가할 수 있다.

- `tools/citation_renderer.py`
- `tools/answer_renderer.py`

외부 authority registry, Neo4j 원천 데이터, Streamlit UI, embedding model/index는 이번 작업 범위가 아니다.

---

## 5. 구현 작업

## Phase 0 — 기준선과 재현 fixture 확보

작업 전 다음을 실행한다.

```powershell
git status --short
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest discover -s tests -v
```

작업 지시서 작성 시점 기준 전체 216개 테스트가 통과한다. 기존 테스트를 삭제하거나 assertion을 약화해 통과시키지 않는다.

실제 응답을 축약한 mock evidence fixture를 만든다.

- `P553`, `P1227`
- 공유 외부 ID `Q464558`
- vector provenance 4건
- 하나 이상의 `entry_id=None`
- 하나 이상의 `entry_position=0`
- 유효한 Work/Entry ID가 있는 정상 provenance
- LLM mock output이 Sources를 완전히 생략하는 경우
- LLM mock output이 URL을 제거한 잘못된 Sources를 생성하는 경우

라이브 Gemini나 Neo4지 없이 재현 가능해야 한다.

## Phase 1 — Sources 섹션을 LLM 밖에서 결정론적으로 조립

### 필수 설계

1. synthesis LLM에는 답변 본문만 작성하도록 지시한다.
2. LLM이 Sources/References/출처/来源 섹션을 작성하지 않도록 prompt를 단순화한다.
3. 그래도 LLM이 임의 Sources를 생성할 수 있으므로 최종 조립 전에 model-generated Sources 영역을 안전하게 제거한다.
4. `build_citations()` 결과를 `render_sources_section(citations, language)` 같은 순수 함수로 렌더링한다.
5. 최종 응답은 코드에서 다음 순서로 조립한다.

```text
sanitized answer body

localized Sources header
exact deterministic citation bullets
```

6. Sources header는 현재 언어 정책을 유지한다.

```text
ko -> 출처
en -> Sources
zh -> 来源
```

7. `poetrytalks wikidata`는 프로젝트에서 정한 고유 그룹명으로 세 언어 모두 그대로 유지한다.
8. citation bullet의 `- `, `[ID](URL)`, URL, 순서를 LLM이 수정할 경로를 제거한다.
9. citation이 없으면 빈 Sources header를 붙이지 않는다.
10. 최종 조립이 끝난 완성 응답만 Streamlit UI와 `::graphRAG` 대화 이력에 저장한다.
11. retrieval failure처럼 LLM을 호출하지 않는 안전 응답에는 불필요한 Sources를 붙이지 않는다.

### model-generated Sources 제거 요구사항

- 영어 `Sources`, `References`
- 한국어 `출처`, `참고문헌`
- 중국어 `来源`, `參考資料`, `参考资料`
- Markdown header 깊이 차이(`#`~`######`)

본문 중간에 일반 단어로 등장하는 “source”까지 잘라내지 않는다. line-start Markdown section header로 식별하고, 제거 규칙을 순수 함수와 테스트로 고정한다.

### 필수 테스트

- LLM이 Sources를 전혀 출력하지 않아도 최종 응답에 모든 deterministic bullet이 있음
- LLM이 `poetrytalks wikidata` bullet을 삭제해도 최종 응답에는 복원됨
- LLM이 가짜 URL을 포함한 Sources를 작성하면 해당 섹션은 제거되고 검증된 URL만 남음
- LLM이 완전한 Sources를 작성해도 중복 섹션은 하나만 남음
- ko/en/zh header가 정확함
- citation이 없을 때 불필요한 빈 header 없음
- 대화 이력에는 조립 완료된 최종 응답이 저장됨

## Phase 2 — Provenance 유효성 검증과 placeholder 제거

### ID 정책

1. `_linked_id(None)`이 사용자용 `"?"`를 반환하지 않도록 계약을 변경한다.
2. ID가 없으면 다음 중 문맥에 맞는 하나를 반환한다.
   - 빈 값/`None`
   - ID 부분 자체 생략
3. citation에서 `(?)`, `(None)`, `[?](...)`, `/None`을 절대 만들지 않는다.
4. Poetry Talks 링크는 기존 `is_valid_node_id()`와 `poetrytalks_url()`을 통과한 내부 node ID로만 생성한다.
5. Wikidata Q-ID, AKS Encyclopedia ID 등 외부 식별자를 Poetry Talks node ID로 오인하지 않는 기존 방어를 유지한다.

### vector provenance 정책

`document_to_parts()`와 `_rebuild_vector_prov_label()`에서 다음 단계로 렌더링한다.

1. 유효한 `entry_id`가 있으면 Entry ID 링크를 표시한다.
2. 유효한 `entry_position > 0`이면 position을 표시한다.
3. position이 없거나 0/음수/비정상 타입이면 position 표기를 생략한다.
4. `entry_id`는 없고 유효한 `work_id`만 있으면 work-only citation으로 낮춘다.
5. Work/Entry 어느 쪽에도 유효한 내부 ID가 없으면 user-facing graph breadcrumb를 만들지 않는다. 진단 로그만 남긴다.
6. `entry_position`만 있고 `entry_id`가 없는 경우 이를 완전한 Entry provenance처럼 가장하지 않는다. 정책적으로 position만 표시할 수는 있지만 `?` ID를 추가하지 않는다.
7. vector retrieval query는 정상적으로 `node.id`, `node.position`, source Work ID를 projection하므로, 누락 시 metadata contract violation을 correlation ID와 함께 로그로 남긴다. raw 본문·secret은 로그에 남기지 않는다.

### graph provenance 정책

- graph row에 entry ID가 없다는 이유만으로 Person 집계 provenance까지 모두 버리지 않는다.
- graph aggregation은 `person_id` 등 실제로 반환된 안정적인 내부 ID를 provenance로 유지한다.
- 존재하지 않는 Entry breadcrumb를 임의 생성하지 않는다.
- Cypher prompt에는 집계/순위 query에서도 결과 대상의 내부 node ID를 함께 반환하라는 일반 규칙을 추가할 수 있다.
- 특정 테스트 질문의 이름이나 계산식을 prompt에 하드코딩하지 않는다.

### 필수 테스트

- `entry_id=None`, `entry_position=31`에서 `(?)`가 출력되지 않음
- `entry_position=None`에서 `Entry None`이 출력되지 않음
- `entry_position=0` 또는 음수에서 `Entry 0`, `Entry -1`이 출력되지 않음
- 유효한 `E031`과 position 31은 정상 링크로 출력
- work ID만 있으면 유효한 work-only citation 생성
- 유효한 graph `person_id=P553`은 Entry ID가 없어도 보존
- 외부 ID만 있는 row는 Poetry Talks provenance를 만들지 않음

## Phase 3 — Citation 포맷과 중복 제거

### 권장 표시 형식

연속 괄호 대신 slash와 square-link를 사용한다.

```markdown
- Sihwa Ch'ongnim Graph: A Storyteller's Miscellany / 패관잡기 [B023] > Entry 31 [E031]
```

실제 Markdown에서는 `[B023]`, `[E031]`을 검증된 Poetry Talks URL 링크로 만든다.

요구사항:

1. 영어·중국어 답변의 bilingual work name 정책은 유지한다.
2. `English title (한국어명) ([B023](...))`와 같은 연속 괄호를 만들지 않는다.
3. ko/en/zh별 Work 이름 선택 규칙을 유지한다.
4. apostrophe와 Unicode romanization을 변경하지 않는다.

### 중복 제거 키

문자열 전체 일치만으로 중복을 제거하지 말고 가능한 경우 구조화된 키를 사용한다.

우선순위:

```text
source_type + entry_id
source_type + poem_or_critique_id
source_type + entity_id
source_type + work_id (work-only citation)
verified source_url
```

주의:

- 서로 다른 내부 node ID는 외부 ID가 같아도 중복으로 제거하지 않는다.
- Entry 28과 Entry 0을 동일 항목이라고 추정해 합치지 않는다.
- 내부 ID가 없으면 `(work_name, position)`만으로 강한 동일성 판정을 하지 않는다. 가능한 경우 citation을 생략하고 진단한다.
- graph와 vector가 같은 node를 반환할 때 `poetrytalks wikidata` node bullet은 한 번만 출력한다.
- graph breadcrumb와 vector breadcrumb는 의미가 다르면 별도 그룹/라인으로 유지할 수 있지만 동일한 잘못된 breadcrumb를 반복하지 않는다.

### 필수 테스트

- 같은 Entry ID의 vector provenance가 여러 번 들어와도 한 breadcrumb
- `P553`와 `P1227`은 공유 Wikidata ID가 있어도 각각 citation 유지
- 동일 node의 `poetrytalks wikidata` bullet은 한 번만 출력
- work-only와 entry-specific citation의 관계가 명확하며 잘못 collapse되지 않음
- 포맷에 `)(`, `(?)`, `Entry 0`이 없음

## Phase 4 — 본문 entity 링크의 코드 수준 보장

결정론적 Sources 조립만으로는 본문의 다음 표현이 자동 링크되지 않는다.

```text
Hŏ Ch'ohŭi (허초희)
```

본문 링크 요구사항을 유지하려면 LLM prompt만 강화하지 말고 검증 가능한 후처리 또는 구조화 출력 계약을 사용한다.

허용 설계:

1. LLM이 `answer_body`와 `referenced_node_ids`를 구조화 형태로 반환하게 하고, 코드가 ID를 evidence allowlist와 대조한 뒤 링크를 렌더링한다.
2. 또는 entity name placeholder를 사용해 LLM 출력 후 검증된 placeholder만 Markdown 링크로 변환한다.
3. 또는 evidence 내에서 이름이 하나의 node ID에만 유일하게 매핑되는 경우 첫 언급에 검증된 node link를 결정론적으로 추가하고, 모호하면 별도의 `Referenced graph nodes` 줄을 코드로 붙인다.

Claude Code는 기존 LangChain/Gemini 호환성과 변경량을 고려해 하나를 선택할 수 있지만 다음 조건을 만족해야 한다.

- evidence에 없는 ID는 링크하지 않음
- 외부 Q-ID를 Poetry Talks URL로 링크하지 않음
- `P553`와 `P1227`을 하나로 합치지 않음
- 동명이인이면 임의로 한 ID를 선택하지 않음
- 이미 존재하는 Markdown 링크, code block, URL을 깨뜨리지 않음
- 링크 생성 실패가 답변 전체 실패로 이어지지 않음
- 본문 링크와 Sources citation이 같은 node URL을 사용함

본문 링크가 제품 요구사항이 아니라 Sources에서만 클릭 가능하면 충분하다고 판단할 경우 임의로 요구사항을 삭제하지 말고, 코드와 문서의 기존 rule 9를 Sources-only 정책으로 변경하는 결정을 완료 보고에 명시한다. 이번 작업의 기본값은 본문 링크 유지다.

### 필수 테스트

- `Hŏ Ch'ohŭi`/`허초희`가 evidence의 `P553` 링크를 사용
- `Hŏ Nansŏrhŏn`/`허난설헌`이 `P1227` 링크를 사용
- 두 node가 같은 `Q464558`을 공유해도 서로 다른 Poetry Talks URL 사용
- evidence에 없는 인물 이름은 자동 링크하지 않음
- 동명이인 두 node가 있으면 임의 링크하지 않음
- 기존 Markdown 링크를 이중 링크하지 않음

## Phase 5 — 진단 가능성과 최종 통합 테스트

### 진단 정보

한 요청에서 다음 단계를 구분해 확인할 수 있도록 correlation ID 기반 debug 로그를 추가한다.

```text
normalized evidence IDs/positions
deterministic citation count and node IDs
LLM body returned 여부
model-generated Sources 제거 여부
final Sources bullet count
invalid provenance skipped count
```

금지 사항:

- API key, password
- 전체 source text
- 전체 대화 이력
- raw authority payload
- 사용자에게 raw exception 노출

### 통합 회귀 테스트

실제 테스트 질문을 이름 그대로 production 분기문에 하드코딩하지 말고, mock evidence 기반으로 다음 결과 계약을 검증한다.

```text
answer body
  - P553 허초희: 10
  - P1227 허난설헌: 8
  - 서로 다른 graph Person node로 유지

Sources
  - P553 Poetry Talks link
  - P1227 Poetry Talks link
  - 유효한 Work/Entry breadcrumb만 표시
  - poetrytalks wikidata group 누락 없음
  - (?), Entry 0, fake URL 없음
```

LLM mock은 의도적으로 다음과 같이 불순응하게 만든다.

- Sources 완전 생략
- bullet collapse
- Markdown URL 제거
- 잘못된 Sources header 생성

그럼에도 최종 반환값이 결정론적 계약을 지켜야 한다.

---

## 6. 비목표

이번 작업에서 하지 않는다.

- `P553`과 `P1227` 병합
- 외부 식별자 기준 Person canonicalization
- 10과 8을 합산
- 특정 질문 전용 Cypher/계산 함수 추가
- Neo4j source data 수정
- vector index 재생성
- 외부 authority endpoint 추가
- UI 전면 개편
- 기존 Cypher safety, auth lazy initialization, embedding hardening 되돌리기

---

## 7. 전체 수용 기준

다음이 모두 충족되어야 완료다.

1. 최종 Sources는 LLM 출력이 아니라 코드가 조립한다.
2. LLM이 citation 명령을 전부 무시해도 Sources가 완전하게 출력된다.
3. `poetrytalks wikidata` bullet은 검증된 내부 node ID별로 생성된다.
4. 사용자 응답 어디에도 `(?)`, `Entry None`, `Entry 0`이 정상 provenance로 노출되지 않는다.
5. 유효한 ID가 없는 provenance는 가짜 링크를 생성하지 않는다.
6. Work/Entry citation은 연속 이중 괄호 없이 읽기 쉬운 형식이다.
7. 동일한 내부 node citation은 중복되지 않는다.
8. `P553`과 `P1227`은 공유 외부 ID와 무관하게 별도 Entity/citation으로 유지된다.
9. 질문별 집계 계산은 계속 LLM이 동적으로 생성하며 해당 테스트 질문 전용 분기문이 없다.
10. 본문 entity link가 코드 수준에서 보장되거나, 정책 변경 시 그 이유와 영향이 명시적으로 문서화된다.
11. 기존 216개 테스트와 신규 테스트가 모두 통과한다.
12. 라이브 Gemini/Neo4j 없이 핵심 최종 조립 경계를 검증할 수 있다.

---

## 8. 최종 검증 명령

```powershell
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest discover -s tests -v
git diff --check
git status --short
```

가능하면 인증된 로컬 환경에서 다음 질문으로 수동 smoke test를 수행한다.

```text
Which woman is mentioned the most in Sihwa ch'ongnim?
```

라이브 환경을 사용할 수 없으면 실행하지 못한 이유를 정확히 보고하고 mock 통합 테스트 결과로 대체한다.

---

## 9. 구현 완료 보고 형식

Claude Code는 완료 시 다음을 보고한다.

1. 변경 파일과 파일별 핵심 변경
2. LLM body와 deterministic Sources의 최종 조립 흐름
3. model-generated Sources 제거 방식
4. missing/invalid provenance 처리 정책
5. citation 구조화 중복 제거 키
6. 본문 entity link 보장 방식
7. `P553`/`P1227` 분리 불변식 검증 결과
8. 추가한 테스트 수와 전체 테스트 결과
9. 라이브 smoke test 실행 여부와 실제 출력
10. 남아 있는 데이터 또는 배포 계층 제한

코드에서 보장한 내용과 Gemini의 비결정적 동작에 남겨둔 내용을 구분해 설명한다.

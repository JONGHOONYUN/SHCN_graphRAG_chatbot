# Claude Code 작업 지시서: 모든 Neo4j 노드의 응답 링크·Sources 커버리지 완성

## 1. 목표

챗봇 최종 응답에서 실제로 언급되거나 근거로 사용된 Neo4j 노드의 내부 `<ID>` 값을 검증한 뒤 다음 URL 형식으로 자동 제공한다.

```text
<Poetry Talks base URL>/<Neo4j node ID>
```

예시:

```markdown
[P553](https://poetrytalks.org/P553)
[E031](https://poetrytalks.org/E031)
[CT017](https://poetrytalks.org/CT017)
```

Person과 Place에 국한하지 않고 다음 모든 node class를 지원한다.

```text
Work, Entry, Poem, Critique, Person, Place, Topic, Era, CriticalTerm
```

현재 구현된 결정론적 Sources 조립, 본문 Person/Place 링크, provenance 검증을 유지하면서 실제 source data의 모든 ID 형식, nested/list 결과, 모든 node class, 실제 응답 사용 범위까지 확장한다.

---

## 2. 현재 상태와 확인된 결함

### 이미 구현되어 유지해야 할 기능

- `tools.evidence.poetrytalks_url()`을 통한 URL 생성
- `build_citations()`의 결정론적 citation 생성
- `tools.answer_renderer.assemble_final_answer()`의 코드 소유 Sources 조립
- LLM이 생성한 가짜/불완전 Sources 제거
- `(?)`, `Entry None`, `Entry 0` 방지
- 동일 내부 node ID citation 중복 제거
- 본문 Person/Place 첫 언급 자동 링크
- 외부 authority ID를 Poetry Talks node ID로 오인하지 않는 방어
- `P553`과 `P1227`처럼 내부 ID가 다른 Person을 별개 node로 유지

### 결함 1 — 실제 CriticalTerm ID가 검증기에서 탈락

현재 `is_valid_node_id()`는 `^[A-Z][0-9]{1,4}$`에 해당하는 형식만 허용한다. 실제 source data의 CriticalTerm ID는 `CT###` 형식이다.

실측 기준:

```text
neo4j_import_nodes.jsonl 전체 node ID: 8,232개
현재 검증기를 통과하지 못하는 ID: 692개
누락 ID 형식: CT + 숫자 3자리
예: CT017, CT018, CT021
```

현재 실제 동작:

```text
poetrytalks_url("P553")  -> https://poetrytalks.org/P553
poetrytalks_url("CT017") -> None
```

기존 테스트가 실제 `CT017` 대신 가상의 `K123`을 CriticalTerm 대용으로 사용하므로 이 문제를 발견하지 못한다.

### 결함 2 — domain 문자열이 분산되고 요구 문구와 철자가 다름

현재 코드와 테스트는 `https://poetrytalks.org/`를 사용한다. 사용자 요구 문구에는 `https://poetrtalks.org/`가 적혀 있다.

둘 중 어느 도메인이 배포의 권위 있는 URL인지 확인 없이 일괄 치환하지 않는다. 하지만 base URL이 prompt, Cypher projection, regex, 테스트에 중복 하드코딩된 현재 구조는 제거한다.

### 결함 3 — evidence에 검색된 ID와 실제 응답에서 사용한 ID를 구분하지 않음

현재 `_collect_all_node_ids()`는 graph/vector evidence에서 찾은 ID 전체를 수집한다. 따라서:

- 검색됐지만 응답에서 사용하지 않은 node가 Sources에 포함될 수 있다.
- 응답에서 사용했지만 normalization 중 ID가 빠진 node는 Sources에 없을 수 있다.

### 결함 4 — 본문 자동 링크가 Person/Place 중심

`assemble_final_answer()`에 전달되는 `evidence["entities"]`는 authority enrichment 대상인 Person/Place 중심이다. Work, Entry, Poem, Critique, Topic, Era, CriticalTerm 이름이 본문에 나와도 자동 링크가 보장되지 않는다.

### 결함 5 — vector metadata가 여러 node class의 ID를 반환하지 않음

현재 enriched vector retrieval metadata에서 다음 항목은 이름만 있고 내부 ID가 없다.

```text
topics
forms_types
critical_terms
era
```

`contained_poems`와 `contained_critiques`는 ID를 가지지만 citation collector가 nested list를 순회하지 않는다.

### 결함 6 — graph row의 scalar ID만 제한적으로 처리

`provenance_from_graph_row()`는 top-level scalar string 중심으로 검사한다. 다음 결과를 완전하게 처리하지 못할 수 있다.

```cypher
RETURN collect({id: poem.id, nameKor: poem.nameKor}) AS poems
```

동일 row에 여러 Poem/Entry/Topic/CriticalTerm ID가 있어도 종류별 첫 ID만 남을 수 있다.

---

## 3. 변경 금지 및 핵심 정책

1. 질문별 계산식을 하드코딩하지 않는다. 자연어 해석과 동적 Cypher 생성은 기존 LLM 역할로 유지한다.
2. Neo4j 내부 node ID가 canonical identity다.
3. 내부 ID가 다른 node를 외부 ID가 같다는 이유로 병합하지 않는다.
4. `P553`과 `P1227`을 병합하거나 집계값을 합산하지 않는다.
5. 외부 ID인 `Q464558`, `E0063034`, `koreanPerson_*` 등을 Poetry Talks node ID로 링크하지 않는다.
6. Neo4j source data를 수정하거나 재수집하지 않는다.
7. vector index를 재생성하지 않는다.
8. 기존 Cypher read-only validator, 인증 후 lazy initialization, embedding hardening을 약화하지 않는다.
9. Sources 조립을 다시 LLM에게 맡기지 않는다.
10. 사용자의 실제 질문 문구를 production 분기문이나 전용 query에 하드코딩하지 않는다.

---

## 4. 권장 구현 순서

## Phase 0 — 기준선 기록과 domain 결정 준비

작업 전에 실행한다.

```powershell
git status --short
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest discover -s tests -v
```

사용자의 기존 변경을 보존하고, 이 작업과 무관한 파일을 포맷팅하거나 되돌리지 않는다.

### Domain 처리

1. 저장소 문서, 배포 설정, 현재 서비스 URL에서 권위 있는 domain을 확인한다.
2. 확인 결과가 없으면 현재 동작인 `https://poetrytalks.org/`를 기본값으로 유지한다.
3. `poetrtalks.org`가 실제 요구 domain임을 확인한 경우에만 기본값을 변경한다.
4. domain 확인 여부와 최종 선택을 완료 보고에 명시한다.
5. domain 확인이 불가능해도 나머지 Phase는 진행한다.

## Phase 1 — 실제 node ID schema와 URL 설정 통합

### 1.1 ID schema 수정

실제 source data 기준 allowlist를 단일 위치에 정의한다.

권장 초기 형태:

```python
NODE_ID_PATTERNS = {
    "Work": r"B\d{1,4}",
    "Entry": r"E\d{1,4}",
    "Poem": r"M\d{1,4}",
    "Critique": r"C\d{1,4}",
    "Person": r"P\d{1,4}",
    "Place": r"L\d{1,4}",
    "Topic": r"T\d{1,4}",
    "Era": r"H\d{1,4}",
    "CriticalTerm": r"CT\d{1,4}",
}
```

또는 동등하게 검증 가능한 prefix registry를 사용한다. 핵심은 `CT###` 지원과 외부 ID fail-closed다.

요구사항:

- `is_valid_node_id("CT017")`은 True
- `poetrytalks_url("CT017")`은 정상 URL
- `Q464558`, `E0063034`, `koreanPerson_16062`, 빈 값은 False/None
- column key가 external authority field인지 검사하는 기존 방어 유지
- `_ID_PREFIX_TO_KIND`가 `CT -> critical_term`을 올바르게 판별
- `C###` Critique와 `CT###` CriticalTerm을 혼동하지 않음

### 1.2 데이터 기반 schema test

`neo4j_data_import/neo4j_import_nodes.jsonl`의 top-level `ID`를 모두 읽는 테스트를 추가한다.

```python
for every row:
    assert is_valid_node_id(row["ID"])
    assert poetrytalks_url(row["ID"]) == BASE_URL + row["ID"]
```

현재 snapshot에서 8,232개 고유 ID가 검사되고 누락이 0이어야 한다. 데이터 파일이 없는 경량 CI에서는 명시적으로 skip하되 순수 fixture로 각 node class를 반드시 검사한다.

### 1.3 Base URL 단일화

다음을 하나의 설정/상수로 통합한다.

```text
POETRYTALKS_BASE_URL
```

중복 제거 대상:

- `tools/evidence.py`
- `tools/answer_renderer.py`
- `tools/synthesis.py`의 Markdown link regex
- `tools/vector.py` prompt와 retrieval query
- `tools/cypher.py` prompt
- `agent.py` prompt 예시
- 관련 테스트와 문서

Python regex는 hardcoded domain 대신 `re.escape(POETRYTALKS_BASE_URL)`로 구성한다. Cypher projection에서 URL을 직접 만들 필요가 없다면 ID만 반환하고 Python에서 URL을 생성하는 구조를 우선한다.

### Phase 1 필수 테스트

- 실제 9개 node class의 ID URL 생성
- `CT017`과 `C017`이 각자 유효하며 충돌하지 않음
- 외부 authority ID 거부
- source JSONL 전체 ID 100% 커버리지
- 코드에서 승인된 base URL 외 hardcoded Poetry Talks domain이 남지 않았는지 정적 검사

## Phase 2 — 모든 node class를 표현하는 `NodeReference` 계약

authority enrichment용 `Entity`와 답변 링크용 node reference를 분리한다.

권장 계약:

```python
@dataclass
class NodeReference:
    node_id: str
    node_type: str
    name_kor: str | None = None
    name_chi: str | None = None
    name_eng: str | None = None
    source_type: str | None = None
    work_id: str | None = None
    entry_id: str | None = None
```

정확한 필드명은 조정 가능하지만 다음 조건을 만족해야 한다.

1. 모든 node class를 담을 수 있음
2. 내부 ID와 외부 authority ID가 구조적으로 분리됨
3. 다국어 이름을 본문 자동 링크에 사용할 수 있음
4. graph/vector provenance와 연결 가능
5. `Evidence.to_dict()`를 통해 synthesis와 renderer로 전달 가능
6. 기존 `Entity`와 authority enrichment API를 깨뜨리지 않음

`Evidence`에 `node_references` 필드를 추가하거나 동등한 명시적 구조를 추가한다. 기존 serialized evidence를 읽는 코드와 테스트의 하위 호환성을 유지한다.

### NodeReference de-dup 규칙

- node ID가 같으면 같은 reference
- node ID가 다르면 외부 ID나 이름이 같아도 별도 reference
- 이름만 같고 ID가 다르면 동명이인으로 처리하며 자동 본문 링크 금지
- 잘못된 ID는 reference 생성 단계에서 차단·진단

### Phase 2 필수 테스트

- Person, Place뿐 아니라 Work/Entry/Poem/Critique/Topic/Era/CriticalTerm reference 생성
- `P553`, `P1227` 별도 유지
- `C017`, `CT017` 별도 유지
- 동명이인 node reference 병합 금지
- 외부 ID로 NodeReference 생성 금지

## Phase 3 — graph/vector retrieval의 모든 node ID 보존

### 3.1 Vector metadata 보완

`tools/vector.py` enriched retrieval query에서 다음 ID를 추가한다.

```text
topics[].id              = t.id
forms_types[].id         = t.id
critical_terms[].id      = ct.id
era.id                   = e.id
```

기존 ID도 유지한다.

```text
entry_id
source_work_id
creator_id
mentioned_persons[].id
audiences[].id
places[].id
contained_poems[].id
contained_critiques[].id
```

metadata normalization이 각 항목을 적절한 `NodeReference`로 변환하게 한다.

### 3.2 Graph result 표준 alias 보완

Cypher generation prompt에 다음 일반 규칙을 추가·정리한다.

- 답변에서 반환할 모든 node는 이름/본문뿐 아니라 내부 `id`를 함께 RETURN
- role이 여러 개면 `critic_person_id`, `subject_person_id`처럼 role prefix 사용
- collection이면 각 map에 `node_id`, `node_type`, 다국어 이름을 포함
- 집계 질문도 aggregate subject의 내부 ID 반환
- 특정 질문이나 계산 공식을 하드코딩하지 않음

기존 few-shot 예시 중 내부 ID 없이 이름/본문만 반환하는 예시를 일반 규칙과 맞게 수정한다. 예시는 계산 의미를 고정하기 위한 것이 아니라 ID provenance를 누락하지 않기 위한 것이다.

### 3.3 재귀적 graph/vector reference 추출

top-level scalar만 스캔하지 말고 알려진 구조의 list/dict를 재귀 처리한다.

안전 요구사항:

- source text 안의 `P553` 같은 문자열을 node ID로 오인하지 않음
- key/path가 node reference임을 입증할 때만 ID 수집
- `idWikidata`, `idAKSency` 등 external field는 모든 depth에서 제외
- arbitrary dict의 모든 문자열을 무차별 ID로 인식하지 않음
- cycle/depth/collection size bound 적용
- 동일 ID는 한 번만 reference 생성
- 동일 row에 여러 Poem/Entry/Topic/CriticalTerm이 있으면 모두 보존

권장 방식:

- 알려진 vector metadata field별 extractor
- standardized graph alias와 `{node_id, node_type}` map extractor
- generic fallback은 엄격한 key allowlist와 ID validator 사용

### Phase 3 필수 테스트

- `topics`, `forms_types`, `critical_terms`, `era` ID 보존
- nested `contained_poems`/`contained_critiques` ID 보존
- `collect([{node_id: ...}, ...])`의 모든 node 보존
- 같은 type node 여러 개가 한 row에 있어도 모두 보존
- nested external IDs는 Poetry Talks reference로 변환되지 않음
- source text 안의 ID-looking 문자열은 무시
- 과도하게 깊은/큰 nested payload가 bounded 처리됨

## Phase 4 — 실제 응답에서 사용한 node ID 추적

현재 evidence 전체 citation 방식에서 “실제 응답에서 언급하거나 근거로 사용한 node” 방식으로 전환한다.

### 권장 synthesis 출력 계약

```python
SynthesisResult = {
    "answer_body": str,
    "referenced_node_ids": list[str],
}
```

요구사항:

1. `referenced_node_ids`는 `Evidence.node_references`에 존재하는 ID만 허용한다.
2. LLM이 evidence에 없는 ID를 반환하면 폐기하고 correlation ID로 진단한다.
3. 같은 ID는 한 번만 유지한다.
4. 서로 다른 내부 ID는 외부 ID가 같아도 별도 유지한다.
5. LLM JSON/structured output 파싱 실패가 전체 답변 실패로 이어지지 않도록 안전한 fallback을 둔다.
6. fallback은 본문에 명시적으로 나타난 검증된 ID와 유일하게 매핑되는 node name을 이용한다.
7. 동명이인은 임의로 선택하지 않는다.
8. retrieval됐지만 최종 답변에서 사용하지 않은 node는 `poetrytalks wikidata` node-link group에서 제외한다.
9. 인용한 원문/집계 근거의 Work/Entry/Poem/Critique provenance가 필요하면 해당 source node ID를 `referenced_node_ids`에 포함하도록 synthesis evidence contract를 명확히 한다.

기존 `build_citations(evidence, language)`는 하위 호환을 위해 optional filter를 받을 수 있다.

```python
build_citations(
    evidence,
    language,
    referenced_node_ids=validated_ids,
)
```

production 정상 경로에서는 검증된 ID 목록을 반드시 전달한다. legacy 호출에서 `None`일 때의 동작은 문서화한다.

### 구조화 출력 안전성

- answer body의 사실 근거는 기존 structured evidence로 제한
- unknown JSON field 무시 또는 거부 정책 명시
- 최대 ID 개수 제한
- ID 배열에 URL이나 외부 ID를 직접 받지 않음
- Markdown Sources는 계속 Python renderer가 생성
- LLM이 Sources를 직접 작성하지 않음

### Phase 4 필수 테스트

- evidence에 P553/P1227/P999가 있고 body가 P553만 사용하면 P553 node citation만 출력
- unknown `P9999`를 LLM이 반환해도 evidence에 없으면 제외
- `Q464558` 제외
- 같은 ID 반복 제거
- structured output 파싱 실패 fallback
- 동명이인 이름 fallback 시 링크 생략
- 근거 Entry/Work ID가 referenced list에 있으면 breadcrumb 유지

## Phase 5 — 모든 node class의 본문 링크와 Sources 생성

`link_entities_in_body()`를 범용 `NodeReference` 기반 renderer로 확장하거나 새 함수를 만든다.

예:

```python
link_node_references_in_body(
    answer_body,
    node_references,
    referenced_node_ids,
)
```

### 본문 링크 규칙

1. 검증된 referenced node만 링크
2. Person, Place, Work, Entry, Poem, Critique, Topic, Era, CriticalTerm 모두 지원
3. 다국어 이름 중 실제 본문에 등장한 첫 안전한 mention을 링크
4. 같은 node의 영문명과 한글명이 연속 등장해도 과도한 이중 링크를 피함
5. 동일 이름이 여러 node ID에 매핑되면 자동 링크하지 않음
6. 기존 Markdown link, image, inline/fenced code, raw URL을 변경하지 않음
7. 영어의 짧은 이름이 더 긴 단어 내부에서 우연히 매치되지 않도록 boundary 처리
8. ID 자체가 본문에 plain text로 명시된 경우 검증 후 링크 가능
9. renderer 오류는 본문 전체 실패로 이어지지 않음

### Sources 규칙

- `poetrytalks wikidata` group name은 기존 프로젝트 정책대로 유지
- ID별 URL은 중앙 base URL + 검증된 내부 ID
- referenced node별 한 bullet
- graph/vector 양쪽에 있어도 같은 ID는 한 번
- 다른 내부 ID는 외부 ID가 같아도 별도 bullet
- nested node도 동일 규칙 적용
- model-generated Sources는 기존처럼 제거
- deterministic Sources section은 최종 코드가 부착

### Phase 5 필수 테스트

- Work 이름 본문 링크
- CriticalTerm `CT017` 이름과 ID 링크
- Topic/Era 링크
- contained Poem/Critique 링크
- 같은 node의 bilingual 표기에 불필요한 링크 중복 없음
- 영어 substring 오탐 없음
- Sources와 본문 링크가 동일 base URL/ID 사용
- LLM이 Sources를 생략·변형해도 최종 Sources 정상

## Phase 6 — 정적 검사, 문서, 전체 회귀 검증

### 코드·문서 정리

1. 오래된 “한 글자 prefix만 지원”, “CriticalTerm은 K prefix” 주석과 테스트 fixture를 실제 schema로 수정한다.
2. `tools/vector.py`와 `tools/cypher.py`의 URL 생성 지시가 새로운 중앙 정책과 일치하도록 한다.
3. `IMPLEMENTATION_NOTE.md`에 다음을 기록한다.
   - authoritative Poetry Talks base URL
   - node ID prefix registry
   - `NodeReference` schema
   - nested extraction bounds
   - referenced-node selection/fallback 정책
4. 실제 domain과 환경별 설정 방법을 README에 기록한다.

### 최종 검증

```powershell
python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests
python -m unittest discover -s tests -v
git diff --check
git status --short
```

가능한 환경에서는 대표 질문으로 smoke test한다.

```text
Which woman is mentioned the most in Sihwa ch'ongnim?
기고(奇古) 비평용어가 사용된 비평문을 알려줘.
지봉유설에 포함된 시와 관련 주제를 보여줘.
```

각 답변에서 실제 언급된 Person, Work, Entry, Poem, Critique, Topic, CriticalTerm의 ID 링크와 Sources를 점검한다.

---

## 5. 전체 수용 기준

다음이 모두 충족되어야 완료다.

1. source JSONL의 8,232개 node ID가 모두 유효한 Poetry Talks URL로 변환된다.
2. `CT###` 692개가 더 이상 누락되지 않는다.
3. 외부 authority ID는 Poetry Talks URL로 변환되지 않는다.
4. base URL이 단일 설정에서 관리되고 prompt/query/regex/test에 서로 다른 값이 남지 않는다.
5. authoritative domain 선택이 문서화된다.
6. 모든 node class가 `NodeReference`로 표현된다.
7. vector metadata의 Topic, Era, CriticalTerm ID가 보존된다.
8. nested Poem/Critique와 graph collection의 모든 node ID가 bounded하게 추출된다.
9. 최종 답변에서 검증된 referenced node만 node-link citation group에 포함된다.
10. 검색됐지만 사용되지 않은 node는 해당 group에 불필요하게 노출되지 않는다.
11. evidence에 없는 node와 동명이인은 자동 링크되지 않는다.
12. 본문과 Sources가 동일한 base URL 및 내부 ID를 사용한다.
13. `P553`과 `P1227`은 별도 node로 유지된다.
14. 질문별 계산식은 계속 동적 LLM Cypher가 담당한다.
15. 기존 deterministic Sources와 보안 테스트를 포함한 전체 테스트가 통과한다.

---

## 6. 완료 보고 형식

Claude Code는 작업 완료 시 다음을 보고한다.

1. 변경 파일 목록과 파일별 핵심 변경
2. 확인한 authoritative Poetry Talks domain과 근거
3. 지원 node ID prefix와 실제 데이터 커버리지 결과
4. `NodeReference` 데이터 계약
5. graph/vector nested reference 추출 방식과 bounds
6. synthesis의 `referenced_node_ids` 검증 및 fallback 정책
7. 본문 링크와 Sources 생성 흐름
8. 추가한 테스트 수, 전체 테스트 수와 결과
9. 실행한 smoke test와 실제 응답의 링크 예시
10. 실행하지 못한 라이브 검증 및 남은 제한

코드가 구조적으로 보장하는 범위와 LLM의 비결정적 출력에 의존하는 범위를 명확히 구분한다.

# Claude Code 작업 지시서 — graphRAG 순위/집계 질의 신뢰성 보완

## 0. 작업 목적

graphRAG 모드에서 다음과 같은 순위형 질문이 실제 그래프 결과가 없어서가 아니라, LLM 생성 Cypher 오류와 후속 처리 오류 때문에 `검색 결과 없음`으로 잘못 응답한 문제를 수정한다.

```text
Who is the most mentioned king in Sihwa ch'ongnim?
```

이번 작업의 목표는 다음 네 가지다.

1. 잘못 생성된 Cypher를 Neo4j 실행 전에 탐지하고, 안전하게 한 번만 복구 재생성한다.
2. `질의 생성 실패`, `일시적 DB 연결 실패`, `정상 실행 후 결과 없음`을 서로 다른 사용자 메시지로 처리한다.
3. 코퍼스 순위/집계 질문을 인물 전기 질문으로 오인하여 무관한 외부 authority API를 대량 호출하지 않게 한다.
4. Aura/Bolt의 일시적 끊긴 연결을 읽기 전용 범위에서 회복 가능하게 처리한다.

이 문서는 구현 작업 지시서다. 구현자는 코드 변경, 단위 테스트, 가능한 범위의 읽기 전용 live smoke test까지 수행한다.

---

## 1. 관찰된 장애와 확정 사실

### 1.1 실제 생성된 잘못된 Cypher

관찰된 질의에는 아래와 같은 다중 라벨 필터가 있었다.

```cypher
MATCH (p:Person)-[:HAS_OFFICE]->(office:Topic)
WHERE office.nameKor CONTAINS '왕'
   OR office.nameEng CONTAINS 'king'
   OR office.nameChi CONTAINS '王'
WITH p
MATCH (text)
WHERE (text:`Entry OR text`:Poem OR text:Critique)
  AND (text)-[:HAS_SUBJECT_PERSON]->(p)
RETURN p.nameKor AS person_name_kor,
       p.nameChi AS person_name_chi,
       p.nameEng AS person_name_eng,
       p.ID AS person_id,
       p.idWikidata AS wikidata_id,
       p.idAKSdigerati AS aks_digerati_id,
       count(text) AS mention_count
ORDER BY mention_count DESC
LIMIT 1
```

Neo4j는 `` `Entry OR text` ``를 하나의 라벨명으로 해석하고, 다음 경고를 냈다.

```text
Neo.ClientNotification.Statement.UnknownLabelWarning
missing label name: Entry OR text
```

따라서 이 실행을 **정상적인 빈 검색 결과**로 취급하면 안 된다.

### 1.2 올바른 다중 라벨 문법

아래 둘 중 하나만 사용한다.

```cypher
WHERE text:Entry OR text:Poem OR text:Critique
```

```cypher
WHERE any(label IN labels(text) WHERE label IN ['Entry', 'Poem', 'Critique'])
```

### 1.3 같은 요청에서 발생한 연쇄 문제

- `blocked unsafe cypher [...]: query has no RETURN clause`도 발생했다.
  - 로그에 보이는 첫 질의에는 `RETURN`이 있으므로, 이는 별도의 후속 생성/재시도 질의다.
  - 원본 Cypher를 사용자 응답이나 synthesis 입력에 노출하지 않는 정책은 유지한다.
- 질문의 `Who is`가 `_PERSON_CUES`에 걸려 순위 질문임에도 인물 authority 보강이 실행되었다.
  - 그 결과 graphRAG 순위 답에 무관한 14명의 후보 중 10명에 대해 외부 source 보강 및 긴 Sources 목록이 붙었다.
- Bolt 연결에서 다음과 같은 일시 장애가 있었다.

```text
Failed to read from defunct connection ... OSError('No data')
Transaction failed and will be retried ...
```

이는 잘못된 라벨 질의의 직접 원인은 아니지만, 별도 복원 정책이 필요한 운영 안정성 이슈다.

---

## 2. 반드시 지킬 제약

1. **읽기 전용 보안을 약화하지 말 것**
   - `SafeNeo4jGraph` 및 `validate_read_only_cypher()`의 write/schema/admin 차단을 제거하거나 완화하지 않는다.
   - `CREATE`, `MERGE`, `DELETE`, `SET`, `CALL`, dynamic query 등은 계속 차단한다.

2. **내부 노드 ID 표기는 반드시 대문자 `ID`를 유지할 것**
   - 이 Neo4j DB의 자체 식별자 속성은 `p.ID`, `e.ID`, `text.ID`다.
   - `p.id`로 바꾸면 값이 `null`이 된다. 이번 수정 과정에서 소문자 `id`로 되돌리지 않는다.

3. **원본 LLM Cypher를 사용자에게 노출하지 말 것**
   - 사용자용 메시지와 synthesis Evidence에는 `invalid_query` / `temporarily_unavailable` 같은 정규화 상태만 전달한다.
   - 운영 로그도 기존 correlation ID 정책을 유지한다. 필요 시 보안 접근이 제한된 debug log에만 hash/분류를 남긴다.

4. **외부 authority는 코퍼스 순위의 근거가 아니다**
   - “가장 많이 언급된 인물/왕/시인”의 순위는 Neo4j 그래프에서만 계산한다.
   - 외부 authority 정보는 사용자가 우승자에 대한 전기·역사·외부 출처 비교를 명시적으로 요청한 후속 질문에서만 보강한다.

5. **`poetrytalks wikidata` 명칭은 임의로 바꾸지 말 것**
   - 현재 프로젝트 정책상 Poetry Talks 내부 노드 링크의 고정 그룹명이다.
   - `T1052` 같은 링크는 외부 Wikidata 결과가 아니라 내부 Topic 링크라는 점만 코드 주석/운영 문서에서 명확히 한다.
   - 이 명칭 자체를 변경하려면 별도 사용자 승인 작업으로 분리한다.

6. 외부 authority registry, AKS Person/Place endpoint 안전장치, source cap의 기본값을 이번 작업에서 임의로 변경하지 않는다. 이번 범위는 **집계 질문에서 불필요한 보강을 시작하지 않게 하는 routing 보완**이다.

7. **한국 관련 개체의 로마자 표기는 Revised Romanization(RR)이 아니라 McCune–Reischauer(MR)를 사용할 것**
   - DB에 저장된 `nameMR`을 한국 Person/Place/Work/Era/Topic 등의 라틴 문자 표기에 대한 유일한 권위 필드로 사용한다.
   - `nameRR`은 사용자 노출 표기에 사용·생성·추정하지 않는다. import 데이터에는 `nameRR`이 존재할 수 있으나, 표준 출력 필드는 아니다.
   - RR 입력 검색 호환성이 필요할 때만, 제한된 검색 조건에서 `nameRR`을 alias로 참조할 수 있다. 이 경우에도 `RETURN`, vector metadata, Evidence, synthesis prompt, 최종 본문 및 Sources로 전달하면 안 된다.
   - `nameEng`은 영어 번역/통용 영문명일 수 있으므로, 한국어 이름의 로마자 표기를 대체하는 fallback으로 조용히 사용하지 않는다.
   - `nameMR`이 비어 있으면 RR이나 임의의 로마자 표기를 만들어 내지 말고 원어 표기(`nameKor`/필요 시 `nameChi`)를 유지한다. `nameEng`은 진짜 영어 번역/제목임이 분명할 때만 별도 영어명으로 표시할 수 있다.
   - 저장된 MR의 breve/diacritic(`ŏ`, `ŭ` 등), apostrophe(`Ch'wisŏn` 등), 공백과 대소문자는 그대로 보존한다. ASCII화·Unicode 정규화·자동 변환으로 표기를 바꾸지 않는다.
   - 중국계 개체의 `namePY`(Hanyu Pinyin), 원문 한자 표기, 인용문은 이 규칙의 대상이 아니다.
   - 검색 입력의 호환성(예: 사용자가 관용 영문명 또는 RR 형태를 입력한 경우)은 표시 표준과 별개다. 검색이 성공하면 최종 답변의 한국 관련 개체 표기는 MR로 정규화한다.

---

## 3. 구현 범위와 우선순위

### P0-A. 잘못된 다중 라벨 Cypher의 사전 탐지 및 단 한 번의 안전한 복구

대상 파일:

- `tools/cypher_safety.py`
- `tools/cypher.py`
- 관련 단위 테스트

#### 요구사항

1. `validate_read_only_cypher()`가 문자열/주석을 제거하기 **전**에 아래와 같은 잘못된 형태를 감지한다.

```cypher
text:`Entry OR text`:Poem
text:`Entry OR text`:Poem OR text:Critique
```

2. 탐지는 좁고 명시적으로 한다.
   - 백틱으로 감싼 라벨 식별자에 `OR` 또는 또 다른 `:`가 섞여 있는, 다중 라벨 결합 오용만 거부한다.
   - 정상적인 속성명/식별자 백틱 사용을 광범위하게 차단하지 않는다.
   - 현재 DB의 노드 라벨은 공백이나 백틱이 필요하지 않으므로, LLM 생성 라벨 필터에 대해 엄격한 정책을 적용해도 된다.

3. `UnsafeCypherError`에 기계적으로 분기 가능한 안전한 사유 분류를 추가한다. 예:

```text
malformed_label_predicate
missing_return
forbidden_keyword
disallowed_call
multi_statement
```

   - 원본 query 문자열은 예외 객체, 사용자 메시지, Evidence에 넣지 않는다.

4. `retrieve_graph_evidence()`는 아래의 **복구 가능 형식 오류**에 한해서만 재생성 1회를 수행한다.

   - `CypherSyntaxError`
   - `malformed_label_predicate`
   - `missing_return`

5. 다음 사유에는 재생성하지 않고 즉시 `invalid_query`로 종료한다.

   - write/schema/admin keyword
   - allowlist 밖의 `CALL`
   - multi-statement
   - 정책상 위험한 동적 실행 계열

6. 재생성 프롬프트에는 원본 query를 그대로 다시 넣지 않는다. 다음처럼 일반화된 힌트만 준다.

```text
The previous Cypher was rejected for query shape. Return exactly one read-only
Cypher query with a RETURN clause. For multiple labels, write separate label
predicates joined by OR; never place “OR” inside a backtick-quoted label.
```

7. 재시도도 실패하면 Evidence에는 다음 중 하나의 status claim만 둔다.

```text
invalid_query
temporarily_unavailable
```

원본 query, Python traceback, Neo4j 호스트명, API secret은 절대 Evidence 또는 최종 답변에 넣지 않는다.

#### 추가 방어

- Neo4j가 `UnknownLabelWarning`을 Python/LangChain API로 제공하는지 현재 의존성 버전에서 확인한다.
- 접근 가능하다면, 실행 중 발생한 `UnknownLabelWarning`도 정상 빈 결과가 아니라 `invalid_query` 또는 1회 복구 재생성 대상으로 처리한다.
- 접근할 수 없다면, 생성 query의 label predicate를 schema allowlist (`Person`, `Poem`, `Critique`, `Entry`, `Topic`, `CriticalTerm`, `Place`, `Work`, `Era`)와 사전 검증하여 같은 오류가 실행 단계로 넘어가지 않게 한다.
- 이 보완은 read-only validator를 우회하는 별도 실행 경로를 만들면 안 된다.

### P0-B. 순위/집계 질문의 결정적(graph-first) 질의 경로

대상 파일:

- `tools/cypher.py`
- 필요 시 새 모듈(예: `tools/graph_intent.py`)
- 관련 테스트

#### 문제

`most mentioned`, `top N`, `most frequent`, `가장 많이 언급된`, `언급 횟수가 가장 많은` 같은 질문을 일반 LLM Cypher 생성에 전적으로 맡기면, 이번과 같이 문법·의미 오류가 발생해도 빈 결과처럼 보일 수 있다.

#### 요구사항

1. 순위/집계 의도를 먼저 감지한다.
   - 영어: `most mentioned`, `most frequent`, `top`, `least`, `how many`, `rank`, `highest count` 등
   - 한국어: `가장 많이 언급`, `가장 많이`, `상위`, `순위`, `횟수`, `몇 번` 등
   - 중국어 지원 규칙이 이미 있으면 같은 방식으로 추가한다.

2. 이번 질문군인 “직위/속성 조건에 맞는 Person 중 시화총림에서 가장 많이 언급된 사람”은 LLM 자유 생성 Cypher 대신 parameterized query template 또는 동등하게 제한된 structured intent → template 경로로 처리한다.

3. `king` / `왕` / `王`은 우선 실제 DB에서 아래를 읽기 전용으로 검증한 뒤 매핑한다.

```cypher
MATCH (p:Person)-[:HAS_OFFICE]->(office:Topic)
WHERE office.nameKor CONTAINS '왕'
   OR office.nameEng CONTAINS 'king'
   OR office.nameChi CONTAINS '王'
RETURN office.ID, office.nameKor, office.nameEng, office.nameChi,
       count(DISTINCT p) AS person_count
ORDER BY person_count DESC
```

   - `HAS_OFFICE`가 왕/군주 신분을 실제로 표현하는 관계인지 검증한다.
   - 데이터 모델이 다른 관계/Topic으로 왕을 표현한다면, live 결과와 스키마 근거를 남기고 올바른 관계로 template를 바꾼다.
   - 관계를 추측해서 새 label/relationship을 만들지 않는다.

4. 집계 template는 다음을 만족해야 한다.

   - `p.ID AS person_id`를 반환한다.
   - 이름과 필요한 authority ID는 표준 alias로 반환한다.
   - count 대상과 순위 기준을 명시한다.
   - `count(DISTINCT text)` 또는 확정된 canonical unit을 사용한다.
   - `LIMIT`이 있다.
   - 외부 입력은 파라미터화하거나 allowlist/정규화한 뒤 사용한다. 자연어를 string interpolation으로 Cypher에 넣지 않는다.

#### 언급 횟수의 정의를 확정할 것

현재 예시는 `Entry`, `Poem`, `Critique`를 모두 count한다. 하나의 Entry와 그 안의 Poem/Critique가 모두 subject relation을 가지면 연구상 같은 언급을 중복 집계할 위험이 있다.

구현 전 다음 두 기준을 실제 데이터에서 비교한다.

1. `Entry`만 기준으로 한 언급 횟수
2. `Entry | Poem | Critique` 텍스트 노드 기준의 언급 횟수

프로젝트의 “most mentioned” 정의를 하나로 확정하고 다음을 문서화한다.

- 선택한 canonical unit
- 중복 제거 방식
- 동률 처리 방식 (`ORDER BY`, 필요 시 동률 전원 반환)
- 최종 답변에 표시할 count의 의미

합의된 기준이 없으면 기본값으로 “Entry 단위의 `HAS_SUBJECT_PERSON` 관계 수”를 사용하되, 구현 보고서에 이를 명시하고 사용자에게 확인이 필요한 결정으로 기록한다.

### P0-C. status 정규화 — 실패를 `검색 결과 없음`으로 바꾸지 않기

대상 파일:

- `tools/cypher.py`
- `tools/orchestrator.py`
- `tools/synthesis.py`
- 관련 테스트

#### 상태 계약

| 상태 | 발생 조건 | 사용자용 의미 |
|---|---|---|
| `no_results` | 검증된 read-only query가 정상 실행되었고 rows가 빈 경우 | “그래프에서 일치하는 결과를 찾지 못했습니다.” |
| `invalid_query` | LLM query가 형식/안전 검증을 통과하지 못했고 복구도 실패한 경우 | “그래프 순위 검색을 완료하지 못했습니다. 다시 시도해 주세요.” |
| `temporarily_unavailable` | Bolt transport, Neo4j service, timeout 등 실행 인프라 실패 | “그래프 검색이 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요.” |

#### 요구사항

1. `rows == []`만으로 `no_results`를 결정하지 않는다.
2. query warning, safety rejection, parsing failure, connection failure가 있었으면 `no_results`가 아니라 해당 실패 상태를 유지한다.
3. graph 순위가 실패한 경우 vectorRAG에서 우연히 얻은 후보 인물이나 외부 authority source를 이용해 순위 답을 합성하지 않는다.
4. 최종 Sources에는 실제 답변 본문에 사용한 정상 graph evidence만 넣는다.
5. graph 순위 실패 시 “가장 많이 언급된 왕은 없다” 같은 부정적 사실을 말하지 않는다.

### P0-D. 집계 질문에서 authority 보강 차단 및 후보 오염 방지

대상 파일:

- `tools/orchestrator.py`
- 필요 시 `tools/evidence.py`, `tools/synthesis.py`
- 관련 테스트

#### 문제

현재 `_PERSON_CUES`에 `who is`가 포함되어 있어, 아래 질문이 전기 질문으로 오인된다.

```text
Who is the most mentioned king in Sihwa ch'ongnim?
```

그 결과 graph 결과가 없거나 불완전한 상태에서도 vector evidence에서 모은 14명의 Person에 대해 authority fetch가 실행된다.

#### 요구사항

1. `is_graph_aggregation_intent(question)` 또는 동등한 구조화된 routing을 만들고, authority cue 판단보다 먼저 적용한다.
2. 코퍼스 순위/집계 질문에서는 `who is`, `tell me about` 등 일반적인 인물 cue만으로 authority fetch를 시작하지 않는다.
3. 외부 authority가 명시된 특수 질문은 다음처럼 분리한다.

   - “Who is the most mentioned king?” → authority fetch 없음
   - “Who is the most mentioned king, and what does Wikidata say about that person?” → 먼저 정상 graph ranking으로 winner를 확정한 뒤 **winner 한 명만** 보강
   - “Compare external sources for the most mentioned king” → 정상 ranking 후 winner 한 명에 대해서만 compare cap 정책 적용

4. graph ranking이 실패한 경우 authority fetch는 0회여야 한다.
5. 후보 entity는 집계의 정상 graph 결과에서만 가져온다. 순위 답에서 vector fallback으로 나온 관련 인물 전체를 후보로 합치지 않는다.
6. 불필요한 authority fetch가 없으면 아래 같은 coverage 문구도 표시되지 않아야 한다.

```text
External authority enrichment was applied to 10 of 14 relevant persons.
```

### P0-E. 한국 관련 개체 로마자 표기의 MR 일관성

대상 파일(실제 구조에 따라 최소 변경하되, 아래 데이터 전달 경로 전체를 점검):

- `tools/cypher.py`
- `tools/vector.py`
- `tools/evidence.py`
- `tools/synthesis.py`
- `tools/answer_renderer.py`
- `agent.py`, `text_rag.py` 및 관련 테스트

#### 사전 점검에서 확인된 불일치

- import CSV에는 `nameMR`과 `nameRR`이 함께 존재한다. 예를 들어 Place 데이터에는 `Puksan`(MR) / `Buksan`(RR), `Sŏgyŏng`(MR) / `Seogyeong`(RR) 쌍이 있다.
- `tools/cypher.py`는 `nameMR`을 McCune–Reischauer 필드로 올바르게 정의하지만, `nameRR`이 DB에 없다고 설명한다. 이 설명은 import 데이터와 맞지 않으므로 수정해야 한다.
- 그러나 `tools/vector.py`의 prompt와 metadata projection에는 여전히 `nameRR`, `creator_rr`, `p.nameRR` 참조가 남아 있다.
- 현재 `Entity`/`NodeReference`의 정규화 필드는 `name_kor`, `name_chi`, `name_eng` 중심이므로, vector/graph에서 얻은 `nameMR`이 최종 synthesis 및 body-linking 단계까지 보존되지 않을 수 있다.

위 세 상태는 서로 모순되므로, 이 작업에서 하나의 데이터 계약으로 정리한다.

#### 데이터 계약 및 구현 요구사항

1. **표준 필드**
   - 한국 관련 개체의 로마자 표기는 저장된 `nameMR`만 사용한다.
   - 내부 Python/structured Evidence 표기는 `name_mr`, Neo4j projection alias는 `person_name_mr`, `place_name_mr`처럼 기존 snake_case alias 규칙과 일치하게 정한다.
   - 기존 이름 필드와 호환성이 필요하면 `nameMR` 입력을 `name_mr`로 정규화하되, 별도의 RR 필드를 만들거나 유지하지 않는다.

2. **Cypher 및 vector metadata**
   - graph ranking template와 일반 graph Cypher에서 한국 Person/Place가 최종 답변의 후보가 될 수 있으면 `nameMR AS ..._name_mr`를 표준 alias로 반환한다.
   - vector retrieval metadata의 `creator_mr`, `mentioned_persons[].nameMR` 등은 유지/정규화하고, `creator_rr`, `nameRR`, `p.nameRR`의 **응답 전달용 projection**은 제거한다.
   - RR 검색 호환성을 구현하는 경우 `nameRR` 참조는 검색 predicate 내부에만 격리하고, 해당 값을 `RETURN`하거나 metadata/Evidence/LLM context에 넣지 않는다.
   - `audiences`, `creator`, `mentioned_persons` 등 동일 개체가 여러 metadata shape로 전달되는 경로에서 MR 누락 여부를 함께 점검한다.
   - 사용자 입력 검색은 `nameMR`과 기존 `nameEng`/원어 표기를 계속 활용할 수 있다. 단, 검색용 alias/관용명 매칭 결과가 표시용 RR 선택으로 이어지면 안 된다.

3. **Evidence 및 rendering**
   - `Entity`, `NodeReference`, graph-row/vector-meta 정규화, merge/dedup, serialization에서 `name_mr`을 보존한다.
   - 최종 body, authority lookup label, deterministic body link 후보, Sources의 사람/장소 이름 표시에 한국 관련 라틴 표기가 필요하면 `nameMR`을 선택한다.
   - 권장 표시 우선순위는 다음과 같다.

     | 응답 맥락 | 한국 관련 개체의 표시 원칙 |
     |---|---|
     | 한국어 응답 | `nameKor` 우선. 라틴 문자 병기가 필요하면 `nameMR`만 병기. |
     | 영어·프랑스어 등 라틴 문자 응답 | `nameMR` 우선. 필요 시 원어 표기를 괄호에 병기. |
     | 한자 사용 언어 응답 | `nameChi` 우선. 라틴 문자 병기가 필요하면 `nameMR`만 사용. |
     | `nameMR` 없음 | 원어 표기를 유지하고, RR/임의 전사 생성 금지. |

   - 외부 authority 응답의 원문 필드나 직접 인용은 출처 원문으로 보존할 수 있다. 다만 동일 graph entity를 가리키는 답변 본문/출처 라벨은 graph의 `nameMR`을 우선해 RR 혼입을 막는다.
   - `namePY`는 Sinitic entity의 Hanyu Pinyin으로 계속 보존한다. 이를 MR로 변환하거나 한국 인명에 적용하지 않는다.

4. **프롬프트와 문서**
   - 모든 graphRAG/textRAG prompt에서 “Korean entity romanization = stored `nameMR` (McCune–Reischauer)”를 명시한다.
   - 현재 `tools/cypher.py`의 응답 언어 규칙에서 `nameEng`을 영어 응답의 일반적인 canonical name으로 두는 문구는, **한국 관련 개체의 라틴 문자 표기에는 적용되지 않도록** 수정한다. 영어 제목/번역과 한국어 이름의 로마자 표기를 혼동하지 않는다.
   - `nameRR`을 표시하거나 함께 출력하라는 문구를 제거한다. RR 입력 검색 호환성을 지원한다면, 그 범위를 검색 전용이라고 명시한다.
   - 이름을 새로 로마자화하는 LLM 작업, 외부 API의 임의 영문 표기 채택, RR 자동 변환 library 추가는 금지한다.
   - 구현 보고서에 `rg -n -i "nameRR|revised romanization"` 결과를 점검하고, 허용된 검색 predicate·legacy 입력 경계·테스트 외에는 사용자 출력 경로와 prompt에 남지 않았음을 기록한다.

5. **이번 graph ranking 작업과의 결합 규칙**
   - “Who is the most mentioned king in Sihwa ch'ongnim?”의 정상 결과에서 우승자가 한국 Person이면, 영어 답변의 이름은 해당 Person의 `nameMR`로 표시한다.
   - ranking count, 내부 `person_id`, graph provenance, authority routing 규칙은 이 표기 변경으로 달라지지 않는다.
   - graph ranking 실패 상태에서는 이름 표기 규칙 때문에 후보를 추정하거나 외부 API를 호출하면 안 된다.

### P1. Neo4j Aura/Bolt 끊긴 연결의 회복성

대상 파일:

- `graph.py`
- `tools/cypher.py` 또는 적절한 read-retrieval wrapper
- 관련 테스트 및 운영 문서

#### 요구사항

1. 현재 `graph.py`의 전역 `Neo4jGraph(...)`가 Streamlit 장기 프로세스에서 stale/defunct connection을 계속 재사용할 수 있는지 점검한다.
2. 설치된 `neo4j`, `langchain_neo4j` 버전에서 지원하는 방식만 사용하여 driver/connection 설정을 구성한다. 지원되지 않는 인자를 추측해 넣지 않는다.
3. **read-only graph retrieval에 한해** transport 계열 오류(`defunct connection`, `OSError('No data')`, service unavailable, transient error)를 제한적으로 재시도한다.
   - 최대 횟수, backoff, timeout을 상수/환경 설정으로 명시한다.
   - 무한 재시도 금지.
   - write/history 경로나 external authority 요청에 이 재시도 정책을 적용하지 않는다.
4. 재시도 후에도 실패하면 stale resource를 안전하게 폐기/재생성 가능한 구조로 만들고 `temporarily_unavailable`을 반환한다.
5. 실제 Neo4j URI, IP, credential은 로그·응답·테스트 fixture에 넣지 않는다.

---

## 4. 권장 구현 흐름

```text
사용자 질문
  │
  ├─ 순위/집계 intent인가?
  │    ├─ 예 → 제한된 graph template 실행
  │    │         ├─ 정상 rows → graph ranking evidence
  │    │         ├─ 정상 빈 rows → no_results
  │    │         └─ transport 실패 → temporarily_unavailable
  │    └─ 아니오 → 기존 LLM Cypher 생성 경로
  │              ├─ preflight safety/shape validation
  │              ├─ recoverable format error → 안전한 힌트로 1회 재생성
  │              └─ 실패 → invalid_query
  │
  ├─ authority가 명시적으로 필요한가?
  │    ├─ 일반 순위 질문 → 아니오, fetch 0회
  │    └─ winner 외부 정보 명시 → winner만 제한적으로 enrichment
  │
  ├─ 사용자에게 이름을 표시해야 하는가?
  │    └─ 한국 관련 개체의 라틴 문자 표기 → 저장된 nameMR(McCune–Reischauer)만 사용
  │
  └─ synthesis
       ├─ 정상 graph evidence → 순위 + count + 관련 graph 출처
       ├─ no_results → 정직한 무결과 메시지
       ├─ invalid_query → 안전한 질의 실패 메시지
       └─ temporarily_unavailable → 일시 장애 메시지
```

---

## 5. 테스트 요구사항

기존 테스트를 수정하기 전에 실패 재현 테스트부터 추가한다. mock만 통과시키지 말고 routing, query shape, status, source 출력까지 검증한다.

### 5.1 Cypher safety 단위 테스트

`tests/test_cypher_safety.py` 또는 적절한 신규 테스트 파일에 추가한다.

1. 아래 질의는 `malformed_label_predicate`로 거부된다.

```cypher
MATCH (text)
WHERE text:`Entry OR text`:Poem OR text:Critique
RETURN text.ID
```

2. 올바른 두 형태는 통과한다.

```cypher
WHERE text:Entry OR text:Poem OR text:Critique
```

```cypher
WHERE any(label IN labels(text) WHERE label IN ['Entry', 'Poem', 'Critique'])
```

3. `RETURN` 없는 query는 복구 가능한 `missing_return` 분류를 낸다.
4. write/call/multi-statement는 복구 불가 분류이며 여전히 차단된다.
5. 원본 query가 예외 문자열 또는 Evidence에 포함되지 않음을 확인한다.

### 5.2 graph retrieval 및 status 테스트

1. recoverable malformed query → 1회 재생성 → 정상 query 성공.
2. 두 번째도 malformed/missing `RETURN` → `invalid_query`, `no_results` 아님.
3. 정상 실행된 빈 rows → `no_results`.
4. Bolt/driver transient exception → 제한된 retry 후 `temporarily_unavailable`.
5. retry 횟수가 설정 최대치를 넘지 않음.
6. `p.ID`가 사용되고 `p.id`가 사용되지 않음을 검증한다.

### 5.3 순위 intent 및 외부 authority 테스트

아래 exact query와 한국어 동등 질문을 각각 테스트한다.

```text
Who is the most mentioned king in Sihwa ch'ongnim?
시화총림에서 가장 많이 언급된 왕은 누구인가요?
```

검증 항목:

1. ranking template 또는 제한된 structured route가 선택된다.
2. 생성/실행 query에 `` `Entry OR text` `` 같은 형태가 없다.
3. 정상 graph result의 1위 Person과 count가 답변에 사용된다.
4. `authority_fetcher` 호출 횟수는 0이다.
5. `External authority enrichment was applied ...` 문구가 없다.
6. vector에서 발견한 추가 Person은 rank 후보/출처/authority 대상으로 섞이지 않는다.
7. “Wikidata에 따르면”, “외부 authority에서 조회됨” 같은 근거 없는 문구가 없다.

추가 테스트:

```text
Who is the most mentioned king, and what does Wikidata say about that person?
```

1. graph ranking이 먼저 성공해야 한다.
2. authority fetch 대상은 1위 Person 한 명뿐이다.
3. graph ranking 실패 시 authority fetch는 0회다.

### 5.4 synthesis/source 테스트

1. `invalid_query`와 `temporarily_unavailable`이 `no_results` 문구로 렌더링되지 않는다.
2. 실패한 순위 질의에는 무관한 vector/authority source 목록이 붙지 않는다.
3. 성공한 순위 질의에는 winner와 실제 count 근거가 되는 graph provenance만 붙는다.
4. `poetrytalks wikidata: [T1052](https://poetrytalks.org/T1052)`는 내부 Topic 링크라는 프로젝트 명명 규칙을 유지한다. 이를 실제 Wikidata API 출처처럼 설명하지 않는다.

### 5.5 로마자 표기 테스트

1. `nameMR`이 있는 한국 Person의 영어 graphRAG 답변은 해당 저장값을 표시한다.
   - 예: DB의 해당 개체가 `nameMR = Chosŏn`이라면 출력에는 `Chosŏn`이 사용되고 `Joseon`으로 재로마자화되지 않는다.
   - 테스트 fixture의 실제 값이 다르면 그 fixture의 저장된 `nameMR`을 기대값으로 사용한다. 이름을 코드에 하드코딩해 변환 규칙을 시험하지 않는다.
   - `Sŏng Hyŏn`, `Hŏ Ch'ohŭi`, `Ch'wisŏn`처럼 breve/diacritic/apostrophe가 있는 fixture를 적어도 하나 포함해 저장값이 변형되지 않음을 검증한다.
2. 한국어 답변에서는 `nameKor`가 우선이며, 로마자 병기가 있는 경우 `nameMR`만 허용된다.
3. `nameMR`이 없는 한국 개체는 RR/임의 로마자를 생성하지 않고 원어 표기를 유지한다.
4. `namePY`가 있는 중국계 개체는 Hanyu Pinyin을 계속 보존한다.
5. graph-row → `Entity`/`NodeReference` → synthesis → `answer_renderer`의 전 과정에서 `name_mr`이 보존되고, MR 표기 이름도 body link 후보로 인식된다.
6. vector metadata projection 및 prompt에 `p.nameRR`, `creator_rr`, `nameRR`이 남아 있지 않음을 검증한다. 단, 과거 데이터 호환을 위해 `nameRR` key를 **읽지 않고 무시**해야 하는 정당한 경계 코드가 있다면, 그 예외와 이유를 테스트 및 구현 보고서에 명시한다.
7. RR/관용 영문명으로 검색 입력을 주더라도, 검색이 성공한 결과의 한국 관련 라틴 표기는 `nameMR`로 나온다.

### 5.6 live smoke test (권한/연결이 있을 때만)

Neo4j credential이 현재 실행 환경에 안전하게 구성되어 있을 때에만, 쓰기 없는 다음 검증을 수행한다.

1. `king`/`왕`/`王` topic과 Person 관계의 실제 개수 확인
2. Entry-only와 all-text count의 차이 확인
3. 확정 template 실행 결과 및 동률 여부 확인
4. Streamlit에서 동일 영어 질문 실행 후 다음 확인
   - 실제 winner 또는 정직한 no-results/error 상태
   - 무관한 10/14 외부 보강 없음
   - no UnknownLabelWarning
   - no `no RETURN` safety rejection

live DB가 없거나 일시적으로 연결되지 않으면 결과를 만들어내지 말고, mock/unit test 범위와 미검증 사유를 구현 보고서에 명시한다.

---

## 6. 완료 기준 (Acceptance Criteria)

다음을 모두 만족해야 작업 완료다.

- [ ] 잘못된 backtick 다중 라벨 구문이 Neo4j에 도달하지 않는다.
- [ ] recoverable 형식 오류는 최대 1회만 안전하게 재생성한다.
- [ ] 위험한 Cypher는 재생성으로 우회하지 않고 계속 차단된다.
- [ ] 실패 상태가 `no_results`로 위장되지 않는다.
- [ ] 순위/집계 질문은 authority fetch 0회가 기본이다.
- [ ] 외부 출처를 명시한 순위 질문은 graph winner를 확정한 뒤 그 winner만 보강한다.
- [ ] `p.ID`/`e.ID` 등 대문자 `ID` 규칙이 보존된다.
- [ ] Aura/Bolt transport 장애는 제한된 읽기 재시도 후 안전한 사용자 메시지로 종료한다.
- [ ] 한국 관련 개체의 사용자 노출 로마자 표기는 저장된 `nameMR`(McCune–Reischauer)만 사용하고, `nameRR`/Revised Romanization을 생성·표시하지 않는다.
- [ ] `nameMR`이 graph/vector/evidence/synthesis/body-linking 경로에서 유실되지 않으며, `nameMR` 부재 시 임의 로마자 표기를 만들지 않는다.
- [ ] 기존 테스트와 신규 테스트가 모두 통과한다.
- [ ] `git diff --check`가 실질적인 whitespace 오류 없이 통과한다.
- [ ] 구현 보고서에 아래를 포함한다.
  - 변경 파일과 핵심 변경 내용
  - 실제로 확정한 “most mentioned” count 정의
  - live smoke test 결과 또는 미실행 사유
  - 남은 제한사항/후속 사용자 결정 사항

---

## 7. 범위 밖 작업

다음은 이번 지시서의 범위가 아니다.

- Neo4j 노드/관계 데이터의 대량 수정 또는 재수입
- Person/Place AKS Digerati endpoint registry 변경
- authority source cap 기본값 변경
- link-only source를 fetchable API로 승격
- Poetry Talks 내부 링크 그룹명 `poetrytalks wikidata`의 명칭 변경
- 외부 API를 이용해 “가장 많이 언급된 왕”의 그래프 순위를 대신 계산하는 방식
- DB 값 자체를 RR에서 MR로 일괄 변환하거나, 새 로마자화 사전/라이브러리를 도입하는 방식

범위 밖 변경이 필요하다고 판단되면 임의로 구현하지 말고, 근거와 선택지를 구현 보고서에 적어 사용자 승인 요청으로 분리한다.

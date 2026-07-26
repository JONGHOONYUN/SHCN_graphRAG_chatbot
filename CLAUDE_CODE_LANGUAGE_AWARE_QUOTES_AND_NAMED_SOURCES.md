# Claude Code 작업지시서: 언어별 인용 순서와 이름이 포함된 Poetry Talks Sources

작성 기준일: 2026-07-26  
대상 저장소: `llm-chatbot-python-main`  
작업 성격: 코드 수정 + 단위/회귀 테스트  
우선순위: P0 응답 정확성 / P1 citation 가독성  

## 1. 작업 목표

아래 세 가지 결과를 구현한다.

1. 영어 질문의 응답에서 `Entry`, `Poem`, `Critique`의 저장 텍스트를 둘 이상 인용할 때 `textEng`가 `textKor`보다 먼저 나오게 한다. 이 순서는 graphRAG의 Graph/Vector evidence와 독립 vectorRAG 경로 모두에서 같아야 한다.
2. 최종 `Sources`의 `poetrytalks wikidata` 항목에 내부 노드 ID 링크뿐 아니라 해당 노드의 `nameEng`와 `nameKor`를 질문 언어 순서에 맞춰 표시한다.
3. external source/API 링크 경로는 현행 동작을 점검·회귀 검증만 하고 이번 작업에서는 수정하지 않는다.

이 문서에서 `poetrytalks wikidata`는 기존 코드가 사용하는 **Poetry Talks 내부 노드 링크 그룹명**이다. 실제 외부 Wikidata와 혼동하거나 그룹명을 번역하지 않는다. URL base는 오직 `https://poetrytalks.org/`를 사용한다.

## 2. 재현 사례와 판정

### 2.1 재현 질문

```text
How is Du Fu critiqued in Sihwa Ch'ongnim?
```

관찰된 본문은 동일 인용을 다음 순서로 노출했다.

```text
textKor: "우리나라 시로는 ..."
textEng: "Our country's poetry. ..."
```

관찰된 Sources는 다음처럼 ID만 표시했다.

```markdown
- poetrytalks wikidata: [P094](https://poetrytalks.org/P094)
- poetrytalks wikidata: [CT004](https://poetrytalks.org/CT004)
```

### 2.2 로그 해석 시 주의점

제공된 Cypher는 `Entry`가 아니라 `c:Critique`의 `c.textKor`, `c.textChi`, `c.textEng`를 `critique_text_*` 별칭으로 반환한다. 따라서 이번 결함을 `Entry.text*`에만 한정해 고치면 같은 문제가 `Critique`와 `Poem`에서 재발한다. 수정 계약은 모든 인용 가능한 텍스트 노드와 중첩 결과에 적용한다.

또한 실제 생성 Cypher의 이름 별칭은 다음처럼 현재 정규화 계약과 어긋난다.

```text
subject_person_id  + subject_name_kor / subject_name_eng
critic_person_id   + critic_name_kor / critic_name_eng
critical_term_id   + critical_term_kor / critical_term_eng
```

현재 `NodeReference` 추출기는 원칙적으로 다음 표준 형태를 기대한다.

```text
subject_person_id  + subject_person_name_kor / subject_person_name_eng
critic_person_id   + critic_person_name_kor / critic_person_name_eng
critical_term_id   + critical_term_name_kor / critical_term_name_eng
```

ID는 수집되지만 이름이 비는 이유 중 하나다.

### 2.3 종합 판정

| 항목 | 판정 | 직접 원인 | 처리 |
|---|---|---|---|
| 영어 응답의 `textKor → textEng` | 결함 | Graph row는 Cypher 반환 순서를 유지하고 `_format_graph_block()`이 raw JSON으로 직렬화함 | 수정 |
| Vector evidence의 고정 `textChi → textKor → textEng` | 결함 | `_format_vector_block()`이 `language`와 무관한 고정 tuple을 사용함 | 수정 |
| 합성 LLM의 인용 순서 | 보장 없음 | `SYNTHESIS_SYSTEM_RULES`는 verbatim만 규정하고 언어별 순서를 규정하지 않음 | 수정 |
| Sources의 ID-only 표기 | 결함 | `_collect_all_node_ids()`가 이름을 가진 `NodeReference`를 ID list로 축소함 | 수정 |
| Graph 이름 누락 | 결함 | 실제 생성 Cypher 별칭과 `_sibling_names_for_id_key()`의 sibling 규칙 불일치 | 수정 |
| 독립 vectorRAG의 metadata 전달 | 잔존 결함 | `create_stuff_documents_chain()`에 `document_prompt`가 없어 기본적으로 `page_content`만 합성 context에 들어감 | 수정 |
| 이번 질문에서 external API 링크 없음 | 정상 | 비평 관계 질문은 biography/location/external-source intent가 아니므로 authority gate가 꺼짐 | 수정 금지 |

`tools/vector.py`의 graphRAG vector projection은 이미 세 언어 텍스트와 다수 노드 이름을 조회한다. 핵심 수정 지점은 조회 데이터 자체보다 evidence 정규화, prompt formatting, deterministic Sources 조립 계층이다.

## 3. 확정 출력 계약

### 3.1 source text 순서

필드 값은 번역·요약·문장부호 정규화·공백 정리 없이 그대로 유지하고 **필드의 제시 순서만** 바꾼다.

| 응답 언어 | 우선순위 |
|---|---|
| `en` | `textEng` → `textKor` → `textChi` |
| `ko` | `textKor` → `textEng` → `textChi` |
| `zh` | `textChi` → `textKor` → `textEng` |
| 미지원/불명 | 기존 언어 fallback인 `ko` 정책 사용 |

적용 대상:

- 직접 필드: `textEng`, `textKor`, `textChi`
- Graph alias: `entry_text_*`, `poem_text_*`, `critique_text_*` 등 `<role>_text_eng|kor|chi`
- `collect()`/map/list 안의 중첩 `Entry`, `Poem`, `Critique` 텍스트
- vector evidence의 `english_translation`, `korean_translation`, `original_chinese`가 정규화된 `text*` 필드

규칙:

- 값이 없는 필드는 조용히 생략한다.
- 동일한 값이 두 속성에 저장되어 있어도 임의로 하나를 삭제하지 않는다. 각 속성은 별도 provenance를 가진 데이터다.
- `descEng`는 source text 세 필드 뒤에 둔다.
- Cypher `RETURN` key 순서나 Python dict insertion order에 최종 보장을 맡기지 않는다.
- LLM prompt만으로 보장하지 말고, LLM이 받는 evidence block 자체를 위 순서로 결정론적으로 구성한다.

### 3.2 `poetrytalks wikidata` 이름 표기

기존 `[ID](URL)`를 앞에 유지해 링크 계약과 기존 substring 기반 테스트의 호환성을 보존한다.

영어 응답:

```markdown
- poetrytalks wikidata: [P094](https://poetrytalks.org/P094) — Du Fu (두보)
```

한국어 응답:

```markdown
- poetrytalks wikidata: [P094](https://poetrytalks.org/P094) — 두보 (Du Fu)
```

이름 fallback 규칙:

1. `en`: `nameEng`를 먼저, 서로 다른 `nameKor`가 있으면 괄호 안에 추가한다.
2. `ko`: `nameKor`를 먼저, 서로 다른 `nameEng`가 있으면 괄호 안에 추가한다.
3. `zh`: 기존 중국어 응답 정책에 맞춰 `nameChi`를 먼저 사용할 수 있으나, 이 작업의 필수 계약인 `nameKor`/`nameEng`도 존재하면 함께 보존한다. 최소한 `nameKor`와 `nameEng`를 모두 버리는 구현은 금지한다.
4. 둘 중 하나만 있으면 있는 이름만 출력한다.
5. 두 값이 trim 후 같으면 한 번만 출력한다.
6. 둘 다 없으면 기존 ID-only bullet로 안전하게 fallback한다. 이름을 ID, node type, 외부 API, LLM 상식으로 만들어 내지 않는다.
7. `nameMR` 또는 `nameChi`를 누락된 `nameEng`의 값인 것처럼 바꾸어 넣지 않는다.

동일성 및 안전 규칙:

- citation identity와 dedup key는 오직 내부 `node_id`다.
- `P553`과 `P1227`처럼 같은 외부 식별자를 공유해도 서로 다른 bullet로 유지한다.
- `Q464558`, `koreanPerson_*`, `E0063034` 등 외부 authority ID를 Poetry Talks node ID로 출력하지 않는다.
- 이름의 앞뒤 공백은 제거하고 개행은 한 줄 표시를 깨지 않도록 안전하게 처리한다. source text 값에는 이 처리를 적용하지 않는다.
- `referenced_node_ids` 필터, 최초 발견 순서, URL validation, provenance breadcrumb dedup 동작은 유지한다.

### 3.3 external source/API 링크 계약 — 점검만 수행

현행 정책을 바꾸지 않는다.

- 일반 graph/vector 검색은 항상 수행할 수 있지만 external authority는 biography, 생몰년, 장소, Wikidata/외부 출처 비교 등 명시적 intent가 있을 때만 실행한다.
- 이름으로 외부 ID를 추정하지 않고 graph node에 저장된 authority ID만 사용한다.
- 최종 Sources에는 URL이 존재하면서 `status == "ok"` 또는 `status == "link_only"`인 claim만 출력한다.
- `unavailable`, `error`, `unsupported`, URL 없음은 외부 citation으로 출력하지 않는다.
- `link_only`는 내용을 조회한 사실 근거가 아니라 참고 링크로만 표시한다.
- 재현 질문은 authority intent가 아니므로 외부 링크 0건이 정상이다.

## 4. 권장 수정 순서

### Phase 0 — 기준선 고정과 실패 테스트 작성

1. 작업 시작 전 `git status --short`와 `git diff`를 확인하고 사용자 변경을 보존한다.
2. 라이브 Gemini, Neo4j, 외부 HTTP 없이 재현되는 fixture를 먼저 만든다.
3. 아래 두 실패를 테스트로 고정한 뒤 구현한다.
   - 영어 Graph/Vector evidence에서 `textEng`가 `textKor`보다 앞에 있어야 함.
   - 이름이 있는 `NodeReference(P094)`의 영어/한국어 citation이 ID-only이면 실패해야 함.

### Phase 1 — graphRAG source-text 순서를 합성 계층에서 결정론적으로 보장

주요 파일: `tools/synthesis.py`

1. 언어별 source-text priority를 한 곳에서 반환하는 pure helper를 추가한다. 이름은 프로젝트 스타일에 맞추되 Graph와 Vector formatter가 반드시 같은 helper를 사용해야 한다.
2. `_format_vector_block()`의 고정 순회 `("textChi", "textKor", "textEng", "descEng")`를 공통 priority 기반 순회로 교체한다.
3. `_format_graph_block(graph, outcome)`에 `language`를 전달하도록 signature와 호출부를 확장한다.
4. Graph row를 문자열 치환하지 말고 dict/list 구조에서 재귀적으로 복사·정렬하는 pure helper를 사용한다.
   - top-level과 nested map/list를 모두 처리한다.
   - `textEng/textKor/textChi` 및 `<role>_text_eng|kor|chi` sibling family끼리만 순서를 정한다.
   - 원본 evidence dict를 mutate하지 않는다.
   - 텍스트 값은 byte-for-byte 동일하게 유지한다.
5. `SYNTHESIS_SYSTEM_RULES`의 verbatim 규칙에 locked response language별 제시 순서를 명시한다.
6. Cypher가 어떤 key 순서로 반환하더라도 formatter가 최종 prompt 순서를 보장하게 한다.

### Phase 2 — Graph node 이름 전달 계약 정리

주요 파일: `tools/evidence.py`, `tools/cypher.py`

1. `tools/evidence.py::_sibling_names_for_id_key()`가 표준 alias와 이번 로그의 legacy 변형을 모두 안전하게 읽게 한다.
   - 표준: `<stem>_name_kor|chi|eng|mr`
   - `subject_person_id`/`critic_person_id`에 대한 legacy: `subject_name_*`/`critic_name_*`
   - `critical_term_id`에 대한 legacy: `critical_term_kor|chi|eng|mr`
   - bare nested map: `id` + `nameKor|nameChi|nameEng|nameMR`
2. 여러 Person 역할이 한 row에 있을 때 다른 역할의 이름을 잘못 결합하지 않는다. `subject_person_id`가 `critic_name_eng`를 가져오는 식의 broad fallback은 금지한다.
3. `tools/cypher.py` prompt에 multi-hop role alias를 ID와 이름 모두 포함해 정확히 고정한다.

```cypher
subject.ID      AS subject_person_id,
subject.nameKor AS subject_person_name_kor,
subject.nameEng AS subject_person_name_eng,
critic.ID       AS critic_person_id,
critic.nameKor  AS critic_person_name_kor,
critic.nameEng  AS critic_person_name_eng,
ct.ID           AS critical_term_id,
ct.nameKor      AS critical_term_name_kor,
ct.nameEng      AS critical_term_name_eng
```

4. 오래된 예시 Cypher도 all-node ID + 표준 name alias 계약에 맞춘다. 예시와 상단 규칙이 서로 다른 별칭을 가르치지 않게 한다.
5. vector metadata의 기존 이름 projection은 보존한다. `contained_critiques`/`contained_poems`에 실제 `nameKor`/`nameEng` 속성이 존재하면 함께 투영하되, null을 다른 텍스트로 대체하지 않는다.

### Phase 3 — 이름을 보존하는 deterministic citation 조립

주요 파일: `tools/synthesis.py`, 필요 시 `tools/evidence.py`와 `agent.py`

1. `_collect_all_node_ids()`의 coverage와 order 동작은 유지하되, citation 렌더 단계에서 `node_id -> merged names`를 함께 사용할 수 있는 구조를 만든다.
2. 이름 source 우선순위:
   - Graph/Vector `node_references`
   - 같은 `node_id`의 `entities`로 빈 이름만 보강
   - provenance/document에서 발견한 ID는 이름 없는 reference로 fallback
3. 같은 ID의 여러 reference는 `merge_node_references()`와 동일하게 최초 순서를 유지하고, 기존 값이 비어 있을 때만 후속 값을 채운다. 서로 다른 비어 있지 않은 이름을 임의로 덮어쓰지 않는다.
4. `build_citations()`가 3.2의 정확한 언어별 형식으로 bullet을 조립하게 한다.
5. `agent.py`의 `referenced_node_ids` 필터링과 deterministic `assemble_final_answer()` 경계를 유지한다. Sources를 다시 LLM에게 작성시키지 않는다.
6. retrieval result에 ID만 있고 이름이 없지만 Neo4j node에는 이름이 있는 경우까지 보장해야 한다면, 최종 evidence node ID들에 대해 **한 번의 bounded, parameterized batch query**로 이름을 보강한다.
   - `is_valid_node_id()`를 통과한 내부 ID만 대상에 포함한다.
   - `MATCH (n) WHERE n.ID IN $ids` 형태로 parameter를 사용한다.
   - N+1 query를 만들지 않는다.
   - 조회 실패 시 전체 응답을 실패시키지 말고 ID-only로 fallback한다.
   - 보강은 body linking / referenced-ID derivation / citation build가 공유하는 `NodeReference`에 병합한다.

### Phase 4 — 독립 vectorRAG(`text_rag.py`) 동작을 같은 계약으로 맞춤

주요 파일: `text_rag.py`, `tools/evidence.py`, `tools/synthesis.py`, `tools/answer_renderer.py`

현재 `_build_light_retrieval_query()`는 세 언어 텍스트를 metadata에 넣지만 `create_stuff_documents_chain()` 호출에 `document_prompt`가 없어 그 metadata가 LLM context에 실제로 전달된다는 보장이 없다. 또한 이 경로는 Sources를 LLM에게 쓰게 하고 결과를 그대로 반환하므로 graphRAG의 deterministic Sources 계약을 우회한다.

1. `_build_light_retrieval_query()`에 `entry_name_kor`, `entry_name_eng`를 추가하고 기존 Work ID/name/position/URL metadata를 유지한다.
2. response language별 custom `document_prompt`를 만들어 `create_stuff_documents_chain(..., document_prompt=...)`에 전달한다.
3. custom document prompt에 아래 값을 실제로 포함한다.
   - 언어별 순서가 적용된 세 본문
   - Entry ID/name/position
   - Work ID/name
   - `poetrytalks_link`
4. vectorRAG system prompt의 “한자 원문 항상 먼저” 지시를 3.1의 language-first 정책과 일치시킨다.
5. LLM에는 answer body만 쓰게 하고, `result["context"]`를 `docs_to_evidence()`로 정규화한 뒤 graphRAG와 같은 `build_citations()` + `assemble_final_answer()` 경계를 재사용한다.
6. 모델이 임의 Sources를 써도 제거되고 최종 Sources가 정확히 한 번만 붙어야 한다.
7. 이번 작업에서 Streamlit의 모드 label이나 내부 함수/세션 suffix 이름을 바꾸지 않는다. UI의 `textRAG → vectorRAG` 명칭 변경은 별도 작업지시서 범위다.

### Phase 5 — external source/API 경로 회귀 점검, 코드 변경 금지

주요 점검 파일: `tools/orchestrator.py`, `tools/external_authority.py`, `tools/synthesis.py`

다음 현행 경로가 유지되는지만 fake fetcher 단위 테스트로 확인한다.

1. 재현 질문은 `authority_intent()`의 Person/Place가 모두 false이며 외부 fetch 0회다.
2. 명시적 biography/location/external 질문은 graph에 저장된 유효 authority ID에 대해서만 registry를 거친다.
3. `status=ok`과 `link_only` + URL만 최종 Sources에 나타난다.
4. 실패/unsupported claim은 facts나 citation URL로 둔갑하지 않는다.

이번 작업에서는 다음 잠재 위험을 발견하더라도 수정하지 말고 최종 보고의 별도 후속 과제로만 남긴다.

- 최종 external citation URL의 scheme/hostname 재검증 부재
- 큰 환경설정 값에 대한 authority cap 상한 부재
- entity cap 경계에서 link-only reference까지 건너뛸 가능성
- prompt에 노출된 external claim 수와 최종 citation 전체 순회 범위의 차이

## 5. 필수 테스트

### 5.1 source text 순서

신규 `tests/test_source_text_language_order.py` 또는 기존 synthesis 테스트에 다음을 추가한다.

- Vector document에 세 필드가 모두 있을 때:
  - `en`: `textEng` index < `textKor` index < `textChi` index
  - `ko`: `textKor` index < `textEng` index < `textChi` index
  - `zh`: `textChi` index < `textKor` index < `textEng` index
- 필드 일부가 null/누락되어도 남은 필드의 상대 순서가 맞음.
- Graph top-level `critique_text_kor/chi/eng`가 영어에서 eng → kor → chi로 정렬됨.
- nested `contained_critiques`와 `contained_poems` map/list도 같은 규칙을 따름.
- 따옴표, 한자 구두점, 줄바꿈을 포함한 각 값이 입력과 정확히 동일함.
- formatter 호출 후 원본 evidence object가 변경되지 않음.

### 5.2 이름 포함 Poetry Talks citation

기존 `tests/test_poetrytalks_wikidata_group.py`, `tests/test_all_node_source_link_coverage.py`, `tests/test_deterministic_sources.py`, `tests/test_sources_language_and_urls.py`를 확장한다.

- 영어 exact output: `[P094](...) — Du Fu (두보)`
- 한국어 exact output: `[P094](...) — 두보 (Du Fu)`
- `nameEng`만 있음 / `nameKor`만 있음 / 두 값 동일 / 둘 다 없음.
- 같은 ID가 Graph ID-only와 Vector named reference에 동시에 있을 때 한 bullet에 병합됨.
- `P553`과 `P1227`은 같은 외부 Q ID를 공유해도 두 bullet로 유지됨.
- 같은 이름을 가진 서로 다른 내부 ID도 합치지 않음.
- `referenced_node_ids` filtering과 first-seen order가 유지됨.
- 외부 `Q*`, `koreanPerson_*`, `idAKSency` 값은 그룹에 들어오지 않음.
- Work/Entry/CriticalTerm 등 Person 외 node class도 같은 형식 또는 이름 없음 fallback을 사용함.
- 실제 재현 alias row를 fixture로 사용:

```python
{
    "subject_person_id": "P094",
    "subject_name_kor": "두보",
    "subject_name_eng": "Du Fu",
    "critic_person_id": "P017",
    "critic_name_kor": "허균",
    "critic_name_eng": "Hŏ Kyun",
    "critical_term_id": "CT004",
    "critical_term_kor": "고고",
    "critical_term_eng": "lofty and ancient",
}
```

각 ID의 `NodeReference`에 올바른 자기 이름만 들어가는지 검증한다.

### 5.3 독립 vectorRAG

- custom `document_prompt`가 metadata의 세 본문과 Entry/Work provenance를 실제로 포함함.
- 영어/한국어 document prompt의 텍스트 순서가 3.1과 일치함.
- `result["context"]` 기반 citation이 deterministic하게 조립됨.
- 모델이 fake `Sources`를 작성해도 제거되고 최종 Sources header가 한 번만 존재함.
- name이 있는 Entry/Work는 언어별 bilingual label, name이 없는 node는 ID-only fallback.

### 5.4 external 회귀 — 네트워크 금지

- `How is Du Fu critiqued in Sihwa Ch'ongnim?` → recording fetcher 호출 0회.
- 명시적 biography 질문 + fake `status=ok` → external citation 1개.
- fake `link_only` → localized reference-link citation.
- fake `unavailable/error/unsupported` → external citation 없음.
- 라이브 HTTP, Gemini, Neo4j 연결을 테스트 성공 조건으로 삼지 않는다.

## 6. 권장 테스트 명령

```powershell
python -m unittest tests.test_source_text_language_order
python -m unittest tests.test_poetrytalks_wikidata_group
python -m unittest tests.test_all_node_source_link_coverage
python -m unittest tests.test_deterministic_sources
python -m unittest tests.test_sources_language_and_urls
python -m unittest tests.test_pipeline
python -m unittest discover -s tests -p "test_*.py"
```

신규 테스트 파일명을 다르게 선택했다면 첫 명령만 실제 파일명에 맞춘다. 실패 시 관련 없는 기존 assertion을 삭제하거나 느슨하게 만들지 말고, 변경된 확정 출력 계약에 맞춰 필요한 assertion만 명시적으로 갱신한다.

## 7. 수동 Streamlit 확인

### 7.1 영어 graphRAG

```text
How is Du Fu critiqued in Sihwa Ch'ongnim?
```

확인 사항:

- 영어 인용이 한국어 인용보다 먼저 나온다.
- 본문에 인용한 문자열은 DB 값과 동일하다.
- Sources에 아래 형태가 보인다.

```markdown
- poetrytalks wikidata: [P094](https://poetrytalks.org/P094) — Du Fu (두보)
```

- name 속성이 실제로 없는 Entry/Critique는 ID-only여도 된다.
- 이 질문에는 external API 링크가 없어야 한다.

### 7.2 한국어 graphRAG

```text
시화총림에서 두보는 어떻게 비평되는가?
```

확인 사항:

- 한국어 인용이 영어 인용보다 먼저 나온다.
- Sources 이름이 `두보 (Du Fu)` 순서다.

### 7.3 독립 vectorRAG

Entry 본문을 직접 묻는 영어/한국어 질문을 각각 실행한다.

- LLM이 세 언어 metadata와 Entry/Work provenance를 실제로 받는다.
- 언어별 인용 순서가 graphRAG와 같다.
- Sources가 한 번만 나타나고 이름/ID/URL fallback이 계약대로다.

## 8. 완료 조건

다음을 모두 만족해야 완료다.

- [ ] 영어 응답의 모든 인용 가능한 텍스트 노드에서 `textEng`가 `textKor`보다 먼저 제시된다.
- [ ] 한국어/중국어 순서와 누락 필드 fallback도 결정론적이다.
- [ ] source text는 순서 외에 한 글자도 변형되지 않는다.
- [ ] `poetrytalks wikidata` Sources가 언어별 `nameEng`/`nameKor`를 표시한다.
- [ ] 이름이 없는 node는 링크를 잃지 않고 ID-only로 fallback한다.
- [ ] 내부 ID가 다른 node는 외부 ID나 이름이 같아도 병합되지 않는다.
- [ ] 실제 로그의 legacy alias와 새 표준 alias가 모두 안전하게 정규화된다.
- [ ] graphRAG와 독립 vectorRAG 모두 Sources를 코드가 한 번만 조립한다.
- [ ] 재현 질문의 external fetch는 0회이며 external authority 구현은 변경되지 않았다.
- [ ] 관련 단위 테스트와 전체 테스트가 통과한다.
- [ ] Claude Code 최종 보고에 변경 파일, 핵심 설계, 실행한 테스트와 결과, 남은 out-of-scope 위험을 기록한다.

## 9. 명시적 비범위

이번 작업에서 다음을 함께 고치지 않는다.

- external authority registry, API endpoint, fetch cap, URL trust 정책
- `poetrytalks wikidata` 그룹명의 변경 또는 실제 Wikidata와의 통합
- Neo4j 내부 ID가 다른 인물의 병합 정책
- 로마자 표기 정책(McCune–Reischauer)
- Streamlit `textRAG` UI label을 `vectorRAG`로 바꾸는 작업
- 검색된 모든 graph provenance를 본문 사용 여부로 추가 필터링하는 정책
- “Yi Yongjae의 특성을 Du Fu에 대한 직접 비평으로 볼 수 있는가”라는 의미론적 귀속 문제

마지막 항목은 제공된 로그의 `Full Context`가 비어 있어 현재 자료만으로 확정할 수 없다. 별도 작업을 진행한다면 Graph row와 relationship 방향, 해당 `Critique` node의 전체 subject 연결을 먼저 캡처해 검증해야 한다.

# 대형 모듈 분리 구현 보고서

작업 지시서: `CLAUDE_CODE_LARGE_MODULE_MODULARIZATION.md`
성격: **동작 보존형 구조 리팩터링**. 사용자 동작·검색 결과·출처 정책·보안
정책·호출 횟수·검색 순서는 의도적으로 하나도 바꾸지 않았다.

---

## 1. 요약

| 항목 | 결과 |
|---|---|
| 기준선 테스트 | `Ran 551 tests / OK` (작업 전 재현 확인) |
| 최종 테스트 | `Ran 581 tests / OK` (기존 551 + 신규 30) |
| compile 검사 | `python -m compileall` 통과 (exit 0) |
| 데이터 계약 parity | 88개 경계 스냅샷이 이동 전(HEAD) 대비 **완전 일치** |
| 프롬프트 바이트 동일성 | Cypher 36,467자 / ReAct 15,317자 / vector 투영 3개 언어 전부 일치 |
| `.streamlit/`, `neo4j_data_import/` | 변경 없음 (git status로 확인) |
| 신규 runtime dependency | 없음 |

대형 8개 모듈의 **6,497줄이 859줄의 얇은 facade로 축소**되고, 구현은 계층별
`chatbot/` 패키지(7,015줄, 정적 프롬프트 1,259줄 포함)로 이동했다.

| facade | 이전 | 이후 |
|---|---:|---:|
| `agent.py` | 870 | 237 |
| `text_rag.py` | 271 | 105 |
| `tools/evidence.py` | 1,153 | 81 |
| `tools/synthesis.py` | 1,071 | 82 |
| `tools/cypher.py` | 1,048 | 105 |
| `tools/external_authority.py` | 1,053 | 95 |
| `tools/orchestrator.py` | 658 | 55 |
| `tools/vector.py` | 373 | 99 |
| **합계** | **6,497** | **859** |

---

## 2. 변경한 파일과 새 파일

### 새 파일 — `chatbot/` 패키지 (39개)

| 계층 | 모듈 | 줄 | 책임 |
|---|---|---:|---|
| domain | `node_identity.py` | 167 | node ID prefix registry · Poetry Talks URL 규칙 |
| domain | `evidence_models.py` | 219 | Entity/Provenance/NodeReference/Evidence |
| domain | `evidence_merge.py` | 110 | entity 병합 · cross-bundle 수집 |
| domain | `retrieval_models.py` | 49 | `RetrievalRequest` · `EvidenceRetriever` 확장 계약 |
| retrieval | `graph_prompt.py` | 750 | Cypher 생성 프롬프트(정적) · 재시도 힌트 |
| retrieval | `graph_query.py` | 240 | 생성 Cypher 실행 · 결정론적 ranking 질의 |
| retrieval | `graph_rows.py` | 421 | graph row → Entity/Provenance/NodeReference/Evidence |
| retrieval | `vector_query.py` | 164 | rich/light retrieval_query 투영(정적) |
| retrieval | `vector_retriever.py` | 94 | Neo4jVector 생성 · 언어별 캐시 · Evidence |
| retrieval | `vector_documents.py` | 314 | vector metadata/document → Evidence |
| authority | `validators.py` | 127 | ID 패턴/변환 · AKS 응답 schema 검증 |
| authority | `parsers.py` | 400 | source별 parser(allowlist 필드만) |
| authority | `registry.py` | 337 | `AUTHORITY_REGISTRY` · source 해석 · link-only |
| authority | `cache.py` | 42 | TTL/bounded cache (성공만 저장) |
| authority | `client.py` | 40 | HTTP GET (이 패키지에서 유일하게 `requests` 사용) |
| authority | `service.py` | 186 | `fetch_authority` · `external_authority_lookup` |
| application | `retrieval_policy.py` | 184 | authority intent cue · cap 설정 |
| application | `retriever_invocation.py` | 136 | arity dispatch · user-safe status |
| application | `authority_enrichment.py` | 136 | entity별 authority fetch/기록 |
| application | `evidence_orchestrator.py` | 259 | graph → vector → authority 조립 |
| application | `graphrag_pipeline.py` | 148 | 정상 graphRAG 파이프라인 본체 |
| application | `vectorrag_pipeline.py` | 177 | textRAG prompt · chain · 이력 · 조립 |
| synthesis | `language_fields.py` | 157 | 언어별 이름 선택 · source-text 순서 |
| synthesis | `source_policy.py` | 148 | `SYNTHESIS_SYSTEM_RULES` |
| synthesis | `history_format.py` | 93 | 이력 직렬화 · `HISTORY_RULES` |
| synthesis | `evidence_format.py` | 375 | evidence block · 예산 · status/coverage |
| synthesis | `citations.py` | 374 | 결정론적 citation · `CITATION_LABELS` |
| synthesis | `prompt.py` | 83 | 언어 지시문 · 최종 합성 프롬프트 |
| legacy | `react_prompt.py` | 509 | ReAct 프롬프트 · tool 설명(정적) |
| legacy | `react_agent.py` | 174 | tool 구성 · executor · 폴백 호출 |
| legacy | `react_vector_tool.py` | 196 | 레거시 벡터 tool prompt + prose 생성 |
| legacy | `graph_qa.py` | 95 | 레거시 prose graph QA |

그 외: 각 계층의 `__init__.py` 6개(계층 규칙 문서화), `chatbot/__init__.py`.

### 새 문서·테스트
- `docs/ARCHITECTURE.md` — 계층·의존성 방향·클라이언트 소유권·확장 지점
- `tests/test_modularization_contracts.py` — 신규 30개 테스트
- `MODULARIZATION_IMPLEMENTATION_NOTE.md` — 이 문서

### 수정한 기존 파일
- facade 8개 (위 표)
- 기존 테스트 8개 — **단언 내용은 한 글자도 바꾸지 않고, 읽는 소스 파일 경로만**
  이동한 소유 모듈로 변경 (§3 참조)

---

## 3. 현재 → 신규 symbol 이동표

| 이전 위치 | 심볼(대표) | 신규 위치 | facade |
|---|---|---|---|
| `tools/evidence.py` | `POETRYTALKS_BASE_URL`, `NODE_ID_PREFIXES`, `split_node_id`, `is_valid_node_id`, `node_type_for_id`, `poetrytalks_url`, `_linked_id`, `normalize_entry_position` | `chatbot/domain/node_identity.py` | re-export |
| `tools/evidence.py` | `Entity`, `Provenance`, `NodeReference`, `Evidence`, `make_node_reference`, `merge_node_references` | `chatbot/domain/evidence_models.py` | re-export |
| `tools/evidence.py` | `merge_entities`, `collect_entities`, `collect_node_references`, `collect_person_entities` | `chatbot/domain/evidence_merge.py` | re-export |
| `tools/evidence.py` | `docs_to_evidence`, `document_to_parts`, `entities_from_vector_meta`, `node_references_from_vector_meta` | `chatbot/retrieval/vector_documents.py` | re-export |
| `tools/evidence.py` | `graph_rows_to_evidence`, `entities_from_graph_row`, `provenance_from_graph_row`, `node_references_from_graph_row`, `_ROW_ID_SUFFIXES` | `chatbot/retrieval/graph_rows.py` | re-export |
| `tools/synthesis.py` | `SYNTHESIS_SYSTEM_RULES` | `chatbot/synthesis/source_policy.py` | re-export |
| `tools/synthesis.py` | `serialize_chat_history`, `HISTORY_RULES`, 이력 예산 상수 | `chatbot/synthesis/history_format.py` | re-export |
| `tools/synthesis.py` | `format_evidence_for_prompt`, `_format_*_block`, 예산 상수, `RETRIEVAL_STATUS_MESSAGES`, `both_retrievals_failed` | `chatbot/synthesis/evidence_format.py` | re-export |
| `tools/synthesis.py` | `build_citations`, `CITATION_LABELS`, `_format_citation_name`, `_collect_all_node_ids` | `chatbot/synthesis/citations.py` | re-export |
| `tools/synthesis.py` | `source_text_priority`, `reorder_source_text_fields`, `_work_name_bilingual` | `chatbot/synthesis/language_fields.py` | re-export |
| `tools/cypher.py` | `CYPHER_GENERATION_TEMPLATE`, `_SYNTAX_RETRY_HINT`, `_SHAPE_RETRY_HINT` | `chatbot/retrieval/graph_prompt.py` | re-export |
| `tools/cypher.py` | `retrieve_graph_evidence`, `retrieve_role_ranking_evidence`, `_extract_intermediate` | `chatbot/retrieval/graph_query.py` | 위임 |
| `tools/cypher.py` | `cypher_qa_safe` | `chatbot/legacy/graph_qa.py` | 위임 |
| `tools/cypher.py` | `_safe_graph`, `cypher_prompt`, `cypher_qa`, `cypher_qa_structured` | **그대로 유지**(composition root) | — |
| `tools/vector.py` | `_build_retrieval_query` | `chatbot/retrieval/vector_query.py` | re-export |
| `tools/vector.py` | retriever 캐시, `retrieve_sihwa_evidence` | `chatbot/retrieval/vector_retriever.py` | 위임 |
| `tools/vector.py` | `instructions`, `_build_prompt`, `get_poetry_plot` | `chatbot/legacy/react_vector_tool.py` | 위임 |
| `text_rag.py` | `_build_light_retrieval_query` | `chatbot/retrieval/vector_query.py` | re-export |
| `text_rag.py` | `TOP_K`, textRAG retriever 캐시 | `chatbot/retrieval/vector_retriever.py` | 위임 |
| `text_rag.py` | `FALLBACK_HINT`, `_build_prompt`, 체인·이력·조립 | `chatbot/application/vectorrag_pipeline.py` | 위임 |
| `tools/external_authority.py` | ID 정규식, `_digits_from`, `validate_aks_*_response` | `chatbot/authority/validators.py` | re-export |
| `tools/external_authority.py` | `parse_*` 7종 | `chatbot/authority/parsers.py` | re-export |
| `tools/external_authority.py` | `AuthoritySourceConfig`, `AUTHORITY_REGISTRY`, `resolve_source`, `link_only_reference` | `chatbot/authority/registry.py` | re-export |
| `tools/external_authority.py` | `_authority_cache`, `clear_authority_cache` | `chatbot/authority/cache.py` | re-export |
| `tools/external_authority.py` | `_fetch`, `MAX_RESPONSE_BYTES` | `chatbot/authority/client.py` | re-export |
| `tools/external_authority.py` | `fetch_authority`, `external_authority_lookup` | `chatbot/authority/service.py` | re-export |
| `tools/orchestrator.py` | cue 목록, `authority_intent`, `needs_authority`, cap 상수 | `chatbot/application/retrieval_policy.py` | re-export |
| `tools/orchestrator.py` | `_fn_accepts_arity`, `_safe_retrieve`, `_call_fetcher` | `chatbot/application/retriever_invocation.py` | re-export |
| `tools/orchestrator.py` | `_enrich_entity`, `_record_*` | `chatbot/application/authority_enrichment.py` | re-export |
| `tools/orchestrator.py` | `gather_graphrag_evidence`, 기본 retriever | `chatbot/application/evidence_orchestrator.py` | re-export |
| `agent.py` | `LANGUAGE_LABEL`, `_build_language_directive`, `synthesis_prompt` 텍스트 | `chatbot/synthesis/prompt.py` | re-export |
| `agent.py` | `synthesize_answer` 본문, `_load_bounded_history` | `chatbot/application/graphrag_pipeline.py` | 위임 |
| `agent.py` | `agent_prompt`, tool 설명, `ITERATION_LIMIT_*`, 파싱오류 안내 | `chatbot/legacy/react_prompt.py` | re-export |
| `agent.py` | `tools`, `agent`, `agent_executor`, `chat_agent`, `general_chat`, `_generate_response_react` | `chatbot/legacy/react_agent.py` | 위임 |
| `agent.py` | `generate_response`, `_react_fallback_or_safe`, `_graphrag_history` | **그대로 유지**(최상위 오류 정책 + composition root) | — |

---

## 4. 최종 package dependency 방향

`docs/ARCHITECTURE.md` §1에 도식으로 기록. 요약:

```text
presentation/composition (bot.py · agent.py · text_rag.py · tools/*)
   → application → retrieval / authority → synthesis → domain
legacy는 composition root의 오류 정책에서만 호출되며 정상 파이프라인이 import하지 않음
```

금지 방향 6가지(domain→인프라, synthesis→클라이언트, retrieval/application→
session_state, requests→authority client 외, legacy→정상 파이프라인, facade↔구현
순환)는 모두 `tests/test_modularization_contracts.py`가 AST/서브프로세스 import로
강제한다.

---

## 5. 유지한 compatibility facade

| facade | 형태 |
|---|---|
| `tools/evidence.py` | 순수 re-export (밑줄 helper 포함, 로직 0줄) |
| `tools/synthesis.py` | 순수 re-export |
| `tools/external_authority.py` | 순수 re-export (+ 기존 테스트가 `ea.requests.get`을 patch하므로 `requests` 이름 유지) |
| `tools/orchestrator.py` | 순수 re-export |
| `tools/cypher.py` | composition root + 3개 함수 얇은 위임 |
| `tools/vector.py` | composition root + session state 읽기 + 얇은 위임 |
| `text_rag.py` | composition root + session state 읽기 + 얇은 위임 |
| `agent.py` | composition root + session state 읽기 + 최상위 오류 정책 + 얇은 위임 |

facade에는 registry·상수·로직 사본이 없다. `tests/…::TestFacadesReExportSingleImplementation`
가 facade 심볼과 구현 심볼이 **동일 객체(`assertIs`)** 임을 검사한다.

---

## 6. 제거하거나 변경하지 않은 기존 동작

- graph → vector → authority 순서, 병렬화 없음
- top-k(그래프RAG 기본 / textRAG 10), authority cap(Person 10 · Place 5 ·
  source 2 · 비교 시 4), timeout·retry 횟수
- LLM/embedding 모델명, temperature, token limit
- cache key·TTL·"성공만 저장" 정책
- ReAct iteration 15회, 파싱 오류 자기수정 안내
- `::graphRAG` / `::textRAG` 이력 namespace와 경로별 저장 소유권 1곳
- 예외 분류별 폴백 여부와 사용자 메시지
- 모든 LLM 생성 Cypher의 `tools/cypher_safety.py` read-only 검증 경로
  (`safe_graph` wrapper는 복제되지 않았고 `tools/cypher_safety.py`는 미변경)
- `GraphCypherQAChain`이 최종적으로 쓰이지 않는 QA prose까지 생성하는 현재 비용
  (§4.4가 명시적으로 이번 작업에서 건드리지 말라고 한 항목)

---

## 7. 테스트 결과

```text
기준선 (작업 전, HEAD d2f3ed7)
  python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools tests   → exit 0
  python -m unittest discover -s tests -p 'test_*.py'                                        → Ran 551 tests / OK

최종
  python -m compileall -q bot.py agent.py llm.py graph.py utils.py text_rag.py tools chatbot tests → exit 0
  python -m unittest discover -s tests -p 'test_*.py'                                              → Ran 581 tests / OK
```

각 Phase 종료 시점마다 전체 스위트를 실행했고, 모든 Phase에서 551개(신규 추가
이후 581개)가 통과했다. 기준선에서 실패하던 테스트는 없었다.

### 신규 테스트 30개 (`tests/test_modularization_contracts.py`)
- facade 심볼 동일성 4종 + 공개 import 경로 해석 + 진입점 정의 검사
- 순수 계층 import 부작용 0회(별도 인터프리터에서 `sys.modules` 검사) 6종
- 전 모듈 일괄 import로 순환 import 부재 검사
- 계층 의존성 방향 AST 검사 6종 (+ 실행 로직 모듈 500행 상한)
- 확장 지점 4종 (RetrievalRequest 필드, 새 retriever 등록, block formatter
  dispatch 등록 소스, 미래 스키마 scaffolding 부재)
- 정상 파이프라인 호출·이력 parity 5종 + 오케스트레이션 순서 parity 2종

### 기존 테스트 수정 내역 (단언 내용 불변, 읽는 파일 경로만 변경)

| 파일 | 변경 |
|---|---|
| `test_pipeline.py` | Cypher 템플릿 AST 추출 → `graph_prompt.py`; `HISTORY_RULES`/`{chat_history}` → `synthesis/prompt.py`; `add_user_message`/`add_ai_message` → `graphrag_pipeline.py` |
| `test_all_node_source_link_coverage.py` | `cypher_src` → `graph_prompt.py` |
| `test_mr_romanization.py` | vector 투영 → `vector_query.py`; Cypher 프롬프트 → `graph_prompt.py` |
| `test_phase6_rag_config.py` | light 투영 추출 → `vector_query.py` |
| `test_vectorrag_document_prompt.py` | 투영/프롬프트/조립 단언을 `vector_query.py`·`vectorrag_pipeline.py`로 분리 |
| `test_deterministic_sources.py` | 조립·저장 순서 → `graphrag_pipeline.py` |
| `test_language_routing.py` | `gather_graphrag_evidence` 호출문 → `graphrag_pipeline.py` (시그니처·세션키 단언은 `agent.py` 유지) |
| `test_sources_language_and_urls.py` | 합성 human message 정규식 → `synthesis/prompt.py` |

`git diff -- tests/`로 확인 가능하듯 **기대 문자열은 한 건도 바뀌거나 완화되지
않았고**, 검사 대상 파일 경로만 이동한 소유 모듈을 가리키도록 갱신했다.

---

## 8. LLM / Neo4j / HTTP call parity 확인 결과

### (a) 데이터 계약 parity — 88개 경계 전수 비교
`git worktree`로 작업 전 HEAD를 꺼내 동일한 probe 스크립트를 양쪽에서 실행하고
JSON 스냅샷을 비교했다. 대상:

`split_node_id`·`is_valid_node_id`·`node_type_for_id`·`poetrytalks_url`·
`normalize_entry_position` / `Entity.to_dict`·`dedup_key`·`merge_entities` /
`make_node_reference`·`merge_node_references` / `Provenance.to_dict` /
`document_to_parts`(정상·퇴화 3종)·`node_references_from_vector_meta`·
`docs_to_evidence` / `entities_from_graph_row`·`provenance_from_graph_row`·
`node_references_from_graph_row`·`graph_rows_to_evidence` / `collect_entities`·
`collect_node_references` / 4개 언어별 `format_evidence_for_prompt`·
`build_citations`(필터 유무)·`assemble_final_answer`·`source_text_priority`·
`reorder_source_text_fields`·`_format_citation_name`·`_rebuild_vector_prov_label` /
`serialize_chat_history`·`both_retrievals_failed`·`retrieval_failure_message` /
registry 전체·`validate_id`·`request_id`·`link_only_reference`·parser 7종·
AKS 응답 validator / `fetch_authority`(주입 fetcher 7종)·
`external_authority_lookup` / `authority_intent`·`needs_authority`·
`_ranking_authority_intent` / `gather_graphrag_evidence` 6개 질문 시나리오
(반환 key·statuses·coverage·evidence·entities·**fetch 호출 목록**) + 실패 경로.

결과: **차이 0건**.

### (b) 프롬프트 바이트 동일성
- `CYPHER_GENERATION_TEMPLATE` 36,467자 — AST로 추출해 HEAD와 비교, 일치
  (이동 중 후행 공백 3곳이 사라져 복원했다)
- 레거시 ReAct `agent_prompt` 15,317자 — 일치 (후행 공백 1곳 복원)
- `_build_retrieval_query` / `_build_light_retrieval_query` — ko/en/zh 3개 언어
  모두 출력 문자열 일치

### (c) 호출 횟수·순서
- 정상 graphRAG: 합성 LLM 호출 정확히 1회, 이력 저장 정확히 1회 (신규 테스트)
- 전체 검색 실패 단락: LLM 호출 0회, 이력 저장 0회 (신규 테스트)
- graph → vector → authority 순서와 authority cap/source cap (신규 + 기존 테스트)
- external 성공 cache / 실패 미cache (기존 테스트, 동일 cache dict 객체 사용)
- transient 오류에서만 ReAct 폴백, 그 외 폴백 0회 (기존 `test_phase3_fallback_policy`)

### (d) HTTP
authority fetch 경로는 `chatbot/authority/client.py` 한 곳으로 모였고, 정책 값
(timeout·content-type·2MB 상한·User-Agent)과 "응답 본문을 로그에 남기지 않음"이
그대로다. 기존 테스트가 `tools.external_authority.requests.get`을 patch하는 방식도
그대로 동작한다.

---

## 9. secrets / data 파일 무변경 확인

```text
git status --short -- .streamlit neo4j_data_import   → (출력 없음)
```

`.streamlit/secrets.toml`은 열지도, 읽지도, 수정하지도 않았다. 이 보고서와 모든
로그·테스트 출력에 API key·Neo4j password·connection URI는 포함되지 않는다.

---

## 10. 발견했지만 범위 밖이라 구현하지 않은 것

1. **사용되지 않는 Graph QA LLM 호출** — 구조화 retrieval이 쓰는
   `cypher_qa_structured`는 rows만 사용하는데도 체인이 QA prose까지 생성한다.
   작업 지시서 §4.4/§8.3이 이번 작업에서 금지한 항목이라 그대로 두었다.
   (비용·응답 timing에 영향을 주므로 별도 작업 권장)
2. **`agent.poetry_chat` / `chat_prompt` 는 어디서도 호출되지 않는다.** 레거시
   ReAct tool은 `general_chat()`의 동적 prompt를 쓴다. 하위 호환을 위해 그대로
   유지했고, 제거는 별도 작업 후보다.
3. **미사용 호환 alias** — `POETRYTALKS_BASE`, `CACHE_KEY`, `ID_TRANSFORMS`,
   `collect_person_entities`, `person_entities_from_vector_meta`. 전부 과거
   호환용이며 이번 작업의 "deprecated 경로 삭제 금지"(§Phase 10.3)에 따라 유지.
4. **`tools/cypher.py`의 미사용 `import streamlit as st`** — facade 재작성 과정에서
   제거했다. 이 모듈은 streamlit을 전혀 쓰지 않았고, `llm.py`/`graph.py`가 이미
   streamlit을 import하므로 import 부작용은 동일하다. (유일하게 코드를 "지운" 변경)
5. **`RETRIEVAL_OUTCOMES` 상수**는 선언만 되어 있고 검증에 쓰이지 않는다.
   status 토큰 검증기를 붙이는 것은 후속 후보.
6. **`errors.py`의 `RetrievalError` / `ModelResponseError`** 는 retrieval 계층에서
   실제로 raise되지 않는다(현재는 status 토큰으로 degrade). 예외 taxonomy 정리는
   별도 작업.

---

## 11. 후행 용어집·논문 작업이 사용할 수 있는 확장 지점

작업 지시서 §9의 7개 질문에 모두 "예"로 답할 수 있다.

| 질문 | 답 | 근거 |
|---|---|---|
| 새 retriever를 기존 graph/vector 수정 없이 등록? | 예 | `gather_graphrag_evidence(..., graph_retriever=…)` 주입. 신규 테스트가 실증 |
| 새 formatter를 대규모 조건문 수정 없이 추가? | 예 | `EVIDENCE_BLOCK_FORMATTERS` / `EVIDENCE_BLOCK_ORDER` dispatch |
| 새 citation 전략을 graph citation 변경 없이 추가? | 예 | `citations.build_citations`의 소스별 분기 |
| 질문 전처리 결과를 별도 객체로 전달? | 예 | `RetrievalRequest`(question/question_language/response_language/history_text) |
| retriever별 timeout/top-k/budget 분리? | 예 | retriever가 주입되므로 호출자가 소유; cap은 `retrieval_policy` |
| 미래 Neo4j label을 domain 전역 enum 수정 없이? | 예 | `graph_rows.py` mapper에서 처리 |
| Streamlit 없이 pipeline 단위 테스트? | 예 | 파이프라인이 체인·이력 팩토리를 주입받음. 신규 테스트가 실증 |

**추가하지 않은 것**(§3.4/§8.1 준수): `Glossary`/`Research*` 노드·관계·필드,
신규 `Evidence.kind`, 빈 placeholder 모듈, 범용 plugin/DI framework.
신규 테스트가 이 부재를 회귀 검사한다.

---

## 12. 남아 있는 기술 부채

1. **정적 소스 검사 테스트의 경로 결합** — 프롬프트/투영 텍스트를 파일 경로로
   pin하는 테스트가 다수라, 앞으로 그 텍스트가 다시 이동하면 테스트 경로도 함께
   옮겨야 한다. (AST 심볼 기준 검사로 바꾸면 결합이 느슨해짐)
2. **`llm.py` / `graph.py`의 module-level 클라이언트** — 인증 게이트가 `bot.py`에
   있는 현재 구조에 의존한다. 제거 조건은 `docs/ARCHITECTURE.md` §3에 명시.
3. **graph read client와 history write client 미분리** — 이번 작업은 인터페이스
   경계(주입)만 만들었고 실제 계정 분리는 배포 작업으로 남겼다(§Phase 9.7).
4. **`chatbot/retrieval/graph_rows.py` 421줄 / `chatbot/synthesis/citations.py`
   374줄 / `evidence_format.py` 375줄** — 500행 미만이지만 상위권이다. 각각 단일
   변경 이유를 갖는다고 판단해 더 쪼개지 않았다.
5. **`tools/` facade와 `chatbot/` 구현의 이중 경로** — 의도된 하위 호환 상태다.
   deprecated 경로 삭제는 이번 작업의 명시적 금지 사항이라 다음 단계 과제.

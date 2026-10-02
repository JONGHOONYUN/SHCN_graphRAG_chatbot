# Graph QA 호출 제거 및 실제 전후 측정 결과

실행일: 2026-10-02 (Asia/Seoul)

## 결과

제거 전 실제 로그 수집, 정상 GraphRAG의 Graph QA 호출 제거, 회귀 테스트,
동일 질문 재측정과 비교 보고서 생성을 완료했다.

- 변경 전 전체 테스트: **681개 통과**.
- 변경 후 전체 테스트: **699개 통과** (신규 18개).
- 실제 요청: 변경 전 8건, 변경 후 8건. **16건 모두 success**, fallback 0건.
- 실제 모델: `gemini-2.5-flash`, temperature 0. 설정 변경 없음.
- 정상 생성형 GraphRAG: 요청당 LLM **3회 → 2회**, Graph QA **1회 → 0회**.
- 결정적 왕 순위 검색: 요청당 최종 합성 LLM **1회 유지**.
- 검색된 graph/vector 노드 ID 집합: **8쌍 모두 동일**.
- 실제 답변 전체 링크 집합: **4/8쌍 동일**, 나머지는 차이가 있어 아래 품질 검토에 기록했다.

구현·회귀 테스트·측정 완료와 실제 답변의 의미적 품질 승인은 구분한다.
이번 실제 응답에서 인물 이름과 링크 대상의 불일치가 발견되어, 모든 답변의
품질이 유지됐다고 결론내리지 않는다. 자동 배포나 원격 반영은 수행하지 않았다.

## 측정 조건

실제 `agent.generate_response`를 호출했다. LLM, embedding, Neo4j graph/vector
검색 및 선택된 외부 전거 조회는 기존 운영 코드를 사용했다.

고정 질문을 아래 순서로 2회씩 실행했다.

| ID | 질문 | 경로 |
|---|---|---|
| person | 허초희는 누구인가? | 생성형 GraphRAG |
| work | 패관잡기의 저자는 누구이며, 어떤 작품인가? | 생성형 GraphRAG |
| entries | 허초희가 언급된 시화 항목을 찾아 근거와 함께 설명해줘. | 생성형 GraphRAG |
| ranking | 시화총림에서 가장 많이 언급된 왕은 누구인가? | 결정적 순위 검색 |

- 질문/응답 언어는 한국어로 고정했다.
- 요청마다 새 메모리 대화 이력을 사용했다. 기존 Neo4j 대화 기록에 시험 답변을 저장하지 않았다.
- 정상 경로와 ReAct 폴백의 이력 팩토리를 모두 시험용 메모리로 격리했다.
- 초기 클라이언트·벡터 인덱스 준비는 시간 측정 밖에서 수행했다.
- 외부 전거 캐시는 각 요청 전에 비웠다. 벡터 retriever는 프로세스 내 재사용했다.
- before와 after는 별도 프로세스에서 실행했다.
- 모델/temperature/패키지 버전, Cypher·합성 prompt와 검색 설정의 소스 해시가 같음을 비교 도구로 확인했다.
- Neo4j 노드 수는 전후 8,682개로 같았다. 이는 데이터 전체의 불변 스냅샷을 증명하지는 않는다.
- 이벤트에는 질문·답변·Cypher 원문을 넣지 않았다. 고정 시험 질문의 실제 답변은 별도 로컬 평가 파일에 저장했다.

측정 지연은 **백엔드 비교용 지연**이다. UI 렌더링, 네트워크로 전달되는 화면,
운영 Neo4j 대화 이력 읽기/쓰기 시간은 포함하지 않는다. 시험 중 관측되는 history
span은 메모리 이력 동작이므로 운영 DB 이력 성능으로 해석하면 안 된다.

## 실제 성능 수치

아래 표는 **생성형 GraphRAG 3개 질문 × 2회 = 전후 6쌍**만 집계했다.
결정적 순위 요청은 원래 Graph QA가 없으므로 이 집계에서 분리했다.

| 지표 | 제거 전 | 제거 후 | 변화 |
|---|---:|---:|---:|
| 요청당 LLM 호출 수 | 3 | 2 | 33.33% 감소 |
| 요청당 Graph QA 호출 수 | 1 | 0 | 제거 |
| 평균 입력 토큰 | 22,031.17 | 20,457.33 | 7.14% 감소 |
| 평균 출력 토큰 | 4,707.17 | 2,909.17 | 38.20% 감소 |
| 평균 전체 토큰 | 26,738.33 | 23,366.50 | **12.61% 감소** |
| 백엔드 응답시간 중앙값 | 59.95초 | 48.91초 | **18.40% 감소** |
| 백엔드 응답시간 p95 | 68.49초 | 60.26초 | 12.00% 감소 |
| graph 검색시간 중앙값 | 14.31초 | 6.28초 | 56.14% 감소 |
| 요청 성공 | 6/6 | 6/6 | 동일 |
| 토큰 usage 완전 제공 | 6/6 | 6/6 | 동일 |

변경 전 Graph QA 호출 자체는 요청당 평균 **2,807.83토큰 / 6.22초**였다.
전체 토큰과 응답시간의 전후 차이에는 최종 생성문 길이, provider 응답시간 및
외부 전거 응답의 변동도 포함되므로, 12.61%/18.40%를 순수한 제거 효과만의
인과 추정값이나 모든 사용자 질문에 대한 성능 보장으로 해석하지 않는다.

p95는 nearest-rank 방식이다. 표본이 6개라 최대 관측값과 같으며 운영 부하
분포를 추정하기에는 부족하다. 이번 작업은 기능 변경을 검증하는 소규모 측정이다.

순위 요청까지 포함한 전체 8건 기준:

- LLM 호출 총합: **20 → 14회**.
- 관측된 모델 토큰 합계: **175,268 → 155,042**.
- 전후 시험에서 관측된 모델 토큰 총합: **330,310**.
- 실제 API를 사용했다. 위 토큰 수는 embedding 비용이나 초기화 호출을 포함한
  청구 총액이 아니며, SDK 내부 재시도 횟수도 관측하지 못한다.

## 실제 답변 및 인용 검토

자동 비교와 직접 확인 결과를 구분했다.

1. **검색 결과 ID 보존:** graph/vector 노드 ID 집합은 모든 8쌍에서 동일했다.
   이 비교만으로 각 노드 속성, 외부 전거 내용, 최종 문장의 완전한 일치까지
   증명하지는 않는다.
2. **고정 입력의 기능 보존:** 동일한 가짜 Cypher·DB 행·최종 모델 출력을 주입한
   회귀 테스트에서는 전체 Evidence, 최종 답변, citations, 저장된 이력이 같았다.
   top-k, 빈 그래프, 안전 거부, 구문 오류 후 1회 재생성도 검증했다.
3. **실제 생성 결과 차이:** 인물/항목 질문 4쌍에서 답변 링크 집합이 달랐다.
   항목 질문의 인용 개수는 33 → 27, 인물 두 번째 반복은 42 → 40이었다.
   본문에서 사용한 노드를 기준으로 일부 인용을 고르는 기존 정책 때문에 최종
   생성 내용의 변화는 인용 차이로 이어질 수 있다. 최종 답변의 문자열 동일성은
   실제 모델에 대해 보장하지 않는다.
4. **구체적 품질 문제:** 변경 후 `entries` 답변에
   `[허봉](https://poetrytalks.org/P017)`이라는 링크가 반복됐다.
   읽기 전용 Neo4j 확인 결과 `P017.nameKor`는 **허균**이다.
   따라서 이름과 링크 대상이 불일치한다. 이 사례는 수정된 행 추출 로직의
   evidence 누락을 입증하지는 않으며, Graph QA 제거가 원인이라고 단정할 수도
   없다. 하지만 실제 품질 검토가 필요한 명확한 문제로 남긴다.
5. **유지된 주요 답변:** 작품 질문의 어숙권 저자 식별과 왕 순위 질문의
   선조 10회 결론은 전후에 유지됐다. 이는 해당 실행의 답변 비교이며,
   모든 학술 사실에 대한 별도 전수 검증을 의미하지 않는다.

후속 품질 작업은 모델이 생성한 인물 링크의 표시명과 대상 노드 이름/별칭을
검증하는 것부터 시작할 수 있다. 이번 변경은 Graph QA 제거에 한정해 기존
프롬프트·링크 렌더러·도메인 데이터는 수정하지 않았다.

## 코드 변경

### 운영 코드

- `chatbot/retrieval/graph_chain.py`
  - 공개 builder에 `return_direct` 옵션을 추가했다. 기본값은 False다.
- `tools/cypher.py`
  - 정상 구조화 체인만 `return_direct=True`로 만들었다.
  - 레거시/ReAct용 자연어 체인은 기존 QA 호출을 유지한다.
- `chatbot/retrieval/graph_query.py`
  - direct 경로의 `result` 리스트와 기존 `intermediate_steps.context`를 모두 지원한다.
  - 명시적인 빈 context는 빈 결과로 유지하며 prose는 행으로 해석하지 않는다.
- `chatbot/application/graphrag_pipeline.py`
  - 호출 구조 설명만 갱신했다. 실행 로직은 변경하지 않았다.

Cypher 생성 prompt, query 안전 경계, retry, top-k, 모델 설정, 검색 순서,
최종 합성 prompt, 오류·폴백 정책은 변경하지 않았다. 기존 공개 함수의 인자와
Evidence 반환 계약도 유지했다.

### 자동화와 검증

- `scripts/graph_qa_benchmark.py`: 실제 전후 측정. 기존 출력 폴더 덮어쓰기 거부,
  요청마다 로그 flush, 연속 실패 시 중지, 자격 증명과 기존 verbose 본문 출력 차단.
- `scripts/graph_qa_report.py`: 비교 조건/실행 완전성 검증, 질문별 대응,
  성공한 생성형 요청과 전체 요청 분리 집계. 미보고 토큰을 0으로 취급하지 않는다.
- `tests/test_graph_qa_removal.py`: 신규 17개 테스트.
- `tests/test_observability_pipelines.py`: 운영 structured/legacy 플래그 분리 테스트 1개 추가.
- 기존 관측성 fixture·기대값을 2회 호출 구조로 갱신했다. legacy QA 계측과
  민감정보 marker 검증은 별도 경로에서 계속 수행한다.
- `docs/OBSERVABILITY.md`, `docs/ARCHITECTURE.md`: 현재 호출 구조와 측정 방법 갱신.
- `.gitignore`: 로컬 비교 로그·답변 자료 `artifacts/graph_qa_*/` 제외.

## 실행 및 검증 기록

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'

# 변경 전
python -B -m unittest discover -s tests -p 'test_*.py'
# Ran 681 tests / OK
python -B scripts/graph_qa_benchmark.py --stage before --output artifacts/graph_qa_20261002/before
# 실제 8건 / exit 0

# 코드 변경 후
python -B -m unittest discover -s tests -p 'test_graph_qa_removal.py'
# Ran 17 tests / OK
python -B -m unittest discover -s tests -p 'test_observability_*.py'
# Ran 101 tests / OK
python -B -m unittest discover -s tests -p 'test_*.py'
# Ran 699 tests / OK
python -B tests/observability_baseline.py
# 오프라인 8개 시나리오 / exit 0 (토큰은 fixture 값)
python -B scripts/graph_qa_benchmark.py --stage after --output artifacts/graph_qa_20261002/after
# 실제 8건 / exit 0
python -B scripts/graph_qa_report.py --before artifacts/graph_qa_20261002/before --after artifacts/graph_qa_20261002/after --output artifacts/graph_qa_20261002/comparison
# 비교 완료 / exit 0
git diff --check
# 통과
```

현재 소스에서 `--stage before`를 실행하면 structured chain 설정 불일치로 중지한다.
before를 재현하려면 제거 전 소스 사본이 필요하다. 기존 로그를 다시 집계하는
작업은 API를 호출하지 않으며, 새 출력 디렉터리를 지정하면 된다.

## 결과 파일

- [제거 전 manifest](artifacts/graph_qa_20261002/before/manifest.json)
- [제거 전 관측 로그](artifacts/graph_qa_20261002/before/events.jsonl)
- [제거 후 관측 로그](artifacts/graph_qa_20261002/after/events.jsonl)
- [자동 비교 보고서](artifacts/graph_qa_20261002/comparison/comparison.md)
- [기계 판독 비교 결과](artifacts/graph_qa_20261002/comparison/comparison.json)
- [제거 전 실제 답변](artifacts/graph_qa_20261002/before/answers.jsonl)
- [제거 후 실제 답변](artifacts/graph_qa_20261002/after/answers.jsonl)

원본 로그와 답변 자료는 로컬에 유지되며 Git에서는 제외된다. 이 구현 보고서는
소스와 함께 보관할 수 있도록 자격 증명과 전체 원문을 포함하지 않는다.

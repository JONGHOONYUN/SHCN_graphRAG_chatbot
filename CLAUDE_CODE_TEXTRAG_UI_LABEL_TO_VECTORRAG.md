# Claude Code 작업 지시서: 프론트엔드 `textRAG` 라벨을 `vectorRAG`로 변경

## 1. 작업 목적

Streamlit 챗봇 프론트페이지에서 사용자에게 노출되는 `textRAG` 명칭을
`vectorRAG`로 변경한다.

변경 전:

```text
graphRAG 모드
textRAG 모드
현재 모드: textRAG
꺼짐 (textRAG): Entry 본문 의미 기반 벡터 검색
```

변경 후:

```text
graphRAG 모드
vectorRAG 모드
현재 모드: vectorRAG
꺼짐 (vectorRAG): Entry 본문 의미 기반 벡터 검색
```

이번 작업은 **사용자에게 보이는 UI 명칭 변경**이다. 내부 모드 식별자,
파일명, 함수명, Neo4j 대화 이력 namespace까지 `vectorRAG`로 마이그레이션하는
작업이 아니다.

---

## 2. 현재 구조

### 2.1 UI 모드 선택

`bot.py`의 Streamlit toggle은 다음 내부 값을 사용한다.

```python
chatbot_mode = "graphRAG" if is_graphrag else "textRAG"
```

- toggle 켜짐: 내부 모드 `"graphRAG"`
- toggle 꺼짐: 내부 모드 `"textRAG"`

### 2.2 응답 생성 라우팅

`bot.py`의 `handle_submit()`은 내부 모드 값으로 backend를 선택한다.

```python
if mode == "graphRAG":
    from agent import generate_response
    response = generate_response(message)
else:
    from text_rag import generate_text_rag_response
    response = generate_text_rag_response(message)
```

### 2.3 대화 이력 분리

현재 대화 이력은 다음 내부 namespace를 사용한다.

```text
graphRAG → <session_id>::graphRAG
textRAG  → <session_id>::textRAG
```

`messages_by_mode`도 `"graphRAG"`와 `"textRAG"`를 key로 사용한다.

이 내부 값들은 기존 세션 호환성과 mode별 이력 분리를 위해 유지한다.

---

## 3. 핵심 구현 정책

### 3.1 내부 식별자와 표시 라벨을 분리

`bot.py`에 한 곳에서 관리되는 display-label 매핑을 추가한다.

권장 형태:

```python
MODE_DISPLAY_LABELS = {
    "graphRAG": "graphRAG",
    "textRAG": "vectorRAG",
}


def mode_display_label(mode: str) -> str:
    return MODE_DISPLAY_LABELS.get(mode, mode)
```

사용자 화면에 mode 이름을 출력할 때는 내부 `chatbot_mode` 값을 직접
보여주지 말고 `mode_display_label(chatbot_mode)`를 사용한다.

현재 다음 코드는 내부 값 `textRAG`를 그대로 노출하므로 반드시 수정한다.

```python
st.caption(
    f"**현재 모드**: `{chatbot_mode}`  \n"
    "※ 모드별로 별도의 대화 이력이 유지됩니다."
)
```

권장 변경:

```python
st.caption(
    f"**현재 모드**: `{mode_display_label(chatbot_mode)}`  \n"
    "※ 모드별로 별도의 대화 이력이 유지됩니다."
)
```

### 3.2 사용자 노출 문구만 변경

`bot.py`에서 최소한 다음 사용자 노출 문구를 변경한다.

#### Sidebar toggle 도움말

변경 전:

```text
꺼짐 (textRAG): Entry 본문 의미 기반 벡터 검색.
```

변경 후:

```text
꺼짐 (vectorRAG): Entry 본문 의미 기반 벡터 검색.
```

#### vector 검색 모드 초기 인사

변경 전:

```text
시화총림 DB 챗봇 — textRAG 모드
```

변경 후:

```text
시화총림 DB 챗봇 — vectorRAG 모드
```

변수명 `GREETING_TEXTRAG`은 내부 구현 이름이므로 이번 작업에서 굳이
변경하지 않는다. 변수 안의 사용자 노출 문자열만 수정한다.

#### 현재 모드 caption

내부 `"textRAG"` 값을 display-label 매핑을 통해 `vectorRAG`로 표시한다.

### 3.3 graphRAG 명칭은 유지

다음 사용자 노출 명칭은 변경하지 않는다.

```text
graphRAG
graphRAG 모드
```

vector 검색으로 답하기 어려운 구조적 질문에 대해 `graphRAG 모드로
전환해 주세요`라고 안내하는 문구도 그대로 유지한다.

---

## 4. 절대 변경하지 말아야 할 항목

단순 전역 검색·치환으로 모든 `textRAG` 문자열을 `vectorRAG`로 바꾸지 않는다.

다음 항목은 반드시 기존 값을 유지한다.

### 4.1 내부 mode key

```python
chatbot_mode = "graphRAG" if is_graphrag else "textRAG"
```

### 4.2 Streamlit session-state key

```python
st.session_state["messages_by_mode"]["textRAG"]
```

구체적인 코드 표현은 달라도 `"textRAG"` key 자체는 유지한다.

### 4.3 Neo4j 대화 이력 suffix

```python
f"{get_session_id()}::textRAG"
```

이를 `::vectorRAG`로 바꾸면 기존 text/vector 모드 대화 이력이 분리되거나
유실된 것처럼 보일 수 있다.

### 4.4 backend 파일·함수·클래스 이름

다음 이름은 변경하지 않는다.

```text
text_rag.py
generate_text_rag_response()
_get_text_retriever_for_lang()
TestTextRagWording
TestTextRagRetrievalMetadataContract
```

### 4.5 graphRAG/vector 검색 동작

이번 작업에서 다음 로직은 변경하지 않는다.

- vector index 선택
- Neo4j retrieval query
- `TOP_K`
- embedding 모델
- graphRAG와 vector 검색 모드의 라우팅
- 대화 이력 저장 방식
- Poetry Talks URL 생성
- Sources 생성
- 프롬프트의 근거·언어 정책

---

## 5. 권장 수정 파일

### 5.1 필수: `bot.py`

수정 대상:

1. 내부 mode → 사용자 표시명 매핑 추가
2. Sidebar 도움말의 `꺼짐 (textRAG)` 문구
3. `GREETING_TEXTRAG` 안의 `textRAG 모드` 문구
4. 현재 mode caption의 직접적인 `chatbot_mode` 출력

### 5.2 필수: 관련 테스트

기존 UI 문구 검증이 있는 `tests/test_pipeline.py` 또는 별도의 작은
UI-label 테스트 파일에 회귀 테스트를 추가한다.

`bot.py`는 import 시 Streamlit 앱 코드와 인증 로직이 실행될 수 있으므로,
기존 테스트 패턴처럼 source를 UTF-8로 읽어 검사하거나 display-label
로직을 부작용 없는 작은 모듈로 분리한다. 단, 이번처럼 작은 UI 변경을 위해
불필요하게 대규모 리팩터링하지 않는다.

### 5.3 선택: 사용자 문서

README나 SETUP 문서에 실제 사용자 모드명이 설명되어 있고 `textRAG`가
노출된다면 해당 설명만 `vectorRAG`로 변경한다. 현재 사용자 문서에 관련
표현이 없다면 새 설명을 억지로 추가하지 않는다.

내부 개발 문서·주석·테스트 클래스명의 `textRAG`는 내부 구현을 가리키므로
일괄 변경하지 않는다.

---

## 6. 필수 회귀 테스트

### 6.1 display-label 매핑

다음 계약을 검증한다.

```python
assert mode_display_label("graphRAG") == "graphRAG"
assert mode_display_label("textRAG") == "vectorRAG"
assert mode_display_label("unknown") == "unknown"
```

### 6.2 사용자 노출 문자열

최소한 다음 조건을 검증한다.

```text
Sidebar off 설명에 "꺼짐 (vectorRAG)"가 존재
초기 인사에 "— vectorRAG 모드"가 존재
현재 모드 caption이 display-label을 사용
사용자 노출 형태 "꺼짐 (textRAG)"가 존재하지 않음
사용자 노출 형태 "— textRAG 모드"가 존재하지 않음
```

파일 전체에서 `textRAG` 문자열이 없어야 한다고 테스트하면 안 된다.
내부 key, suffix, 함수 설명에는 합법적으로 남아 있어야 한다.

### 6.3 내부 호환성 유지

다음 계약을 함께 검증한다.

```text
내부 off-mode key가 계속 "textRAG"
messages_by_mode에 "textRAG" key가 계속 존재
text_rag.py의 session suffix가 계속 "::textRAG"
graphRAG 선택 시 agent.generate_response() 호출
vectorRAG로 표시되는 off 상태에서
text_rag.generate_text_rag_response() 호출
```

### 6.4 전체 테스트

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

테스트 로그에 의도적으로 발생시키는 warning이 있어도 최종 결과가
`OK`인지 확인한다.

---

## 7. 수동 검증 시나리오

Streamlit을 완전히 재시작한 뒤 새 브라우저 세션에서 확인한다.

```powershell
streamlit run bot.py
```

### 시나리오 A — toggle 켜짐

기대 결과:

```text
현재 모드: graphRAG
초기 인사: graphRAG 모드
질문 제출 시 agent.generate_response() 경로 사용
```

### 시나리오 B — toggle 꺼짐

기대 결과:

```text
Sidebar 도움말: 꺼짐 (vectorRAG)
현재 모드: vectorRAG
초기 인사: vectorRAG 모드
질문 제출 시 generate_text_rag_response() 경로 사용
```

### 시나리오 C — 모드 왕복 전환

1. graphRAG에서 메시지를 한 번 전송한다.
2. vectorRAG로 전환해 다른 메시지를 전송한다.
3. 다시 graphRAG로 돌아간다.

기대 결과:

- 두 모드의 화면 표시명은 `graphRAG` / `vectorRAG`다.
- 내부 대화 이력은 기존처럼 서로 분리된다.
- mode 전환 시 `KeyError`, 이력 초기화, backend 역전이 발생하지 않는다.

---

## 8. 완료 조건

다음 조건을 모두 만족해야 작업 완료로 판단한다.

- [ ] 프론트페이지에 사용자 모드명 `vectorRAG`가 표시된다.
- [ ] `꺼짐 (textRAG)`가 `꺼짐 (vectorRAG)`로 변경된다.
- [ ] vector 검색 모드 초기 인사가 `vectorRAG 모드`로 표시된다.
- [ ] 현재 모드 caption이 `textRAG`가 아닌 `vectorRAG`를 표시한다.
- [ ] `graphRAG` 표시와 동작은 변경되지 않는다.
- [ ] 내부 mode key `"textRAG"`가 유지된다.
- [ ] `messages_by_mode["textRAG"]` 호환성이 유지된다.
- [ ] Neo4j history suffix `::textRAG`가 유지된다.
- [ ] `text_rag.py`와 기존 backend 함수명이 유지된다.
- [ ] graphRAG/vector 검색 라우팅이 변경되지 않는다.
- [ ] 관련 회귀 테스트와 전체 테스트가 통과한다.
- [ ] Streamlit 재시작 후 두 모드의 표시와 이력 분리를 수동 검증한다.

---

## 9. Claude Code 최종 보고 형식

작업 완료 후 다음 내용을 간결하게 보고한다.

1. 변경한 파일 목록
2. 변경한 사용자 노출 문구
3. 유지한 내부 식별자와 session suffix
4. 추가·수정한 테스트
5. 전체 테스트 결과
6. Streamlit 수동 검증 여부와 결과
7. 미완료 사항 또는 발견된 별도 결함


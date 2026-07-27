# Claude Code 작업지시서: 혼합 문자 질문의 언어 판별 및 검색·응답 언어 분리

작성 기준일: 2026-07-27  
대상 저장소: `llm-chatbot-python-main`  
작업 성격: 코드 수정 + 단위/통합/회귀 테스트  
핵심 재현 입력: `How is 杜甫 critiqued?`  
필수 기대 결과: `question_language=en`, `response_language=en`, 영어 vector index 검색, 영어 답변  

## 0. 작업 목표

현재의 “한글 한 글자라도 존재 → 한국어, 아니면 한자 한 글자라도 존재 → 중국어” 방식은 질문 문법보다 개체명의 문자에 더 큰 영향을 받는다. 그 결과 아래 영어 질문이 중국어로 오판된다.

```text
How is 杜甫 critiqued?
```

이 작업에서는 다음을 구현한다.

1. 개체명에 포함된 한글·한자보다 질문을 구성하는 영어/한국어/중국어 문법 신호를 우선한다.
2. 질문의 언어(`question_language`)와 최종 응답 언어(`response_language`)를 분리한다.
3. 명시적 언어 고정 문구는 실제 검색 질문에서 제거하고, 응답 언어에만 적용한다.
4. vector index는 `question_language`, LLM 답변·Sources·인용 순서·오류 문구·external locale은 `response_language`를 사용한다.
5. 기존 호출부의 급격한 파손을 막기 위해 `effective_language = response_language` 호환 alias를 유지한다.
6. Streamlit/Gemini/Neo4j를 import하지 않는 순수 언어 정책 모듈과 네트워크 없는 회귀 테스트를 추가한다.

애플리케이션 코드 이외의 데이터, Neo4j schema, embedding, 외부 authority endpoint는 변경하지 않는다.

## 1. 현재 동작과 결함 원인

### 1.1 현재 자동 판별

`bot.py::detect_language()`는 다음 선착순 정규식을 사용한다.

```python
if re.search(r"[가-힣]", text):
    return "ko"
if re.search(r"[一-鿿]", text):
    return "zh"
if re.search(r"[A-Za-z]", text):
    return "en"
return "ko"
```

이 방식은 문자 수, 문장 문법, 질문어, 서술 구조를 보지 않는다. 따라서 `How is 杜甫 critiqued?`에서 영어 문장 전체보다 `杜甫` 두 글자가 먼저 판정되어 `zh`가 된다.

### 1.2 현재 상태값 결합

`bot.py`는 매 턴 다음 한 값만 만든다.

```python
effective_language = locked_language or detect_language(prompt)
```

이 한 값이 다음을 동시에 제어한다.

- 최종 답변 언어
- vector 검색 index
- source text 제시 순서
- Sources header/name 순서
- 오류·fallback 문구
- external authority locale

따라서 `영어로 답변해줘. 두보는 어떻게 평가되는가?` 같은 입력은 응답만 영어가 되는 것이 아니라 한국어 질문을 영어 vector index에서 검색하게 된다.

### 1.3 테스트 구조상 제약

`bot.py`는 import 시점에 Streamlit page config, 인증, sidebar/widget, chat loop를 실행한다. 언어 함수를 테스트하기 위해 `bot.py`를 직접 import하면 UI 부작용이나 `st.stop()`이 발생할 수 있다. 언어 정책은 반드시 독립 pure module로 분리한다.

## 2. 필수 상태 및 API 계약

### 2.1 세 가지 언어 상태

| 상태 | 의미 | 사용처 |
|---|---|---|
| `question_language` | 언어 제어 문구를 제거한 실제 질문의 언어 | vector index, 검색 query 해석 |
| `response_language` | 이번 턴 최종 출력 언어 | LLM directive, Sources, 인용 순서, 오류 문구, external locale |
| `locked_language` | 사용자가 세션에 고정한 응답 언어 | `response_language` override |

기존 호환 alias:

```python
effective_language = response_language
```

`effective_language`는 제거하지 않는다. 기존 `agent.py`, fallback 테스트, external helper가 단계적으로 마이그레이션되는 동안 응답 언어 alias로 유지한다. 단, 새 코드가 vector index를 선택할 때 `effective_language`를 읽는 것은 금지한다.

### 2.2 pure module

`tools/language_policy.py`를 신설한다. 동등한 이름을 사용할 수 있으나 다음 조건은 필수다.

- Python 표준 라이브러리만 사용한다(`re`, `unicodedata`, `dataclasses`, `typing` 등).
- `streamlit`, `agent`, `text_rag`, Neo4j, LangChain, LLM 모듈을 import하지 않는다.
- import만으로 환경 변수, 네트워크, UI, DB 작업을 수행하지 않는다.
- 판별 상수와 regex는 module-level에서 한 번만 compile한다.

권장 public API:

```python
detect_question_language(text: str) -> str
detect_language_control(text: str) -> LanguageControl
remove_language_control(text: str, spans) -> str
resolve_languages(prompt: str, locked_language: str | None = None) -> LanguageResolution
```

`LanguageControl`과 `LanguageResolution`은 dataclass 또는 동일하게 테스트 가능한 불변 구조로 구현한다.

권장 `LanguageResolution` 필드:

```text
original_prompt
question_text
question_language
response_language
locked_language
action              # lock | release | none
control_only        # 실제 질문이 비었는지
scores/debug_info   # 선택 사항, 원문 전체를 로그에 남기지 말 것
```

## 3. 1. 변경할 판별 원칙

단순히 `[A-Za-z]` 검사를 `[一-鿿]`보다 앞으로 옮기지 않는다. 그렇게 하면 `《Sihwa Ch'ongnim》中如何评价杜甫？`처럼 영문 서명이 포함된 중국어 질문을 영어로 오판한다.

새 판별 순서는 다음과 같다.

```text
명시적 응답 언어 제어문 탐지 및 span 분리
        ↓
제어문을 제거한 실제 question_text 확보
        ↓
영어·한국어·중국어 문법 신호 계산
        ↓
문법 신호가 강한 언어 우선
        ↓
문법 신호가 약하거나 없을 때 문자 분포로 fallback
        ↓
완전 동점/무문자 입력은 ko fallback
```

핵심 불변조건:

- `How is 杜甫 critiqued?`에서 `杜甫`는 개체명이고 `How is ... critiqued`가 문장 문법이므로 `en`이다.
- `Who is 이규보?`도 영어 문법이므로 `en`이다.
- `杜甫如何被评价？`는 중국어 문법이므로 `zh`다.
- `두보(杜甫)는 어떻게 평가되는가?`는 한국어 문법이므로 `ko`다.
- LLM 또는 외부 언어 감지 API를 호출하지 않는다. 판별은 로컬·결정론적·재현 가능해야 한다.

## 4. 2. 권장 판별 로직

### 4.1 입력 정규화

1. `None`은 빈 문자열로 안전하게 처리한다.
2. Unicode NFC 정규화를 적용한다.
3. 대소문자 비교가 필요한 영문 cue에는 `casefold()`를 사용한다.
4. 원문 질문을 번역하거나 개체명 철자를 변경하지 않는다.
5. 정규화한 복사본은 판별에만 사용하며 사용자 표시 원문은 보존한다.

### 4.2 문법 신호

최소한 다음 범주를 중앙 상수로 관리한다. 실제 regex는 오탐을 줄이도록 영문 word boundary와 한국어 어절/어미 경계를 사용한다.

영어 신호:

```text
질문어: who, what, when, where, why, how, which
조동사/계사: am, is, are, was, were, do, does, did,
             has, have, had, can, could, will, would, should, may, might
명령형: tell me, explain, describe, compare, list, show
```

한국어 신호:

```text
질문어: 누구, 무엇, 뭐, 어떻게, 어디, 왜, 언제, 어느
질문/요청 어미: 인가, 인가요, 입니까, 습니까, 는가, 나요,
                알려줘, 알려주세요, 설명해줘, 설명해주세요
조사형 신호: 은/는, 이/가, 을/를, 에서, 에게
```

한국어 조사는 한 글자 substring을 전역 검색하지 말고 한글 어절 끝에 붙은 형태만 제한적으로 인식한다. 인명 내부 음절을 조사로 잘못 세지 않게 한다.

중국어 신호:

```text
질문/서술: 如何, 什么, 什麼, 谁, 誰, 哪, 哪些,
           为什么, 為什麼, 是否, 怎么, 怎麼,
           请问, 請問, 评价, 評價
문말/문법: 吗, 嗎, 呢
```

`中`, `在`, `被` 같은 단일 문자는 인명·서명에도 나타날 수 있으므로 강한 문법 cue와 같은 가중치를 주지 않는다. 필요하면 별도 약한 cue로 둔다.

### 4.3 문자 신호

- 영어: Latin word token 수. 가능하면 ASCII뿐 아니라 `Hŏ`, `Ch'ongnim` 같은 Unicode Latin 문자를 `unicodedata`로 인식한다.
- 한국어: 완성형 한글 syllable 수와 필요한 경우 Jamo/Compatibility Jamo 범위.
- 중국어/한자: 기본 CJK Unified Ideographs와 일반적으로 사용하는 Extension/Compatibility 범위.
- 문자 신호는 개체명 하나가 긴 문장을 덮어쓰지 못하도록 상한을 둔다.

### 4.4 점수 공식

한 곳에 상수로 정의하고 테스트가 기대값을 검증할 수 있게 한다.

권장 기준:

```python
GRAMMAR_WEIGHT = 5
CONTENT_CAP = 6

score_en = en_grammar_matches * GRAMMAR_WEIGHT + min(latin_word_count, CONTENT_CAP)
score_ko = ko_grammar_matches * GRAMMAR_WEIGHT + min(hangul_syllable_count, CONTENT_CAP)
score_zh = zh_grammar_matches * GRAMMAR_WEIGHT + min(han_character_count, CONTENT_CAP)
```

이 값은 필수 숫자라기보다 최소 행동 계약이다. 다른 수치를 선택해도 아래 전체 fixture를 만족해야 하며, “문법 cue 하나 이상이 삽입 개체명 한두 글자보다 강하다”는 조건을 유지한다.

### 4.5 tie-break

결정 순서를 명시적으로 구현한다.

1. 총점이 높은 언어
2. 총점 동점이면 grammar match 수가 많은 언어
3. 다시 동점이면 해당 script content unit이 많은 언어
4. 완전 동점이면 기존 호환 순서 `ko → zh → en`
5. 문자 신호가 전혀 없으면 `ko`

동일 입력은 항상 같은 결과를 반환해야 한다. dict/set iteration order에 결과를 맡기지 않는다.

### 4.6 필수 예상 결과

| 입력 | 기대 `question_language` | 판정 이유 |
|---|---:|---|
| `How is 杜甫 critiqued?` | `en` | 영어 질문어+계사+서술 구조 |
| `Who is 이규보?` | `en` | 영어 질문 문법, 한글은 개체명 |
| `杜甫如何被评价？` | `zh` | `如何`, `评价` 중국어 문법 |
| `杜甫如何被評價？` | `zh` | 번체 중국어 cue |
| `《Sihwa Ch'ongnim》中如何评价杜甫？` | `zh` | 영문 서명보다 중국어 질문 문법 우선 |
| `두보(杜甫)는 어떻게 평가되는가?` | `ko` | 조사+한국어 질문어/어미 |
| `시화총림에서 Du Fu는 어떻게 평가되는가?` | `ko` | 한국어 문장 문법, 영문은 개체명 |
| `Du Fu` | `en` | Latin-only fallback |
| `杜甫` | `zh` | Han-only fallback |
| `두보` | `ko` | Hangul-only fallback |
| `123?! 😊` | `ko` | 무문자 기본값 |

## 5. 3. 질문 언어와 응답 언어 분리

### 5.1 상태 계산

```python
question_language = detect_question_language(question_text)
response_language = locked_language or question_language
effective_language = response_language  # backward compatibility only
```

### 5.2 라우팅 계약

| 소비자 | 전달할 언어 |
|---|---|
| `rag_config.index_config_for()` / vector retriever | `question_language` |
| Graph Cypher 검색 | 언어 제어문이 제거된 `question_text` 원문; 별도 번역 금지 |
| graphRAG final synthesis directive | `response_language` |
| vectorRAG answer prompt | `response_language` |
| vectorRAG source-text ordering/document prompt | `response_language` |
| `build_citations`, `assemble_final_answer`, Sources header | `response_language` |
| localized fallback/error | `response_language` |
| external authority fetch language/label | `response_language` |

### 5.3 대표 분리 사례

입력:

```text
영어로 답변해줘. 두보는 어떻게 평가되는가?
```

기대:

```text
question_text     = 두보는 어떻게 평가되는가?
question_language = ko
response_language = en
vector index      = EntryTextsKor / textKor
final answer      = English
Sources header    = Sources
```

입력:

```text
Answer in Korean: How is 杜甫 critiqued?
```

기대:

```text
question_text     = How is 杜甫 critiqued?
question_language = en
response_language = ko
vector index      = EntryTextsEng / textEng
final answer      = Korean
Sources header    = 출처
```

## 6. 4. 언어 제어 문구는 판별·검색 대상에서 제외

### 6.1 기존 패턴 보존 및 확장 방식

현재 `EXPLICIT_LOCK_PATTERNS`와 `RELEASE_LOCK_PATTERNS`의 의도를 유지하되, 단순 언어/boolean 반환 대신 match span을 함께 반환한다.

요구사항:

- `answer/respond/reply ... in English|Korean|Chinese`
- `use/switch/change to ...`
- `영어로 답변`, `한국어로 대답`, `중국어로 응답`
- `用中文回答`, `请用英文回答`, `用韩文回答`
- `auto-detect language`, `follow my question language`
- `언어 락 해제`, `자동 언어 감지`
- 기존 일반 문장 `I love English literature`는 언어 고정으로 오탐하지 않는다.

### 6.2 span 제거

1. 모든 control match의 `(start, end)`를 수집한다.
2. 겹치거나 인접한 span은 병합한다.
3. 뒤쪽 span부터 제거하여 index drift를 방지한다.
4. 제거 후 남은 구두점(`:`, `,`, `.`, `。`)과 연속 공백만 최소 정리한다.
5. 실제 개체명, 따옴표, 아포스트로피, 원질문 문자는 바꾸지 않는다.
6. 사용자 화면과 user-visible `messages_by_mode`에는 original prompt를 표시한다.
7. graph/vector retrieval과 최종 질문 evidence에는 정리된 `question_text`를 사용한다.

### 6.3 제어 우선순위

- 같은 입력에 명시적 lock과 release가 모두 매치되면 현재 정책대로 lock이 우선한다.
- 새로운 명시적 lock은 기존 `locked_language`를 교체한다.
- release는 기존 lock을 제거하고, 같은 입력의 `question_text` 언어를 응답 언어로 사용한다.
- 여러 서로 다른 lock 대상이 한 입력에 동시에 존재하는 비정상 입력은 결정론적으로 처리하고 해당 정책을 테스트에 고정한다. 기존 호환을 우선한다면 현재 pattern-order first match를 유지한다.

### 6.4 control-only 입력

`Answer in English`, `언어 락 해제`처럼 제어문을 제거한 뒤 실제 질문이 비면 graph/vector retrieval을 호출하지 않는다.

- lock-only: 새 응답 언어로 설정 완료 안내를 결정론적으로 반환한다.
- release-only: 자동 질문 언어 판별 재활성화 안내를 반환한다.
- 이 응답은 factual RAG 답변이 아니므로 Sources를 붙이지 않는다.
- `question_language`는 다음으로 확정한다: lock-only는 target language, release-only는 제거 전 control command 자체를 `detect_question_language(original_prompt)`로 판별한 언어. 그래도 신호가 없으면 기존 기본값 `ko`를 사용한다.

## 7. 5. 권장 의사코드

```python
def resolve_languages(prompt: str, locked_language: str | None = None):
    original = prompt or ""
    control = detect_language_control(original)
    question_text = remove_language_control(original, control.spans).strip()

    if control.lock_language is not None:
        next_locked = control.lock_language
        action = "lock"
    elif control.release:
        next_locked = None
        action = "release"
    else:
        next_locked = locked_language
        action = "none"

    if question_text:
        question_language = detect_question_language(question_text)
    elif control.lock_language:
        question_language = control.lock_language
    elif control.release:
        question_language = detect_question_language(original)
    else:
        question_language = "ko"

    response_language = next_locked or question_language

    return LanguageResolution(
        original_prompt=original,
        question_text=question_text,
        question_language=question_language,
        response_language=response_language,
        locked_language=next_locked,
        action=action,
        control_only=not bool(question_text),
    )
```

`bot.py` 적용 예:

```python
resolution = resolve_languages(
    prompt,
    st.session_state.get("locked_language"),
)

if resolution.locked_language:
    st.session_state["locked_language"] = resolution.locked_language
else:
    st.session_state.pop("locked_language", None)

st.session_state["question_language"] = resolution.question_language
st.session_state["response_language"] = resolution.response_language
st.session_state["effective_language"] = resolution.response_language
```

## 8. 코드 수정 순서 및 파일별 지시

### Phase 0 — 기준선과 실패 테스트

1. `git status --short`, `git diff`를 확인하고 기존 사용자 변경을 보존한다.
2. 라이브 Streamlit, Gemini, Neo4j 없이 실패하는 테스트부터 추가한다.
3. 핵심 fixture `How is 杜甫 critiqued? == en`이 기존 코드에서 실패함을 확인한다.

### Phase 1 — pure language policy 구현

대상: 신규 `tools/language_policy.py`

1. Unicode 정규화, script feature 추출, grammar cue, 점수/tie-break를 구현한다.
2. language control regex와 span 추출/제거를 이 모듈의 single source of truth로 이동한다.
3. `detect_question_language`, `resolve_languages`를 순수 함수로 제공한다.
4. 현재 `bot.py::detect_language`, `detect_explicit_lock`, `detect_release_request`를 다른 코드가 참조할 가능성에 대비해 얇은 호환 wrapper/re-export를 둘 수 있다. 중복 regex 정의는 남기지 않는다.

### Phase 2 — Streamlit session 상태 적용

대상: `bot.py`

1. raw prompt 표시·메시지 저장은 유지한다.
2. `resolve_languages()` 결과를 session state 네 키에 반영한다.
3. backend에는 `question_text`와 두 언어를 전달한다.
4. control-only 입력은 RAG backend를 호출하지 않고 localized 확인문을 반환한다.
5. lock은 graphRAG/vectorRAG 공용 session state로 계속 유지한다.
6. UI label 변경, greeting 번역, 신규 언어 selector 추가는 이번 범위가 아니다.

### Phase 3 — graphRAG 전파 분리

대상: `agent.py`, `tools/orchestrator.py`, `tools/vector.py`

1. `agent.generate_response()`와 `synthesize_answer()`가 `question_language`와 `response_language`를 명시적으로 받을 수 있게 한다. 기존 호출자를 위해 optional/default fallback을 유지한다.
2. synthesis prompt, evidence formatting, citations, final assembly, error/fallback에는 `response_language`를 사용한다.
3. `gather_graphrag_evidence()`에 backward-compatible optional `question_language` 또는 `retrieval_language`를 추가한다.
   - 기존 positional `language` 인자를 삭제하거나 의미를 조용히 바꾸지 않는다.
   - vector retriever에는 `question_language`를 전달한다.
   - external authority fetch와 coverage 문구에는 `response_language`를 전달한다.
4. Graph retriever에는 번역하지 않은 `question_text`를 전달한다. Graph Cypher 검색은 response lock에 따라 질문을 번역하지 않는다.
5. ReAct fallback과 `General Chat`도 검색/입력은 `question_text`, 최종 출력 directive는 `response_language`를 사용한다.
6. `tools/vector.py::retrieve_sihwa_evidence()`의 `language`는 query index 언어라는 점을 docstring/parameter name으로 명확히 한다.
7. 레거시 `get_poetry_plot()`는 retriever에는 `question_language`, `_build_prompt()`에는 `response_language`를 사용한다.

### Phase 4 — 독립 vectorRAG 전파 분리

대상: `text_rag.py`, `tools/vectorrag_prompt.py`

현재 `_get_text_retriever_for_lang(lang)`은 같은 `lang`으로 index를 선택하고 `prepare_documents_for_prompt(docs, lang)`까지 cached lambda 안에서 수행한다. 이 구조를 그대로 두면 질문 언어와 응답 언어가 다를 때 인용 순서가 query 언어로 고정된다.

필수 수정:

1. base retriever cache key와 index 선택은 `question_language`로 한다.
2. `prepare_documents_for_prompt(docs, response_language)`는 요청 시점에 적용하고 cached query-language lambda에 캡처하지 않는다.
3. `document_prompt_for_lang`, `_build_prompt`, source-text order, citations, assembly, fallback은 `response_language`를 사용한다.
4. 검색에 전달하는 문장은 control phrase가 제거된 `question_text`다.
5. 다음 조합을 반드시 지원한다.

```text
question_language=en + response_language=ko
  → EntryTextsEng 검색
  → Korean answer prompt
  → textKor-first 인용 순서
  → 출처 header
```

### Phase 5 — 기존 하류 정책 보존

대상 점검: `rag_config.py`, `tools/synthesis.py`, `tools/answer_renderer.py`, `tools/external_authority.py`

- `rag_config.INDEX_BY_LANG` 값은 변경하지 않는다.
- `ko/en/zh` 이외의 언어를 새로 추가하지 않는다.
- 기존 source-text order는 유지한다.

```text
response en: textEng → textKor → textChi
response ko: textKor → textEng → textChi
response zh: textChi → textKor → textEng
```

- Sources 이름/헤더 및 오류 메시지는 `response_language`를 따른다.
- external authority의 API 선택·URL·cap은 변경하지 않고 locale만 `response_language`가 전달되는지 회귀 확인한다.

## 9. 6. 필요한 테스트

### 9.1 pure detector 테스트

신규 `tests/test_language_policy.py`를 만든다. `bot.py`는 import하지 않는다.

필수 assertion:

```python
assert detect_question_language("How is 杜甫 critiqued?") == "en"
assert detect_question_language("Who is 이규보?") == "en"
assert detect_question_language("杜甫如何被评价？") == "zh"
assert detect_question_language("杜甫如何被評價？") == "zh"
assert detect_question_language("《Sihwa Ch'ongnim》中如何评价杜甫？") == "zh"
assert detect_question_language("두보(杜甫)는 어떻게 평가되는가?") == "ko"
assert detect_question_language("시화총림에서 Du Fu는 어떻게 평가되는가?") == "ko"
assert detect_question_language("Du Fu") == "en"
assert detect_question_language("杜甫") == "zh"
assert detect_question_language("두보") == "ko"
assert detect_question_language("123?! 😊") == "ko"
```

추가:

- 빈 문자열/`None` 안전 처리
- NFC/NFD 입력
- Unicode Latin diacritic(`Hŏ`, `Ch'ongnim`)
- 일반 문장 `I love English literature`가 lock 요청으로 오탐되지 않음
- 동일 입력 반복 호출의 결과가 항상 같음
- regex가 긴 입력에서도 비정상적으로 느려지지 않음

### 9.2 control 및 state resolver 테스트

```python
r = resolve_languages("Answer in Korean: How is 杜甫 critiqued?")
assert r.question_text == "How is 杜甫 critiqued?"
assert r.question_language == "en"
assert r.response_language == "ko"
assert r.locked_language == "ko"
```

필수 matrix:

- `영어로 답변해줘. 두보는 어떻게 평가되는가?` → q=`ko`, response=`en`
- `请用英文回答。杜甫如何被评价？` → q=`zh`, response=`en`
- prior lock=`ko` + 중국어 질문 → q=`zh`, response=`ko`, lock 유지
- 새로운 영어 lock → 기존 한국어 lock 교체
- release + 한국어 질문 → lock 제거, q/response=`ko`
- lock과 release 동시 매치 → lock 우선
- control-only lock/release → retrieval 불필요 상태
- span 제거 후 개체명/아포스트로피/구두점 보존

여러 turn sequence를 plain dict/local variable로 검증한다. Streamlit session state가 없어도 상태 전이가 테스트되어야 한다.

### 9.3 routing 통합 테스트

신규 `tests/test_language_routing.py` 또는 기존 injected-mock 테스트를 확장한다. 네트워크와 실제 DB를 사용하지 않는다.

1. 영어 질문 + 한국어 응답 lock:
   - vector retriever가 `en`을 받음
   - synthesis/prompt/citation이 `ko`를 받음
   - external fetch locale이 `ko`를 받음
2. 한국어 질문 + 영어 응답 lock:
   - vector retriever가 `ko`를 받음
   - answer/source order가 `en`을 받음
3. Graph retriever가 control phrase 없는 원질문을 받음.
4. `effective_language == response_language` 호환 alias 확인.
5. 기존 `gather_graphrag_evidence(question, "ko", ...)` positional 호출이 계속 작동함.
6. 독립 vectorRAG에서 base retriever cache는 q-lang, document preparation은 response-lang임.
7. control-only 입력에서 graph/vector/external mock 호출이 모두 0회임.

### 9.4 기존 회귀 테스트

다음 계약을 유지한다.

- `tests/test_phase6_rag_config.py`: index mapping/unknown→ko
- `tests/test_source_text_language_order.py`: response language별 인용 순서
- `tests/test_vectorrag_document_prompt.py`: metadata 전달 및 deterministic Sources
- `tests/test_phase3_fallback_policy.py`: `effective_language` 기반 기존 fallback 호환
- `tests/test_pipeline.py`: orchestrator signature와 외부 authority 라우팅

### 9.5 권장 테스트 명령

```powershell
python -m unittest tests.test_language_policy
python -m unittest tests.test_language_routing
python -m unittest tests.test_phase6_rag_config
python -m unittest tests.test_source_text_language_order
python -m unittest tests.test_vectorrag_document_prompt
python -m unittest tests.test_phase3_fallback_policy
python -m unittest tests.test_pipeline
python -m unittest discover -s tests -p "test_*.py"
```

## 10. 수동 Streamlit 검증

### 10.1 기본 혼합 영어 질문

```text
How is 杜甫 critiqued?
```

확인:

- `question_language=en`
- `response_language=en`
- `EntryTextsEng` index
- 영어 답변
- `Sources` header
- source text는 English-first

### 10.2 영어 응답을 요청한 한국어 질문

```text
영어로 답변해줘. 두보는 어떻게 평가되는가?
```

확인:

- 검색 query에 `영어로 답변해줘`가 없음
- `question_language=ko`, `response_language=en`
- `EntryTextsKor` 검색
- 영어 답변

### 10.3 한국어 응답을 요청한 혼합 영어 질문

```text
Answer in Korean: How is 杜甫 critiqued?
```

확인:

- `question_language=en`, `response_language=ko`
- `EntryTextsEng` 검색
- 한국어 답변, `출처`, Korean-first 인용

### 10.4 중국어 문법 + 영문 서명

```text
《Sihwa Ch'ongnim》中如何评价杜甫？
```

확인:

- `question_language=zh`
- `EntryTextsChi` 검색
- 중국어 답변

### 10.5 lock 지속과 해제

1. `Answer in English`
2. 한국어 질문 → q=`ko`, response=`en`
3. 중국어 질문 → q=`zh`, response=`en`
4. `자동 언어 감지`
5. 한국어 질문 → q/response=`ko`

graphRAG/vectorRAG를 전환해도 response lock은 유지되고, 각 질문의 vector index는 해당 `question_language`로 바뀌어야 한다.

## 11. 완료 조건

- [ ] `How is 杜甫 critiqued?`가 `en`으로 판정되고 영어로 답한다.
- [ ] 영어 문장 속 한글/한자 개체명은 문장 언어를 덮어쓰지 않는다.
- [ ] 중국어 문장 속 영문 서명은 중국어 판정을 덮어쓰지 않는다.
- [ ] `question_language`, `response_language`, `locked_language`가 분리되어 저장된다.
- [ ] `effective_language`는 `response_language` 호환 alias로 유지된다.
- [ ] 언어 제어 문구가 실제 검색 query에서 제거된다.
- [ ] vector index는 질문 언어, 답변·Sources·인용·오류·external locale은 응답 언어를 따른다.
- [ ] graphRAG와 독립 vectorRAG 모두 분리 계약을 지킨다.
- [ ] control-only 입력은 RAG/API를 호출하지 않는다.
- [ ] 언어 정책이 pure module로 분리되어 `bot.py` import 없이 테스트된다.
- [ ] 신규 테스트와 전체 기존 테스트가 통과한다.
- [ ] Claude Code 최종 보고에 변경 파일, 판별 점수/동점 정책, 테스트 명령과 결과, 남은 제한을 기록한다.

## 12. 명시적 비범위

이번 작업에서 다음은 변경하지 않는다.

- 일본어·프랑스어 등 `ko/en/zh` 이외 언어 지원 추가
- LLM/외부 API 기반 언어 감지
- 기존 Neo4j vector index 이름·embedding 재생성
- source text 번역 또는 DB 값 변경
- Poetry Talks URL, citation 이름, external authority endpoint/cap
- Streamlit 언어 선택 UI 추가 및 greeting 전체 번역
- graphRAG/vectorRAG 대화 이력 구조 변경

이 classifier는 동아시아 고전 문헌 질문에서 “문장 문법과 삽입 개체명 문자”를 구분하기 위한 결정론적 도메인 heuristic이다. 범용 자연어 식별기로 확장하지 말고, 위 fixture와 라우팅 계약을 안정적으로 만족하는 데 집중한다.

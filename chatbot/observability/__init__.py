"""Phase 1 observability — request-scoped structured telemetry.

측정 전용 계층이다. 현재 챗봇의 답변·검색·오류·폴백 동작을 바꾸지 않고,
요청 단위 지연시간·LLM 호출 목적/횟수·토큰·검색 결과·폴백 여부를 구조화
이벤트로 기록한다.

모듈 책임:
    events.py     이벤트 이름·허용 필드·enum·검증/직렬화 (단일 스키마 계약)
    sinks.py      EventSink protocol · NullSink · LoggingSink · MemorySink
    context.py    RequestContext와 ContextVar 수명주기 저장소
    emitter.py    sink 설정(환경변수/주입) · 이벤트 발행 · 내부 오류 보고
    spans.py      perf_counter span · annotate · 시도 횟수 · Neo4j operation 라벨
    telemetry.py  공개 facade — 위 두 모듈 재노출 + request_scope / 누계 API
    usage.py      provider token usage metadata 정규화
    callbacks.py  LangChain ChatModel/LLM callback 계측 + 목적(purpose) 태깅

애플리케이션 코드는 telemetry(와 purpose 태깅용 callbacks.with_llm_purpose)만
import한다.

의존성 규칙:
  * events / sinks / context / emitter / spans / telemetry / usage 는 표준
    라이브러리만 쓴다
    (Streamlit·LangChain·Neo4j·requests 미사용). 따라서 순수 계층에서 import
    해도 외부 연결이나 무거운 의존성이 생기지 않는다.
  * callbacks.py 만 langchain_core를 import한다. telemetry는 요청 시작 시점에만
    지연 로드한다.
  * 관측 실패는 절대 사용자 요청으로 전파되지 않는다.
"""

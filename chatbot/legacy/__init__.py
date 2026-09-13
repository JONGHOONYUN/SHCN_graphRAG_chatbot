"""Legacy layer — the ReAct fallback agent and its tools.

이 계층은 정상 graphRAG 파이프라인이 실패했을 때만(그리고 최상위
`generate_response()`의 예외 정책이 명시적으로 허용한 transient 오류에서만)
사용되는 구버전 구현을 담는다.

의존성 규칙:
  * 정상 파이프라인(chatbot.application.graphrag_pipeline)은 이 계층을
    절대 import하지 않는다.
  * 이 계층이 정상 경로와 공유하는 것은 prompt/schema/safety 수준까지이며
    (chatbot.retrieval.graph_prompt, tools.cypher_safety), 정상 경로의
    evidence/합성 내부 구현은 사용하지 않는다.
  * 유일한 진입점은 composition root인 agent.py의 오류 정책 분기다.

이 계층만 user-facing prose를 직접 생성하는 레거시 책임을 갖는다 — 정상
경로의 retrieval은 구조화 Evidence만 반환한다.
"""

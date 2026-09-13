"""Application layer — graphRAG evidence orchestration and routing policy.

책임: retrieval/authority intent 판별, retriever 호출 adapter(arity dispatch·
user-safe status), authority enrichment 서비스, graph→vector→authority
순서의 evidence bundle 조립.
허용 의존성: chatbot.domain, chatbot.retrieval, chatbot.authority,
chatbot.synthesis, tools.graph_intent(단일 소유 모듈). Streamlit UI 접근·
user-facing prose 작성·citation markdown 생성은 하지 않는다.
외부 부작용: 주입되거나 지연 로드된 retriever/fetcher 호출 + 경고 로그
(logger 이름은 기존 "tools.orchestrator" 유지).
기존 facade: tools/orchestrator.py (모든 심볼 re-export).
"""

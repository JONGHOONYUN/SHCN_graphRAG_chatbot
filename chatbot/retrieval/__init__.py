"""Retrieval layer — row/document → Evidence mappers and query executors.

책임: vector document·graph row의 Evidence 변환(순수), 그리고 주입받은
chain/graph 클라이언트로 graph 검색을 실행하는 서비스.
허용 의존성: chatbot.domain, chatbot.authority.registry(순수),
tools.cypher_safety / tools.graph_intent (단일 소유 모듈), neo4j 예외 타입.
클라이언트(LLM chain, Neo4j graph)는 절대 여기서 생성하지 않고 인자로
주입받는다 — 생성은 facade(tools/cypher.py 등)의 composition root 몫이다.
외부 부작용: 주입된 클라이언트 호출 + 경고 로그.
기존 facade: tools/evidence.py, tools/cypher.py.
"""

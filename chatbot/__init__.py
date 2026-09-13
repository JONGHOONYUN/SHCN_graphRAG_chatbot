"""시화총림 챗봇 — 계층화된 구현 패키지 (large-module modularization work order).

기존 루트/`tools/` 모듈은 하위 호환 facade로 유지되고, 실제 구현은 이 패키지의
계층으로 이동했다. 계층과 허용 의존성 방향:

    presentation/composition  bot.py, agent.py, text_rag.py, tools/*.py (facades)
        │  may import every layer below; owns Streamlit + module-level clients
        ▼
    chatbot.application       pipelines/orchestration (no Streamlit UI, no prose)
        │  may import: retrieval, authority, synthesis, domain
        ▼
    chatbot.retrieval         graph/vector row·document → Evidence mappers,
        │                     query-execution services (clients are INJECTED)
        │  may import: domain, authority.registry, tools.cypher_safety,
        │              tools.graph_intent
        ▼
    chatbot.authority         external authority registry / parsers / client
        │  registry+parsers are pure; only client.py imports `requests`
        ▼
    chatbot.synthesis         deterministic formatting/citation (pure)
        │  may import: domain only
        ▼
    chatbot.domain            evidence data contract (stdlib only — the
                              lowest layer; never imports streamlit /
                              langchain / neo4j / requests)

    chatbot.legacy            ReAct fallback agent — imported ONLY by the
                              agent.py composition root, never by the normal
                              graphRAG pipeline.

Infrastructure (chat LLM / embeddings / Neo4j graph construction) deliberately
stays in the root `llm.py` / `graph.py` modules: they are the process-wide
composition root, imported lazily by bot.py only after authentication, and
several tests reload them by that exact module name. New-layer services never
import them — clients are passed in by the facades.

Forbidden directions (§3.3 of the work order):
  domain → streamlit/langchain/neo4j/requests · retrieval → Streamlit UI ·
  synthesis → LLM/Neo4j clients · infrastructure → application ·
  legacy → normal pipeline internals · any chatbot.* → tools facade modules
  that re-export from chatbot (no facade↔implementation cycles).
"""

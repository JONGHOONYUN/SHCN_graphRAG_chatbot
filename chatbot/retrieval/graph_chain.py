"""GraphCypherQAChain construction with a distinct purpose per inner LLM.

책임: `GraphCypherQAChain`을 만들 때 내부 두 LLM — Cypher 생성
(`cypher_generation`)과 그래프 답변 생성(`graph_qa`) — 에 서로 다른 관측 목적
라벨을 붙인다. 공개 파라미터 `cypher_llm` / `qa_llm`만 사용하며 private 속성에
의존하지 않는다.

두 파라미터에는 composition root가 넘긴 **같은 LLM 객체**를 감싼
RunnableBinding이 들어간다(기존 `from_llm(llm, ...)`이 내부에서
`qa_llm = cypher_llm = llm`으로 두던 것과 같은 모델). 모델·temperature·retry·
timeout·prompt·`allow_dangerous_requests`는 그대로다. 구조화 검색 호출자는
`return_direct=True`로 QA를 건너뛰고 조회 행을 직접 받는다. 기본값 False는
자연어 결과가 필요한 기존 호출자와의 호환성을 유지한다.

허용 의존성: langchain_neo4j(GraphCypherQAChain), chatbot.observability.
클라이언트는 주입받는다. 기존 facade: tools/cypher.py.
"""

from __future__ import annotations

from langchain_neo4j import GraphCypherQAChain

from chatbot.observability import events as ev
from chatbot.observability.callbacks import with_llm_purpose


def build_graph_cypher_chain(llm, *, graph, cypher_prompt,
                             return_intermediate_steps: bool = False,
                             return_direct: bool = False,
                             verbose: bool = True) -> GraphCypherQAChain:
    kwargs = {
        "cypher_llm": with_llm_purpose(llm, ev.LLM_CYPHER_GENERATION),
        "qa_llm": with_llm_purpose(llm, ev.LLM_GRAPH_QA),
        "graph": graph,
        "verbose": verbose,
        "cypher_prompt": cypher_prompt,
        "allow_dangerous_requests": True,
        "return_direct": return_direct,
    }
    if return_intermediate_steps:
        kwargs["return_intermediate_steps"] = True
    return GraphCypherQAChain.from_llm(**kwargs)

"""Legacy graph QA — LLM-written prose over a generated Cypher answer.

책임: 레거시 ReAct tool "Sihwa Graph Query"가 사용하는 prose 반환 wrapper.
정상 graphRAG의 구조화 evidence 경로와는 분리되어 있으며, 정상 경로와
공유하는 것은 prompt/schema/safety까지다 (§4.5).
허용 의존성: chatbot.retrieval.graph_prompt(재시도 힌트), tools.cypher_safety,
neo4j 예외 타입. 체인은 주입받는다.
외부 부작용: 주입된 chain 호출.
기존 facade: tools/cypher.py의 `cypher_qa_safe` (동일 시그니처로 위임).

Moved verbatim from tools/cypher.py (modularization work order Phase 4.5).
"""

from __future__ import annotations

from neo4j.exceptions import CypherSyntaxError, ClientError

from chatbot.retrieval.graph_prompt import _SYNTAX_RETRY_HINT
from tools.cypher_safety import UnsafeCypherError


# ──────────────────────────────────────────────
# Safe wrapper
# LLM이 생성한 Cypher가 syntax/runtime 오류로 Neo4j에서 거부되면 전체 요청이
# 중단되어 사용자가 에러 페이지를 보게 됨. wrapper로 예외를 잡아 agent가
# 다음 iteration에서 vector search 등 다른 tool로 fallback 가능하게 함.
# ──────────────────────────────────────────────
def cypher_qa_safe(question: str, *, chain) -> str:
    """GraphCypherQAChain 호출 wrapper.

    langchain-neo4j 0.8 + langchain-classic 1.0.2 조합에서 간헐적으로 KeyError가
    발생하는 사례가 있어 (qa_chain의 output key 불일치 추정), 다음을 수행:
      1. 반환 dict에서 result/answer/text 세 키를 순차 확인 (LangChain 버전차 대응)
      2. KeyError 발생 시 어느 키가 missing이었는지 실제 메시지에 포함해
         verbose 로그에서 원인 진단 가능하게 함
      3. 한 번 자동 재시도 (transient Gemini 이슈에 대한 회복)

    `chain`은 prose를 생성하는 레거시 GraphCypherQAChain으로, composition
    root(tools/cypher.py)가 주입한다.
    """
    last_error_msg = None
    query = question
    for attempt in range(2):  # 첫 시도 + 1회 재시도
        try:
            result = chain.invoke({"query": query})
            if isinstance(result, dict):
                # LangChain 버전별 output key 불일치 대응
                answer = (
                    result.get("result")
                    or result.get("answer")
                    or result.get("text")
                )
                if answer:
                    return answer
                return "No graph results found."
            return str(result)
        except CypherSyntaxError:
            if attempt == 0:
                # 아포스트로피 이스케이프 힌트를 덧붙여 Cypher 재생성 시도
                query = question + _SYNTAX_RETRY_HINT
                last_error_msg = "CypherSyntaxError (retried with escaping hint)"
                continue
            return (
                "Graph query failed (Cypher syntax error). "
                "Try rephrasing the question, or fall back to Sihwa Content Search."
            )
        except UnsafeCypherError as e:
            # LLM-generated Cypher tried to write / call an un-allowlisted
            # procedure / etc. Never echo the query text back — reason only.
            return (
                f"Graph query blocked by safety validator [{e.correlation_id}]. "
                "Please rephrase the question."
            )
        except ClientError as e:
            return (
                f"Graph query failed ({type(e).__name__}: {e.code}). "
                "Try rephrasing, or fall back to Sihwa Content Search."
            )
        except KeyError as e:
            # 재시도 대상. 진단 정보를 축적하여 마지막 attempt에서 노출.
            missing = e.args[0] if e.args else "unknown"
            last_error_msg = (
                f"KeyError('{missing}') from GraphCypherQAChain internals. "
                "This is likely a langchain-neo4j / langchain-classic version integration issue "
                "(qa_chain output key mismatch or intermediate step parsing)."
            )
            continue  # 재시도
        except Exception as e:
            last_error_msg = f"{type(e).__name__}: {str(e)[:120]}"
            continue  # 재시도

    return (
        f"Graph query failed after retry — {last_error_msg}. "
        "Try rephrasing, or fall back to Sihwa Content Search."
    )

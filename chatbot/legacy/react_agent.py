"""Legacy ReAct agent — tool wiring, executor, and fallback invocation.

책임: ReAct tool 목록 구성, AgentExecutor/RunnableWithMessageHistory 구성,
iteration-limit 폴백 처리, General Chat tool의 동적 언어 prompt 실행.

의존성 규칙(§3.3/§8.3): 정상 graphRAG 파이프라인
(chatbot.application.graphrag_pipeline)은 이 모듈을 절대 import하지 않는다.
호출은 오직 composition root(agent.py)의 최상위 오류 정책에서만 일어난다.
정상 경로와 공유하는 것은 언어 지시문(prompt) 수준까지다.

허용 의존성: langchain(agent/executor/prompt), chatbot.legacy.react_prompt,
chatbot.synthesis.prompt(_build_language_directive). llm·tool 콜러블·이력
팩토리·session_id는 모두 주입받으며 Streamlit session state는 읽지 않는다.
외부 부작용: 주입된 LLM/tool 호출, RunnableWithMessageHistory의 이력 읽기/쓰기
(이 경로의 이력 저장 소유자).
기존 facade: agent.py (`tools`, `agent_executor`, `chat_agent`,
`_generate_response_react`, `general_chat`).

Moved verbatim from agent.py (modularization work order Phase 8.1/8.3).
"""

from __future__ import annotations

from langchain_classic.agents import create_react_agent, AgentExecutor
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.tools import Tool

from chatbot.legacy.react_prompt import (
    AGENT_PROMPT_TEMPLATE,
    CHAT_SYSTEM,
    GENERAL_CHAT_SYSTEM,
    ITERATION_LIMIT_FALLBACK,
    ITERATION_LIMIT_PLACEHOLDER,
    PARSE_ERROR_INSTRUCTION,
    TOOL_DESCRIPTION_AUTHORITY_LOOKUP,
    TOOL_DESCRIPTION_COMBINED_SEARCH,
    TOOL_DESCRIPTION_CONTENT_SEARCH,
    TOOL_DESCRIPTION_GENERAL_CHAT,
    TOOL_DESCRIPTION_GRAPH_QUERY,
)
from chatbot.synthesis.prompt import LANGUAGE_LABEL, _build_language_directive


def build_chat_prompt() -> ChatPromptTemplate:
    """정적 진입용 chat prompt (하위 호환 `poetry_chat` 체인용)."""
    return ChatPromptTemplate.from_messages(
        [("system", CHAT_SYSTEM), ("human", "{input}")]
    )


def run_general_chat(input_text: str, *, llm, user_language: str) -> str:
    """General Chat tool 본체.

    agent_prompt의 language_directive를 모르는 별도 LLM 호출이므로, 호출 시점의
    응답 언어를 강한 지시로 prepend한 동적 prompt로 chain을 다시 만들어 실행한다.
    이렇게 해야 agent가 입력을 다른 언어로 번역해 넣었더라도 tool 출력은 사용자
    언어로 강제된다. 언어 값은 호출자(composition root)가 세션에서 읽어 넘긴다."""
    label = LANGUAGE_LABEL.get(user_language, LANGUAGE_LABEL["ko"])
    dyn_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", GENERAL_CHAT_SYSTEM.format(label=label)),
            ("human", "{input}"),
        ]
    )
    chain = dyn_prompt | llm | StrOutputParser()
    return chain.invoke({"input": input_text})


def build_tools(*, general_chat, get_poetry_plot, cypher_qa_safe,
                external_authority_lookup) -> list:
    """ReAct tool 목록. 각 tool의 실제 구현은 주입받고, 라우팅 설명 텍스트만
    이 레거시 계층(chatbot/legacy/react_prompt.py)이 소유한다."""
    return [
        Tool.from_function(
            name="General Chat",
            description=TOOL_DESCRIPTION_GENERAL_CHAT,
            func=general_chat,
        ),
        Tool.from_function(
            name="Sihwa Content Search",
            description=TOOL_DESCRIPTION_CONTENT_SEARCH,
            func=get_poetry_plot,
        ),
        Tool.from_function(
            name="Sihwa Graph Query",
            description=TOOL_DESCRIPTION_GRAPH_QUERY,
            func=cypher_qa_safe,
        ),
        Tool.from_function(
            name="Combined Sihwa Search",
            description=TOOL_DESCRIPTION_COMBINED_SEARCH,
            func=lambda q: f"{cypher_qa_safe(q)}\n\n[보완 정보]\n{get_poetry_plot(q)}",
        ),
        Tool.from_function(
            name="External Authority Lookup",
            description=TOOL_DESCRIPTION_AUTHORITY_LOOKUP,
            func=external_authority_lookup,
        ),
    ]


def _parse_error_handler(error) -> str:
    """ReAct format 위반 시 구체적인 자기수정 안내를 Observation으로 반환.
    Gemini가 Thought/Action/Action Input을 한 줄에 합치거나 Action Input을
    누락하는 사례가 잦아 다음 iteration에서 실수를 바로잡도록 명시한다."""
    return PARSE_ERROR_INSTRUCTION


def build_agent_prompt() -> PromptTemplate:
    """레거시 ReAct prompt 객체. 템플릿 텍스트는 react_prompt.py 소유."""
    return PromptTemplate.from_template(AGENT_PROMPT_TEMPLATE)


def build_agent(*, llm, tools):
    """ReAct agent 구성 (executor와 분리 — 기존 모듈 전역 `agent`와 동일 객체)."""
    return create_react_agent(llm, tools, build_agent_prompt())


def build_agent_executor(*, agent, tools) -> AgentExecutor:
    """ReAct executor 구성. iteration 수와 파싱 오류 정책은 불변."""
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        # callable로 지정하여 매 parsing 실패마다 구체적인 자기수정 instruction을
        # Observation으로 전달. 단순 문자열보다 Gemini의 회복 성공률이 높음.
        handle_parsing_errors=_parse_error_handler,
        max_iterations=15,
        # early_stopping_method는 기본값("force") 사용.
        # create_react_agent가 만드는 RunnableAgent는 "generate"를 지원하지 않아
        # ValueError를 raise하므로 명시하지 않음. placeholder 출력은
        # generate_response_react에서 후처리.
    )


def build_chat_agent(agent_executor, memory_factory) -> RunnableWithMessageHistory:
    """이력 래퍼. 이 경로의 대화 이력 저장은 RunnableWithMessageHistory가
    단독 소유한다 — 정상 경로와의 이중 저장을 막기 위해 폴백 경로에서는
    파이프라인이 별도로 저장하지 않는다."""
    return RunnableWithMessageHistory(
        agent_executor,
        memory_factory,
        input_messages_key="input",
        history_messages_key="chat_history",
    )


def generate_response_react(user_input, response_language, *, chat_agent,
                            session_id: str):
    """레거시 ReAct agent 경로 (폴백). 파이프라인 실패 시에만 사용.

    `user_input`은 이미 question_text(제어 문구 제거본)이며 그대로 tool
    Action Input에 사용된다 — 번역하지 않는다. `response_language`는 최종
    'Final Answer:' directive에만 적용된다 (work order §5.2 라우팅 계약).
    `session_id`는 이미 `::graphRAG` namespace가 붙은 값이다 (호출자 책임)."""
    language_directive = _build_language_directive(response_language)

    try:
        response = chat_agent.invoke(
            {"input": user_input, "language_directive": language_directive},
            {"configurable": {"session_id": session_id}},)
    except ValueError as e:
        # Gemini가 빈 스트림을 반환한 경우 ("No generation chunks were returned").
        if "No generation chunks were returned" in str(e):
            return ITERATION_LIMIT_FALLBACK.get(response_language, ITERATION_LIMIT_FALLBACK["ko"])
        raise

    output = response['output']
    # iteration limit placeholder를 세션 언어 친화 안내로 교체
    if ITERATION_LIMIT_PLACEHOLDER in output:
        return ITERATION_LIMIT_FALLBACK.get(response_language, ITERATION_LIMIT_FALLBACK["ko"])
    return output

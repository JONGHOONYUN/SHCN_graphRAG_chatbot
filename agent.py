from llm import llm
from graph import graph
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.tools import Tool
from langchain_neo4j import Neo4jChatMessageHistory
from langchain_classic.agents import create_react_agent, AgentExecutor
from langchain_core.runnables.history import RunnableWithMessageHistory
from utils import get_session_id

from tools.vector import get_poetry_plot
from tools.cypher import cypher_qa

chat_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "당신은 한국 시화총림(詩話叢林) 전문가입니다. "
           "조선시대 시화집(지봉유설, 성수시화, 호곡시화 등)의 인물·시·비평·"
           "주제·장소·시대 정보를 그래프 DB에서 조회하여 답변합니다. "
           "한국어, 영어, 한문(漢文)으로 응답할 수 있습니다."),
        ("human", "{input}"),
    ]
)

poetry_chat = chat_prompt | llm | StrOutputParser()

# 1. tools 정의
tools = [
    Tool.from_function(
        name="General Chat",
        description="For general poetry chat not covered by other tools",
        func=poetry_chat.invoke,
    ), 
    Tool.from_function(
        name="Sihwa Content Search",  
        description="시화집(Book)의 내용·주제·수록 항목을 의미 기반으로 검색할 때 사용. "
                "예: '달을 노래한 시화는?', '은일 정서가 강한 시화집은?', "
                "'성수시화의 주요 내용은?'. 인물의 생몰년이나 정확한 관계 조회에는 "
                "사용하지 말 것.",
        func=get_poetry_plot, 
    ),
    Tool.from_function(
        name="Sihwa Graph Query",
        description="DB에서 정확한 사실을 조회할 때 최우선으로 사용. "
                "처리 가능한 질의: (1) 인물 속성 - 생몰년, 본관, 관직, 신분 / "
                "(2) 작품 관계 - 누가 어떤 시를 지었는가, 누가 누구를 평했는가 / "
                "(3) 수록 관계 - 어느 시화집에 어떤 항목이 실렸는가 / "
                "(4) 주제·장소·시대별 작품 검색 / "
                "(5) 비평용어가 쓰인 비평문 검색. "
                "예: '이수광의 생몰년은?', '허균이 평한 시는?', "
                "'호곡시화에 실린 칠언절구는?', '한강이 등장하는 시는?'",
        func = cypher_qa
    ),
    Tool.from_function(
    name="Combined Sihwa Search",
    description=(
        "벡터 검색과 그래프 쿼리를 모두 사용해야 하는 복합 질문에 사용. "
        "예: '주제 + 인물 + 시화집' 세 조건이 모두 명시된 경우."
    ),
    func=lambda q: f"{cypher_qa(q)}\n\n[보완 정보]\n{get_poetry_plot(q)}"
)
]

def get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)

# 2. agent_prompt 정의
agent_prompt = PromptTemplate.from_template("""
You are an expert in East Asian humanities providing information on Korean Sihwa literature.

# Decision Tree (도구 선택 절차)
질문을 받으면 다음 순서로 판단하세요:
1단계: 질문이 인사말/잡담인가? → General Chat
2단계: 질문에 고유명사 + 속성/관계가 명확히 있는가? → Sihwa Graph Query
3단계: 질문이 추상적·주제적·내용 검색인가? → Sihwa Content Search
4단계: Graph Query 결과가 비어있다면 → Sihwa Content Search로 재시도

CRITICAL FORMAT RULES:
- 모든 응답은 반드시 "Thought:"로 시작합니다.
- 도구를 사용할 때는 "Action:", "Action Input:" 형식을 정확히 따릅니다.
- 최종 답변을 줄 때는 반드시 "Final Answer:" 라벨로 시작해야 합니다.
- "Final Answer:" 없이 바로 답변 내용을 쓰면 안 됩니다.

# Multi-step Reasoning
복잡한 질문은 한 번에 답하지 말고 여러 단계로 나누세요:
예) "지봉유설에 실린 달 주제 시의 작자는 어느 시대 사람인가?"
1) Sihwa Content Search → '달' 주제 시 검색
2) 발견된 작자명 추출
3) Sihwa Graph Query → 그 작자의 yearBirth, HAS_ERA 조회
4) 결과 종합

Do not use pre-learned knowledge to answer questions. Use only the information provided in the context. If the question cannot be answered with the given context, respond with "제공된 자료에 없습니다".


TOOLS:
------

You have access to the following tools:

{tools}

To use a tool, please use the following format:

```
Thought: Do I need to use a tool? Yes
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
```

When you have a response to say to the Human, or if you do not need to use a tool, you MUST use the format:

```
Thought: Do I need to use a tool? No
Final Answer: [your response here]
```

Begin!

Previous conversation history:
{chat_history}

New input: {input}
{agent_scratchpad}
""")

# 3. agent 생성
agent = create_react_agent(llm, tools, agent_prompt)

# 4. agent_executor 생성
agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    handle_parsing_errors="형식 오류가 발생했습니다. 'Final Answer:'로 시작하는 답변 형식을 사용해주세요.",
    max_iterations=5,
)

chat_agent = RunnableWithMessageHistory(
    agent_executor,
    get_memory,
    input_messages_key="input",
    history_messages_key="chat_history",
)

def generate_response(user_input):
    """
    Create a handler that calls the Conversational agent
    and returns a response to be rendered in the UI
    """

    response = chat_agent.invoke(
        {"input": user_input},
        {"configurable": {"session_id": get_session_id()}},)

    return response['output']
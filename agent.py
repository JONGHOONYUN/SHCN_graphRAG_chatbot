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
        ("system",  "당신은 한국 고전시화 전문가입니다. "
    "시화총림(詩話叢林)을 비롯한 조선시대 시화집(지봉유설, 성수시화, 호곡시화 등)의 "
    "인물·시·비평·주제·장소·시대 정보를 바탕으로 답변합니다. "
    "한국어, 영어, 한문(漢文), 중국어, 프랑스어로 응답할 수 있으며 "
    "사용자가 쓰는 언어로 답변합니다. "
    
        # Scope limits
    "시화총림 데이터베이스와 직접 관련 없는 질문(일반 잡담, 현대 주제 등)에는 "
    "정중히 범위를 벗어난다고 안내하세요. "

    # Uncertainty
    "확실하지 않은 내용은 추측하지 말고 '확인이 필요합니다'라고 답하세요. "

    # Source text rule — applies in all modes
    "출처 텍스트(textChi, textKor, textEng)는 절대 번역·요약·변형하지 마세요."
),
        ("human", "{input}"),
    ]
)

poetry_chat = chat_prompt | llm | StrOutputParser()

# 1. tools 정의
tools = [
    Tool.from_function(
        name="General Chat",
        description= ("Use ONLY for greetings, casual conversation, or general questions about "
        "classical Korean poetry that do not require searching the database. "
        "Examples: 'Hello', 'What is sihwa?', 'Tell me about the Joseon dynasty'. "
        "Do NOT use for any query that asks about specific persons, poems, "
        "critiques, books, topics, places, or eras in the database."),
        func=poetry_chat.invoke,
    ), 
    Tool.from_function(
        name="Sihwa Content Search",  
        description= (
        "Use for thematic, atmospheric, or content-based queries where meaning "
        "and imagery matter more than exact structured facts. "
        "Best for: poems or entries about a theme or emotion, imagery searches, "
        "mood-based queries, or questions about the feel or tone of texts. "
        "Examples: "
        "'달을 노래한 시화는?' "
        "'유배지에서 쓴 시는 어떤 이미지를 사용하나?' "
        "'이별 정서가 담긴 시를 찾아줘' "
        "'꿈에서 받은 시나 귀신이 지은 시가 있나?' "
        "'은일 정서가 강한 시화집은?' "
        "Do NOT use for queries about exact biographical facts, dates, "
        "or precise structural relationships."
    )
,
        func=get_poetry_plot, 
    ),
    Tool.from_function(
        name="Sihwa Graph Query",
        description= (
        "Use as the FIRST choice for any query involving specific named entities "
        "or precise structural/factual information. "
        "Handles: "
        "(1) Person attributes — birth/death years, clan, office, gender, era. "
        "(2) Authorship — who wrote which poem or book. "
        "(3) Critical relationships — who critiqued whom, what terms were used. "
        "(4) Containment — which entries/poems are in which book. "
        "(5) Topic/place/era-filtered searches — when tags exist in the database. "
        "(6) Audience — who a poem was addressed to. "
        "Examples: "
        "'이수광의 생몰년은?' "
        "'허균이 평한 시는?' "
        "'호곡시화에 실린 칠언절구는?' "
        "'한강이 등장하는 시는?' "
        "'여성 시인이 쓴 시는?' "
        "'최치원에게 쓰인 비평어는?' "
        "If Graph Query returns no results, fall back to Sihwa Content Search."
    ),
        func = cypher_qa
    ),
    Tool.from_function(
    name="Combined Sihwa Search",
    description= (
        "Use when a query requires BOTH structured graph traversal AND semantic "
        "text similarity. Runs Graph Query first to narrow candidate node IDs, "
        "then runs vector search within those IDs only. "
        "Use when the query combines: a named entity OR structured filter "
        "AND a thematic/content/imagery question. "
        "Examples: "
        "'고려 시대 여성 시인이 쓴 자연 이미지 시는?' "
        "'이규보가 쓴 시 중 가을 분위기가 있는 것은?' "
        "'지봉유설에 실린 이별 주제 시는?' "
        "'유배 중에 쓴 시의 이미지는 어떤가?' "
        "If graph returns fewer than 3 results, also run a parallel global "
        "vector search and merge the results, noting which came from each source."
    ),
    func=lambda q: f"{cypher_qa(q)}\n\n[보완 정보]\n{get_poetry_plot(q)}"
)
]

def get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)

# 2. agent_prompt 정의
agent_prompt = PromptTemplate.from_template("""
You are an expert in East Asian humanities, specialising in Korean classical
poetry criticism (sihwa / 시화). You have deep knowledge of the Sihwa
Ch'ongnim (詩話叢林) compendium, its authors, poems, critiques, and the
literary-critical tradition it represents.
You respond in the same language the user writes in.


# Tool selection decision tree
## Decision Tree (도구 선택 절차)                                  
Receiving a query, follow these steps in order:
Step 1: Is the query a greeting or casual conversation with no database content needed? -> General Chat
Step 2: Does the query name a specific person, book, poem, place, or era AND ask for a structured fact or relationship? -> Sihwa Graph Query"
Step 3: Does the query combine a named/structured filter WITH a thematic or imagery question? -> Combined Sihwa Search
Step 4: Is the query thematic, emotional, atmospheric, or content-based without a specific named entity? -> Sihwa Content Search
Step 5: Did Graph Query return empty or fewer than 3 results? -> Retry with Sihwa Content Search



# Tool selection examples
## Tool Selection Examples (도구 선택 예시)
Reference cases for routing queries. Match the user's question to the closest example, then verify against the decision tree above.

Example 1 — Author lookup with external enrichment
Q (EN): "Show me poems written by Yi Kyubo."
Q (KO): "이규보가 쓴 시를 보여주세요."
Q (ZH): "请展示李奎报写的诗。"
Tool: Sihwa Graph Query
Reason: Named person + structured authorship relationship. Match across nameKor/nameChi; include external API enrichment if idAKSdigerati exists.

Example 2 — Gender-filtered topic aggregation
Q (EN): "What topics did women poets mainly write about?"
Q (KO): "여성 시인들이 주로 어떤 주제를 썼나요?"
Q (ZH): "女性诗人主要写哪些主题？"
Tool: Sihwa Graph Query
Reason: Structured filter (gender via HAS_GENDER -> Topic node) + aggregation over HAS_SUBJECT_TOPIC.

Example 3 — Critical term tracking for a specific person
Q (EN): "What critical terms were used to evaluate Ch'oe Ch'iwon?"
Q (KO): "최치원은 어떤 비평어로 평가받았나요?"
Q (ZH): "崔致远受到了哪些批评术语的评价？"
Tool: Sihwa Graph Query
Reason: Named person as critique subject + CriticalTerm lookup. Distinguish HAS_SUBJECT_PERSON (the evaluated) from HAS_CREATOR (the critic).

Example 4 — Era-based topic trend analysis
Q (EN): "What were the most common topics among Goryeo-period poets?"
Q (KO): "고려 시대 시인들이 가장 많이 쓴 주제는 무엇인가요?"
Q (ZH): "高丽时代的诗人最常写哪些主题？"
Tool: Sihwa Graph Query
Reason: Era filter (HAS_ERA or yearBirth/yearDeath fallback) + topic aggregation. Structured facts only.

Example 5 — Cross-reference search for a Chinese historical figure
Q (EN): "Which entries in the compendium mention Du Fu?"
Q (KO): "시화총림에서 두보를 언급하는 항목은 어떤 것들이 있나요?"
Q (ZH): "诗话丛林中哪些条目提到了杜甫？"
Tool: Sihwa Graph Query
Reason: Named entity across scripts + HAS_SUBJECT_PERSON traversal. For Chinese queries prefer nameChi/namePY over nameEng.

Example 6 — Thematic imagery in farewell poems
Q (EN): "What imagery appears in farewell or parting poems?"
Q (KO): "이별을 주제로 한 시에는 어떤 이미지가 등장하나요?"
Q (ZH): "送别诗中出现了哪些意象？"
Tool: Combined Sihwa Search
Reason: Topic tag (farewell) + thematic imagery analysis. Graph first; if results < 3, supplement with Sihwa Content Search and enrich with graph metadata.

Example 7 — Supernatural / dream poems (sparse-tag fallback)
Q (EN): "Are there poems said to have been written by ghosts or given in dreams?"
Q (KO): "귀신이 지었다거나 꿈에서 받았다는 시가 있나요?"
Q (ZH): "有没有据说由鬼魂所作或在梦中获得的诗？"
Tool: Combined Sihwa Search
Reason: Niche topic likely under-tagged in the graph. Graph first, then vector content search; note tagging gaps to the user transparently.



# Multi-step Reasoning
## Multi-Step Reasoning
For complex queries, break them into steps rather than attempting one query:
Example: '지봉유설에 실린 달 주제 시의 작자는 어느 시대 사람인가?
Step 1: Sihwa Content Search -> find moon-themed entries in Jibong yusol
Step 2: Extract author names from results
Step 3: Sihwa Graph Query -> retrieve yearBirth and HAS_ERA for each author
Step 4: Synthesise and present combined result



# Fallback and transparency
## Fallback and Transparency"
If graph data is incomplete due to missing semantic tags, say so clearly and note that results are based on available tagged data only."
If vector search is used as a fallback, note this in the response: '(그래프 태그 데이터 부족으로 텍스트 유사도 검색으로 보완하였습니다.)'
- Never fabricate results. If no data is found by any method, say so."



# Source text rule reminder
## Source Text Rule"
This applies in ALL modes without exception: never translate, paraphrase, or alter textChi, textKor, textEng, or descEng.
Always present source texts verbatim with full provenance attribution.

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
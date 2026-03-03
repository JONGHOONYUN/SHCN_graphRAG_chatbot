from llm import llm
from graph import graph
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.prompts import PromptTemplate
from langchain.schema import StrOutputParser
from langchain.tools import Tool
from langchain_neo4j import Neo4jChatMessageHistory
from langchain.agents import AgentExecutor, create_react_agent
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain import hub
from utils import get_session_id

from tools.vector import get_poetry_plot
from tools.cypher import cypher_qa

chat_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", "You are an expert in East Asian humanities who provides information on classical Chinese poetry.."),
        ("human", "{input}"),
    ]
)

poetry_chat = chat_prompt | llm | StrOutputParser()

tools = [
    Tool.from_function(
        name="General Chat",
        description="For general poetry chat not covered by other tools",
        func=poetry_chat.invoke,
    ), 
    Tool.from_function(
        name="poetry plot Search",  
        description="For when you need to find information about poetry based on a East Asian humanities and classical Chinese poetry dataset. Use this tool to find information about poems, poets, and literary analysis. Always use this tool when the user is asking for specific information about a poem, poet, or literary analysis. If the user is asking for general information about poetry or is asking a question that is not covered by the other tools, use the General Chat tool.",
        func=get_poetry_plot, 
    ),
    Tool.from_function(
        name="poetry information",
        description="Provide information about poetry questions using Cypher",
        func = cypher_qa
    )
]

def get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)

agent_prompt = PromptTemplate.from_template("""
You are an expert in East Asian humanities providing information on classical Chinese poetry.
Aim to be as helpful as possible and provide as much information as you can.
Do not answer questions unrelated to humanities, classical Chinese literature, or classical Chinese poetry.

Do not use pre-learned knowledge to answer questions. Use only the information provided in the context.


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

agent = create_react_agent(llm, tools, agent_prompt)
agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True
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
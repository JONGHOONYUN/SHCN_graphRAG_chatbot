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
        ("system", "You are an expert in East Asian humanities who provides information on classical Chinese poetry. You should understand and respond in Korean(한국어), English, and Classical Chinvese(漢文)."),
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
        name="Bookplot Search",  
        description="Use this tool ONLY when searching for Book titles or Book content by meaning or theme. Do NOT use for person attributes like birthyear. For example, if the user asks 'What is the plot of Journey to the West?' or 'Can you summarize the story of Water Margin?', use this tool to provide a detailed summary of the book's plot and themes. Always prioritize this tool for any questions related to book content or thematic searches to ensure comprehensive and relevant information retrieval.",
        func=get_poetry_plot, 
    ),
    Tool.from_function(
        name="poetry information",
        description="Use this tool FIRST for specific queries about person attributes(birthyear, deathyear, name), relationships between nodes, or any structured data lookup in the database. For example, if the user asks 'What is the birth year of Li Bai?' or 'Who are the poets that influenced Du Fu?', use this tool to query the database and provide a precise answer. Always prioritize this tool for any questions that can be answered with a specific Cypher query to ensure accurate and relevant information retrieval.",
        func = cypher_qa
    )
]

def get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)

agent_prompt = PromptTemplate.from_template("""
You are an expert in East Asian humanities providing information on classical Chinese poetry.
Aim to be as helpful as possible and provide as much information as you can.


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
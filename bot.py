import streamlit as st
from utils import write_message
from agent import generate_response

# Page Config
st.set_page_config("PoetryTalks", page_icon=":speech_balloon:")

# Set up Session State
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "안녕하세요! PoetryTalks 챗봇입니다. 시인, 시 제목, 내용으로 질문해보세요. (예: 이백이 지은 시를 알려줘 / 李白의 작품은?)"},
    ]

# Submit handler
def handle_submit(message):
    """
    Submit handler:

    You will modify this method to talk with an LLM and provide
    context using data from Neo4j.
    """

    # Handle the response
    with st.spinner('Thinking...'):
        # Call the agent
        response = generate_response(message)
        write_message('assistant', response)
        


# Display messages in Session State
for message in st.session_state.messages:
    write_message(message['role'], message['content'], save=False)

# Handle any user input
if prompt := st.chat_input("영어, 한국어, 한문으로 질문해보세요"):
    # Display user message in chat message container
    write_message('user', prompt)

    # Generate a response
    handle_submit(prompt)
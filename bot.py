import streamlit as st
from utils import write_message
from agent import generate_response

# Page Config
st.set_page_config("PoetryTalks", page_icon=":speech_balloon:")

# Set up Session State
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": (
    "안녕하세요! **시화총림(詩話叢林) DB 챗봇**입니다.\n\n"
    "조선시대 시화집 13종, 인물 1,225명, 시 1,829편, 비평 1,759개를 "
    "그래프로 연결한 데이터를 검색합니다.\n\n"
    "**질문 예시**\n"
    "- 이수광의 생몰년과 관직은?\n"
    "- 허균이 평한 시 목록을 알려줘\n"
    "- 지봉유설에 실린 '달'을 주제로 한 시는?\n"
    "- 칠언절구를 가장 많이 지은 시인은?\n"
    "- '기고(奇古)' 비평용어가 쓰인 비평문은?"
)},
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
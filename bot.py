import re
import streamlit as st
from utils import write_message
from agent import generate_response


# ──────────────────────────────────────────────
# 응답 언어 락 (Locked-language)
# 사용자의 첫 질문 언어를 감지해 세션 동안 답변 언어를 고정.
# 한글 → ko, 한자만 → zh, 라틴 문자 → en, 기타는 ko로 폴백.
# (한글에는 한자가 섞일 수 있으므로 한글 판정을 한자보다 먼저 수행)
# ──────────────────────────────────────────────
def detect_language(text: str) -> str:
    if re.search(r"[가-힣]", text):
        return "ko"
    if re.search(r"[一-鿿]", text):
        return "zh"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "ko"

# Page Config
st.set_page_config("PoetryTalks", page_icon=":speech_balloon:")

# ──────────────────────────────────────────────
# 접근 인증 (테스터 공유 비밀번호)
# secrets.toml 의 APP_PASSWORD 값과 일치해야 통과.
# 인증 실패 시 st.stop()으로 이하 챗봇 로직 실행을 차단하여
# 미인증 사용자가 LLM/DB 호출을 트리거하지 못하게 함.
# ──────────────────────────────────────────────
def check_password() -> bool:
    """비밀번호 일치 시 True, 아니면 입력창을 표시하고 False."""
    def _on_submit():
        if st.session_state.get("password") == st.secrets["APP_PASSWORD"]:
            st.session_state["auth_ok"] = True
            del st.session_state["password"]      # 입력값을 세션에서 즉시 제거
        else:
            st.session_state["auth_ok"] = False

    if st.session_state.get("auth_ok"):
        return True

    st.markdown("## 🔒 시화총림 챗봇 — 접근 인증")
    st.caption("테스터 권한 비밀번호를 입력해주세요.")
    st.text_input("비밀번호", type="password", on_change=_on_submit, key="password")

    if st.session_state.get("auth_ok") is False:
        st.error("비밀번호가 올바르지 않습니다.")
    return False


if not check_password():
    st.stop()

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
if prompt := st.chat_input("한국어, 영어, 중국어(한문)로 질문해보세요"):
    # 첫 질문에서만 답변 언어 결정 → 세션 내내 유지
    if "user_language" not in st.session_state:
        st.session_state["user_language"] = detect_language(prompt)

    # Display user message in chat message container
    write_message('user', prompt)

    # Generate a response
    handle_submit(prompt)
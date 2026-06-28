import re
import streamlit as st
from utils import write_message
from agent import generate_response


# ──────────────────────────────────────────────
# 응답 언어 정책
# 기본: 매 질문마다 그 질문의 언어로 자동 응답 (질문 언어 = 응답 언어).
# 예외: 사용자가 명시적으로 특정 언어로 답하라고 요청하면 그 언어로 락하고
#       이후 모든 답변은 질문 언어와 무관하게 락 언어로 생성.
#       락은 사용자가 다시 다른 언어로 락하거나 명시적으로 해제할 때까지 유지.
# ──────────────────────────────────────────────
def detect_language(text: str) -> str:
    """질문 문자열의 언어 자동 감지. ko / en / zh, 기본 ko."""
    if re.search(r"[가-힣]", text):
        return "ko"
    if re.search(r"[一-鿿]", text):
        return "zh"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "ko"


# 명시적 언어 락 요청 패턴. 사용자가 "X 언어로 답해줘"라고 명령한 경우만 매치.
# 일반 문장에 언어 이름이 우연히 들어간 경우(예: "I love English literature")는
# 매치되지 않도록 동사·전치사와 결합된 형태만 인식.
EXPLICIT_LOCK_PATTERNS = [
    # English
    (re.compile(r"\b(?:answer|respond|reply|talk|speak|write|chat)\s+(?:to me\s+|with me\s+)?(?:in\s+)?english\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(?:answer|respond|reply|talk|speak|write|chat)\s+(?:to me\s+|with me\s+)?(?:in\s+)?korean\b", re.IGNORECASE), "ko"),
    (re.compile(r"\b(?:answer|respond|reply|talk|speak|write|chat)\s+(?:to me\s+|with me\s+)?(?:in\s+)?chinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"\b(?:please\s+)?use\s+english\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(?:please\s+)?use\s+korean\b", re.IGNORECASE), "ko"),
    (re.compile(r"\b(?:please\s+)?use\s+chinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"\b(?:switch|change)\s+to\s+english\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(?:switch|change)\s+to\s+korean\b", re.IGNORECASE), "ko"),
    (re.compile(r"\b(?:switch|change)\s+to\s+chinese\b", re.IGNORECASE), "zh"),
    (re.compile(r"\bin\s+english\s+(?:please|from now on)\b", re.IGNORECASE), "en"),
    (re.compile(r"\bin\s+korean\s+(?:please|from now on)\b", re.IGNORECASE), "ko"),
    (re.compile(r"\bin\s+chinese\s+(?:please|from now on)\b", re.IGNORECASE), "zh"),
    # Korean
    (re.compile(r"한국어로\s*(?:대답|답변|응답|답|말)"), "ko"),
    (re.compile(r"영어로\s*(?:대답|답변|응답|답|말)"), "en"),
    (re.compile(r"중국어로\s*(?:대답|답변|응답|답|말)"), "zh"),
    (re.compile(r"(?:앞으로|이제부터|계속)\s*한국어로"), "ko"),
    (re.compile(r"(?:앞으로|이제부터|계속)\s*영어로"), "en"),
    (re.compile(r"(?:앞으로|이제부터|계속)\s*중국어로"), "zh"),
    # Chinese
    (re.compile(r"用中文\s*(?:回答|回复|说|回應|對話)"), "zh"),
    (re.compile(r"用英(?:语|文)\s*(?:回答|回复|说|回應|對話)"), "en"),
    (re.compile(r"用韩(?:语|文)\s*(?:回答|回复|说|回應|對話)"), "ko"),
    (re.compile(r"请用中文"), "zh"),
    (re.compile(r"请用英(?:语|文)"), "en"),
    (re.compile(r"请用韩(?:语|文)"), "ko"),
]

RELEASE_LOCK_PATTERNS = [
    re.compile(r"\b(?:remove|cancel|stop|clear|reset|disable)\s+(?:the\s+)?(?:language\s+)?lock\b", re.IGNORECASE),
    re.compile(r"\bfollow\s+(?:my|the)\s+question\s+language\b", re.IGNORECASE),
    re.compile(r"\bauto[-\s]?detect\s+language\b", re.IGNORECASE),
    re.compile(r"언어\s*락\s*(?:해제|취소|초기화|리셋)"),
    re.compile(r"자동\s*(?:언어\s*감지|감지|판별)"),
    re.compile(r"(?:跟着|跟随|根据)我的语言"),
]


def detect_explicit_lock(text: str):
    """명시적 락 요청 시 'ko'|'en'|'zh' 반환, 없으면 None."""
    for pattern, lang_code in EXPLICIT_LOCK_PATTERNS:
        if pattern.search(text):
            return lang_code
    return None


def detect_release_request(text: str) -> bool:
    return any(p.search(text) for p in RELEASE_LOCK_PATTERNS)

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
    "시화총림(詩話叢林)에 담긴 지식·정보를 그래프 데이터로 구조화하여 연결한 데이터를 검색합니다.\n\n"
    "인물, 비평, 장소, 시 등 9개의 클래스(Class)로 분류된 8,232개의 노드 데이터와 43,123개의 관계 데이터를 탐색할 수 있습니다.\n\n"
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
    # 1) 명시적 락/해제 요청 처리. 락 요청이 발견되면 그 언어로 락 갱신,
    #    해제 요청이면 락 제거. 두 형태가 모두 없으면 락 상태는 그대로 유지.
    explicit_lock = detect_explicit_lock(prompt)
    if explicit_lock:
        st.session_state["locked_language"] = explicit_lock
    elif detect_release_request(prompt):
        st.session_state.pop("locked_language", None)

    # 2) 이번 턴 적용 언어 결정.
    #    locked_language 가 있으면 그것을 사용, 없으면 현재 질문 언어 자동 감지.
    st.session_state["effective_language"] = (
        st.session_state.get("locked_language") or detect_language(prompt)
    )

    # Display user message in chat message container
    write_message('user', prompt)

    # Generate a response
    handle_submit(prompt)
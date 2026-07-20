import hmac
import logging
import re
import uuid

import streamlit as st

from utils import write_message

# NOTE — Phase 2 hardening (auth-gated lazy init):
# `agent` and `text_rag` are NOT imported at module top-level. Importing them
# would transitively import `llm.py` and `graph.py`, whose module bodies open
# Google Gemini and Neo4j clients at import time. Deferring those imports
# until AFTER `check_password()` succeeds guarantees that a user who fails
# authentication triggers zero external calls. Python's `sys.modules` cache
# means the deferred `from ... import ...` is essentially free on repeat
# submissions — no per-turn cost.

logger = logging.getLogger(__name__)


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
    """비밀번호 일치 시 True, 아니면 입력창을 표시하고 False.

    Constant-time comparison via `hmac.compare_digest` — prevents input-length
    or early-mismatch timing side-channels from leaking password structure.
    Application-level rate limiting is intentionally NOT implemented here: it
    belongs at the deployment proxy (Streamlit Cloud IP throttling, or a
    reverse proxy in front); documented in README."""
    def _on_submit():
        entered = st.session_state.get("password") or ""
        expected = st.secrets["APP_PASSWORD"]
        # `compare_digest` requires both arguments to be str or bytes of the
        # same type. Streamlit always yields str; cast defensively.
        ok = hmac.compare_digest(str(entered), str(expected))
        st.session_state["auth_ok"] = ok
        # Clear the entered password from session state regardless of outcome
        # so a failed attempt does not leave the plaintext in memory across
        # reruns / subsequent screen captures.
        if "password" in st.session_state:
            del st.session_state["password"]

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

# ──────────────────────────────────────────────
# Sidebar: 챗봇 모드 토글 (graphRAG on/off)
# 켜짐 → graphRAG (그래프 관계 + 벡터, 풍부하지만 느림)
# 꺼짐 → textRAG (Entry 본문 벡터 검색만, 빠르지만 관계 취약)
# 두 모드는 messages_by_mode + Neo4j session_id suffix로 이력이 완전 분리됨.
# ──────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ 챗봇 모드")
    is_graphrag = st.toggle(
        "graphRAG 모드",
        value=st.session_state.get("chatbot_mode", "graphRAG") == "graphRAG",
        help=(
            "켜짐 (graphRAG): 그래프 관계 + 벡터 + 외부 authority. "
            "구조적 사실·관계·다국어 인용 우수. 응답 5~30초.\n\n"
            "꺼짐 (textRAG): Entry 본문 의미 기반 벡터 검색. 그래프 관계 추론은 "
            "수행하지 않고, Entry–Work 포함 관계는 출처 표기에만 사용. 응답 1~3초."
        ),
    )
    chatbot_mode = "graphRAG" if is_graphrag else "textRAG"
    st.session_state["chatbot_mode"] = chatbot_mode
    st.caption(
        f"**현재 모드**: `{chatbot_mode}`  \n"
        "두 모드는 별도의 대화 이력을 유지합니다."
    )


# ──────────────────────────────────────────────
# 모드별 초기 인사 메시지
# ──────────────────────────────────────────────
GREETING_GRAPHRAG = {
    "role": "assistant",
    "content": (
        "안녕하세요! **시화총림(詩話叢林) DB 챗봇 — graphRAG 모드**입니다.\n\n"
        "시화총림(詩話叢林)에 담긴 지식·정보를 그래프 데이터로 구조화하여 연결한 데이터를 검색합니다.\n\n"
        "인물, 비평, 장소, 시 등 9개의 클래스(Class)로 분류된 8,232개의 노드 데이터와 43,123개의 관계 데이터를 탐색할 수 있습니다.\n\n"
        "**질문 예시 (구조적 사실·관계)**\n"
        "- 이수광의 생몰년과 관직은?\n"
        "- 허균이 평한 시 목록을 알려줘\n"
        "- 지봉유설에 실린 '달'을 주제로 한 시는?\n"
        "- 칠언절구를 가장 많이 지은 시인은?\n"
        "- '기고(奇古)' 비평용어가 쓰인 비평문은?"
    ),
}

GREETING_TEXTRAG = {
    "role": "assistant",
    "content": (
        "안녕하세요! **시화총림(詩話叢林) DB 챗봇 — textRAG 모드**입니다.\n\n"
        "이 모드는 Entry 본문(한국어·한문·영어)에 대한 의미 기반 벡터 검색으로 답변합니다. "
        "그래프 관계 추론(작자·관직·시대·비평 관계 등 구조 질의)은 수행하지 않으며, "
        "Entry가 속한 시화집(Work) 정보는 출처 표기를 위해서만 사용합니다. "
        "의미·정서·주제 기반 질문에 빠르게 응답합니다.\n\n"
        "**질문 예시 (의미·주제 검색)**\n"
        "- 이별의 정한이 담긴 시를 소개해줘\n"
        "- 달빛을 노래한 구절이 있나?\n"
        "- 유배지에서 쓴 시 이미지는 어떤가?\n"
        "- 자연 이미지가 강렬한 비평은?\n\n"
        "구조적 사실(작자, 관직, 시대 등)이 필요한 질문은 사이드바에서 **graphRAG 모드**로 전환해 주세요."
    ),
}


# ──────────────────────────────────────────────
# 모드별 메시지 이력 초기화
# ──────────────────────────────────────────────
if "messages_by_mode" not in st.session_state:
    st.session_state["messages_by_mode"] = {
        "graphRAG": [GREETING_GRAPHRAG],
        "textRAG": [GREETING_TEXTRAG],
    }


# ──────────────────────────────────────────────
# 제출 핸들러 (모드에 따라 다른 백엔드 호출)
# ──────────────────────────────────────────────
# Localized fallback for infrastructure errors — Gemini/Neo4j misconfigured or
# briefly unavailable. Rendered instead of a raw stack trace / secret leak.
_INIT_FAILURE_MESSAGE = {
    "ko": "죄송합니다. 챗봇 서비스가 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    "en": "Sorry — the chatbot service is temporarily unavailable. Please try again shortly.",
    "zh": "抱歉，聊天服务暂时不可用。请稍后重试。",
}


def _init_failure_message() -> str:
    lang = st.session_state.get("effective_language", "ko")
    return _INIT_FAILURE_MESSAGE.get(lang, _INIT_FAILURE_MESSAGE["ko"])


def handle_submit(message: str, mode: str):
    """Route a user submission to the selected mode's backend.

    Backend modules (`agent`, `text_rag`) are imported LAZILY — the first call
    after authentication triggers Google Gemini and Neo4j client creation via
    Python's own import machinery. Subsequent calls hit the `sys.modules`
    cache and pay no re-import cost.

    A top-level guard converts any infrastructure or coding error into a
    localized safe message. Correlation id is logged server-side so operators
    can correlate without exposing raw exception text to the user."""
    with st.spinner("Thinking..."):
        try:
            if mode == "graphRAG":
                from agent import generate_response
                response = generate_response(message)
            else:
                from text_rag import generate_text_rag_response
                response = generate_text_rag_response(message)
        except Exception as exc:
            correlation_id = uuid.uuid4().hex[:8]
            logger.exception(
                "handle_submit failed [%s] mode=%s type=%s",
                correlation_id, mode, type(exc).__name__,
            )
            response = f"{_init_failure_message()} [ref: {correlation_id}]"
        st.session_state["messages_by_mode"][mode].append(
            {"role": "assistant", "content": response}
        )
        write_message("assistant", response, save=False)


# ──────────────────────────────────────────────
# 현재 모드의 메시지 표시
# ──────────────────────────────────────────────
for message in st.session_state["messages_by_mode"][chatbot_mode]:
    write_message(message["role"], message["content"], save=False)


# ──────────────────────────────────────────────
# 사용자 입력 처리 (모드별 placeholder)
# ──────────────────────────────────────────────
placeholder = (
    "한국어, 영어, 중국어(한문)로 질문해보세요"
    if chatbot_mode == "graphRAG"
    else "의미·주제 기반 질문을 입력해보세요 (텍스트 벡터 검색)"
)
if prompt := st.chat_input(placeholder):
    # 1) 명시적 락/해제 요청 처리
    explicit_lock = detect_explicit_lock(prompt)
    if explicit_lock:
        st.session_state["locked_language"] = explicit_lock
    elif detect_release_request(prompt):
        st.session_state.pop("locked_language", None)

    # 2) 이번 턴 적용 언어 결정
    st.session_state["effective_language"] = (
        st.session_state.get("locked_language") or detect_language(prompt)
    )

    # 3) 사용자 메시지 저장 + 즉시 표시
    st.session_state["messages_by_mode"][chatbot_mode].append(
        {"role": "user", "content": prompt}
    )
    write_message("user", prompt, save=False)

    # 4) 모드별 응답 생성
    handle_submit(prompt, chatbot_mode)
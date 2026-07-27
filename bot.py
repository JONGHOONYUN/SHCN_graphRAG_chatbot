import hmac
import logging
import uuid

import streamlit as st

from mode_labels import mode_display_label
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
# 언어 판별·제어 정책
# (work order CLAUDE_CODE_MIXED_SCRIPT_LANGUAGE_DETECTION_AND_ROUTING.md)
#
# 실제 판별/제어-구문 regex와 grammar-cue 기반 판별 로직은 전부
# `tools/language_policy.py`(streamlit/Neo4j/LLM을 import하지 않는 순수 모듈)로
# 이전되었다. 이 파일은 그 결과를 세션 상태에 반영하는 얇은 wrapper만 둔다.
#
# 세 가지 언어 상태:
#   question_language — 언어 제어 문구를 제거한 실제 질문(question_text)의 언어.
#                        vector index 선택에 사용.
#   response_language — 이번 턴 최종 출력 언어. LLM directive/Sources/인용
#                        순서/오류 문구/external locale에 사용.
#   locked_language    — 사용자가 세션에 고정한 응답 언어(response_language의
#                        override). 명시적 lock/release 전까지 유지.
#
# `effective_language`는 response_language의 하위 호환 alias로 계속 유지한다
# (기존 agent.py/text_rag.py/fallback 테스트가 이 키를 읽는 동안의 점진적 마이그
# 레이션 용도) — 새 코드가 vector index 선택에 이 값을 읽는 것은 금지.
#
# NOTE: `tools.language_policy`는 streamlit/Neo4j/LLM을 import하지 않는 순수
# 모듈이라 그 자체로는 backend 초기화를 유발하지 않지만, 위 Phase 2 하드닝
# 규칙("bot.py는 tools.* 를 top-level에서 import하지 않는다")과의 일관성을
# 위해 이 모듈도 lazy import로 유지한다 — 실제 사용 지점(입력 처리 블록)에서만
# import한다.
# ──────────────────────────────────────────────


def detect_language(text: str) -> str:
    """하위 호환 wrapper. 신규 코드는 `tools.language_policy.detect_question_language`
    를 직접 사용할 것 — 이 함수는 기존 참조가 있을 경우를 대비해서만 남긴다."""
    from tools.language_policy import detect_question_language

    return detect_question_language(text)


def detect_explicit_lock(text: str):
    """하위 호환 wrapper. 신규 코드는 `tools.language_policy.detect_language_control`
    를 직접 사용할 것."""
    from tools.language_policy import detect_language_control

    return detect_language_control(text).lock_language


def detect_release_request(text: str) -> bool:
    """하위 호환 wrapper. 신규 코드는 `tools.language_policy.detect_language_control`
    를 직접 사용할 것."""
    from tools.language_policy import detect_language_control

    return detect_language_control(text).release


# ──────────────────────────────────────────────
# control-only 턴(언어 lock/release만 있고 실제 질문이 없는 입력)에 대한
# 결정론적 확인 문구 (work order §6.4). RAG backend를 전혀 호출하지 않고,
# Sources도 붙이지 않는다.
# ──────────────────────────────────────────────
_LOCK_ONLY_CONFIRMATION = {
    "ko": "알겠습니다. 앞으로 한국어로 답변하겠습니다. 다시 자동 감지로 되돌리려면 "
          "'자동 언어 감지'라고 말씀해 주세요.",
    "en": "Got it — I will answer in English from now on. Say \"auto-detect "
          "language\" any time to switch back to matching each question's language.",
    "zh": "好的，我将从现在开始用中文回答。如需恢复自动语言检测，请说"
          "“自动检测语言”。",
}
_RELEASE_ONLY_CONFIRMATION = {
    "ko": "언어 고정을 해제했습니다. 이제부터는 질문 언어에 자동으로 맞춰 답변합니다.",
    "en": "Language lock released — I will automatically match each question's "
          "language from now on.",
    "zh": "已解除语言锁定，现在将根据每个问题的语言自动回答。",
}


def _control_only_response(resolution) -> str:
    """lock-only/release-only 입력에 대한 결정론적 확인 문구.
    `resolution.action`이 'lock'/'release' 둘 중 하나임을 호출부가 보장한다
    (control_only=True인 경우에만 호출됨)."""
    table = (_LOCK_ONLY_CONFIRMATION if resolution.action == "lock"
             else _RELEASE_ONLY_CONFIRMATION)
    return table.get(resolution.response_language, table["ko"])

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
            "꺼짐 (vectorRAG): Entry 본문 의미 기반 벡터 검색. 그래프 관계 추론은 "
            "수행하지 않고, Entry–Work 포함 관계는 출처 표기에만 사용. 응답 1~3초."
        ),
    )
    chatbot_mode = "graphRAG" if is_graphrag else "textRAG"
    st.session_state["chatbot_mode"] = chatbot_mode
    st.caption(
        f"**현재 모드**: `{mode_display_label(chatbot_mode)}`  \n"
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
        "안녕하세요! **시화총림(詩話叢林) DB 챗봇 — vectorRAG 모드**입니다.\n\n"
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
    # 1) 언어 상태 해석: 제어 문구 탐지·제거 + question/response/locked 언어 분리.
    #    control phrase가 포함된 원문 그대로를 검색·evidence에 넘기지 않도록,
    #    이 시점에 question_text(제어 문구 제거본)를 확정한다.
    from tools.language_policy import resolve_languages

    resolution = resolve_languages(prompt, st.session_state.get("locked_language"))

    if resolution.locked_language:
        st.session_state["locked_language"] = resolution.locked_language
    else:
        st.session_state.pop("locked_language", None)

    st.session_state["question_language"] = resolution.question_language
    st.session_state["response_language"] = resolution.response_language
    # 하위 호환 alias — response_language와 항상 동일하게 유지.
    # 신규 코드가 vector index 선택에 이 값을 읽는 것은 금지(question_language 사용).
    st.session_state["effective_language"] = resolution.response_language

    # 2) 사용자 메시지 저장 + 즉시 표시 — 화면·이력에는 항상 원문(raw prompt)을 표시.
    st.session_state["messages_by_mode"][chatbot_mode].append(
        {"role": "user", "content": prompt}
    )
    write_message("user", prompt, save=False)

    # 3) control-only 입력(실제 질문 없이 언어 lock/release만 있는 경우)은
    #    RAG backend를 호출하지 않고 결정론적 확인 문구만 반환한다.
    if resolution.control_only:
        confirmation = _control_only_response(resolution)
        st.session_state["messages_by_mode"][chatbot_mode].append(
            {"role": "assistant", "content": confirmation}
        )
        write_message("assistant", confirmation, save=False)
    else:
        # 4) 모드별 응답 생성 — 검색·평가에는 제어 문구가 제거된 question_text를 사용.
        handle_submit(resolution.question_text, chatbot_mode)
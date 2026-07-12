import streamlit as st
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


LANGUAGE_LABEL = {
    "ko": "Korean (한국어)",
    "en": "English",
    "zh": "Chinese (中文)",
}

# iteration/time limit 도달 시 AgentExecutor가 반환하는 placeholder를
# 세션 언어에 맞는 친화 안내로 교체. 사용자에게 의미 없는 영문 placeholder
# 노출을 방지.
ITERATION_LIMIT_PLACEHOLDER = "Agent stopped due to iteration limit or time limit."
ITERATION_LIMIT_FALLBACK = {
    "ko": "질문이 복잡하거나 적합한 자료를 찾지 못했습니다. 더 구체적인 키워드(인물명·서명·시기 등)로 다시 질문해 주세요.",
    "en": "I couldn't reach a clear answer within the search limit. Please try a more specific question (names, book titles, time period).",
    "zh": "在搜索范围内未能得出明确答案。请尝试更具体的问题（人物、书名、时期等）。",
}


def _build_language_directive(user_language: str) -> str:
    label = LANGUAGE_LABEL.get(user_language, LANGUAGE_LABEL["ko"])
    return (
        "# Response Language For This Turn\n"
        f"The response for THIS turn MUST be written in: {label}.\n"
        f"You MUST write the entire 'Final Answer:' in {label}, regardless of the "
        "language of tool outputs, source documents, or earlier turns in the chat history.\n"
        "Exception: source text fields (textChi, textKor, textEng, descEng) must still "
        "be quoted verbatim in their original characters. Only your own commentary, "
        f"explanations, section labels, and tool-routing notes follow the {label} rule.\n"
    )

from tools.vector import get_poetry_plot
from tools.cypher import cypher_qa_safe
from tools.external_authority import external_authority_lookup

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


# General Chat tool은 별도 LLM 호출이라 agent_prompt의 language_directive를 모름.
# 매 호출 시 session_state의 effective_language를 읽어 강한 언어 지시를 prepend한
# 동적 prompt로 chain을 다시 만들어 실행. 이렇게 해야 agent가 입력을 다른 언어로
# 번역해 넣었더라도 tool 출력은 사용자 언어로 강제됨.
def general_chat(input_text: str) -> str:
    user_language = st.session_state.get("effective_language", "ko")
    label = LANGUAGE_LABEL.get(user_language, LANGUAGE_LABEL["ko"])
    dyn_prompt = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                f"You MUST answer entirely in {label}, regardless of the language "
                "of the input text passed in {input}. "
                "당신은 한국 고전시화 전문가입니다. "
                "시화총림(詩話叢林)을 비롯한 조선시대 시화집(지봉유설, 성수시화, 호곡시화 등)의 "
                "인물·시·비평·주제·장소·시대 정보를 바탕으로 답변합니다. "
                "시화총림 데이터베이스와 직접 관련 없는 질문(일반 잡담, 현대 주제 등)에는 "
                "정중히 범위를 벗어난다고 안내하세요. "
                "확실하지 않은 내용은 추측하지 말고 'needs verification' 또는 그에 상당하는 "
                f"{label} 표현으로 답하세요. "
                "출처 텍스트(textChi, textKor, textEng)는 절대 번역·요약·변형하지 마세요."
            ),
            ("human", "{input}"),
        ]
    )
    chain = dyn_prompt | llm | StrOutputParser()
    return chain.invoke({"input": input_text})


# 1. tools 정의
tools = [
    Tool.from_function(
        name="General Chat",
        description= ("Use ONLY for greetings, casual conversation, or general questions about "
        "classical Korean poetry that do not require searching the database. "
        "Examples: 'Hello', 'What is sihwa?', 'Tell me about the Joseon dynasty'. "
        "Do NOT use for any query that asks about specific persons, poems, "
        "critiques, books, topics, places, or eras in the database."),
        func=general_chat,
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
        func = cypher_qa_safe
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
    func=lambda q: f"{cypher_qa_safe(q)}\n\n[보완 정보]\n{get_poetry_plot(q)}"
),
    Tool.from_function(
        name="External Authority Lookup",
        description=(
            "Fetch external biographical/reference data from public authorities "
            "using an external ID stored on a Person node. "
            "Use this AFTER Sihwa Graph Query has returned a Person node with "
            "an idWikidata or idAKSdigerati value, AND the user's question asks "
            "for details that go beyond the local graph: aliases, cross-lingual "
            "names, biographical dates confirmed by an international authority, "
            "occupation, dynasty context, or a short authoritative summary. "
            "Do NOT call for questions that only need graph facts (poem lists, "
            "critique relationships, etc.), and do NOT call speculatively when "
            "no external ID is present. "
            "Input format: 'source:id'. Supported sources: "
            "  - 'wikidata' with a Wikidata Q-id (e.g., 'wikidata:Q2913717' for 이규보) "
            "  - 'aks_digerati' with a koreanPerson_* id (e.g., 'aks_digerati:koreanPerson_18816'). "
            "Returns a JSON string. On failure returns a JSON with 'error' — "
            "in that case, proceed with graph-only information and note that "
            "external data was unavailable."
        ),
        func=external_authority_lookup,
    ),
]

def get_memory(session_id):
    return Neo4jChatMessageHistory(session_id=session_id, graph=graph)

# 2. agent_prompt 정의
agent_prompt = PromptTemplate.from_template("""
{language_directive}

You are an expert in East Asian humanities, specialising in Korean classical
poetry criticism (sihwa / 시화). You have deep knowledge of the Sihwa
Ch'ongnim (詩話叢林) compendium, its authors, poems, critiques, and the
literary-critical tradition it represents.
You respond ONLY in the locked session language declared above.


# ─────────────────────────────────────────────────────────────
# Data-Driven Reasoning Guide (실측 스키마 기반 종합 추론 원칙)
# ─────────────────────────────────────────────────────────────

## Corpus scale (참고 규모 — 답변할 수 있는 범위를 결정)
Nodes: Critique 1,828 · Poem 1,771 · Person 1,255 · Topic 1,060 · Entry 921 ·
       CriticalTerm 692 · Place 545 · Work 116 · Era 44 (총 8,232 노드)
Relationships: 43,133 총. HAS_SUBJECT_TOPIC(14,353) 이 가장 밀도 높고,
       HAS_AUDIENCE(179 · Poem에만), HAS_CONTRIBUTOR(16) 는 매우 희소.
Work 116개 중 25개(B001~B025)만 시화 원전 (파한집·지봉유설·성수시화·호곡시화 등),
나머지 91개는 시화 안에서 인용·언급되는 외부 참조 서적 (시경·논어·태평광기 등).

## Scope guardrails (반드시 준수)
- IN scope: 이 그래프에 등재된 25개 시화집의 저자·시·비평·주제·비평용어·
  시대·장소, 그리고 이들 사이 관계와 그로부터 도출되는 사실.
- OUT of scope: 근·현대 시, 등재되지 않은 인물·시·비평, 사용자 개인 정보,
  이전 채팅 세션 로그(:Message/:Session 노드), 개인 견해 요청.
  이런 질문은 정중히 범위 밖임을 안내하고 시화총림 관련 대체 질문을 제안.

## Synthesis rule — Three Person roles (항상 구별, 혼동 금지)
한 텍스트에 여러 인물이 등장할 때 관계 방향과 역할을 절대 왜곡하지 마세요:
  1) AUTHOR    : (text)-[:HAS_CREATOR]->(person)         # 지은이 (총 4,459 edges)
  2) SUBJECT   : (text)-[:HAS_SUBJECT_PERSON]->(person)  # 평가·언급 대상 (5,384)
  3) ADDRESSEE : (Poem)-[:HAS_AUDIENCE]->(person)        # 시의 수신자 (179, Poem 전용)
예) "허균이 이백을 논한 비평"과 "허균이 이백에게 준 시"는 완전히 다른 관계이므로
     tool observation을 읽을 때 이 세 역할을 색인·검증한 뒤 답변에 반영하세요.

## Citation rule — Work의 두 종류 구분
- 시화 원전 (B001~B025, descEng 상세): 1차 사료. 전체 출처 경로 인용.
      예: 지봉유설(B016) > 제N항목(E###) > 제N시(M###)
- 외부 참조 서적 (B026~B131, position 없거나 descEng 없음): 배경 컨텍스트만.
      예: 시경(B035), 논어(B067), 태평광기(B077)는 참고 문헌으로만 언급하고
      "이 시가 실려 있는 시화" 로 잘못 인용하지 마세요.

## Chronology fallback chain (없는 정보는 지어내지 말 것)
  1) Person.yearBirth / Person.yearDeath      (정확한 연도, 우선)
  2) (Person)-[:HAS_ERA]->(Era).yearStart/yearEnd  (시대 범위 폴백)
  3) Era.nameKor / Era.nameEng                 (시대명만)
  4) 없음 → 답변에서 '기록되지 않음' / 'not recorded in the database' 로 명시
Era는 계층 구조(예: 조선 → 조선 후기)를 가지므로 상위 시대 질의 시 하위 시대
결과도 그 상위에 속함을 인지하고 종합.

## Density-aware tool routing (희소 관계 대응)
- HAS_SUBJECT_TOPIC(14,353), HAS_PART(5,492), HAS_SUBJECT_PERSON(5,384),
  HAS_CREATOR(4,459), HAS_TYPE(3,520): 밀도 높음 → Sihwa Graph Query 단독으로 충분.
- HAS_SUBJECT_CRITICAL_TERM(2,390), HAS_SUBJECT_PLACE(1,656), HAS_SUBJECT_TEXT(1,627):
  중간 밀도 → Graph Query 우선, 빈 결과 시 Combined Search 폴백.
- HAS_AUDIENCE(179 · Poem에만), HAS_CONTRIBUTOR(16), HAS_SUBJECT_ERA(307):
  희소 → Graph Query가 빈 결과 낼 가능성 큼. Combined Sihwa Search를 먼저
  시도하거나 결과가 <3이면 즉시 Content Search 폴백.

## Intertextual relationships (텍스트 간 인용·참조 — 1,624 edges 총)
- Critique → Poem 논평 (1,516): "X 비평문이 Y 시를 논함"
- Poem → Poem 참조 (108):        시-시 상호텍스트 인용
이런 상호텍스트 질문("A가 인용한 B", "Y를 평한 비평문")은 HAS_SUBJECT_TEXT 관계로
표현되므로 Sihwa Graph Query가 정확도 우수.

## Cross-linguistic entity resolution
동일 인물·개념의 다양한 표기를 통합하여 인식:
- 이규보 / 李奎報 / Yi Kyubo         (nameKor / nameChi / nameMR)
- 조선 / 朝鮮 / Chosŏn / Joseon        (Era)
- 두보 / 杜甫 / Du Fu / Tu Fu          (Person)
Tool observation에서 이 세 형태 중 하나만 매치되어도 다른 형태로 표기된 사용자
질문과 같은 실체임을 인지하고 답변에 통합.


## Anti-Hallucination Rule — External Authority Lookup 결과 처리 (엄격 준수)
LLM(당신)의 pretrained 지식에서 오는 인물 상세(관직 이력, 가족 관계, 자·호 목록,
행적 등)를 답변에 절대 포함하지 마세요. 답변에 등장하는 모든 전기적 사실은 반드시
다음 세 정보원 중 하나의 실제 tool observation에 있어야 합니다:
  1) Sihwa Graph Query가 반환한 필드
  2) External Authority Lookup(wikidata:*)의 실제 JSON 필드
  3) External Authority Lookup(aks_digerati:*)의 실제 JSON 필드

External Authority Lookup 응답 처리 규칙:
- 응답 JSON에 'error' 필드가 있으면 그 authority에서 얻은 정보를 답변에 넣지 말고,
  "{Wikidata|AKS Digerati}에서는 이 인물의 데이터를 조회하지 못했습니다"라고 명시.
- 응답 JSON에 'schema_hint' 필드가 있으면 그 목록에 없는 유형의 정보는
  자체 지식으로 채우지 말고 아예 생략.
- AKS Digerati API가 반환하는 사실 유형은 다음이 전부입니다:
      · KoName, ChName, YearBirth, YearDeath
      · aliases (aks_PersonAliases: 字/號/諡號 등)
      · addresses (aks_Address: 籍貫 등)
      · examination_entries (aks_Entry: 급제/입사 이력)
      · canonical_link
  → 관직 이력·가족 관계·전기 서술은 이 API에 존재하지 않습니다.
    당신이 그런 내용을 알고 있더라도 답변에 넣지 마세요.
    필요하다면 "AKS Digerati에는 이 항목이 없습니다"라고 안내.
- 신뢰성 정책: 학술 챗봇의 근간이므로 검증되지 않은 사실을 넣느니 정보 부족을
  명시하는 편이 훨씬 낫습니다. 모호할 때는 반드시 생략.

### Concrete negative examples — 이런 답변은 절대 금지:

❌ 금지 사례 1 (관직 이력 창작):
   AKS 응답에 aks_Entry가 [{RuShiType: '進士試', RuShiYear: '1189'}]만 있는데
   답변에 "문하시랑평장사, 좌사간, 한림학사, 국자감좨주를 역임했다"고 쓰기.
   → API가 관직 이력을 반환하지 않았으므로 절대 지어내지 말 것.
✅ 올바른 처리: "AKS Digerati에는 1189년 進士試 합격 기록만 있고 관직 이력은
   포함되지 않습니다. Sihwa 그래프의 HAS_OFFICE 관계에서 [실제 값]이 확인됩니다."
   (그래프에 있으면 그래프 값만 인용, 없으면 두 사료 모두에 없다고 명시)

❌ 금지 사례 2 (지명 확대·왜곡):
   aks_Address에 [{AddrType: '籍貫', AddrName: '驪州'}]만 있는데
   답변에 "출생지: 황해도 해주"라고 쓰기 (驪州를 海州로 오독).
   → 驪州는 경기도 여주(黃驪→驪州)이고 海州는 황해도의 별개 지명입니다.
   → API의 원문자를 그대로 사용해야 합니다.
✅ 올바른 처리: "本貫은 驪州(경기도 여주)입니다." (原文 그대로 표기)

❌ 금지 사례 3 (본관 창작):
   aks_Address에 '驪州'가 있는데 "본관: 전주 이씨"라고 쓰기.
   → 驪州(여주) 이씨가 맞으며, 전주 이씨는 완전히 다른 본관.
✅ 올바른 처리: aks_Address 원본 값을 그대로 사용.

❌ 금지 사례 4 (가족·관련 인물 창작):
   AKS 응답에 가족 정보가 전혀 없는데 "아버지는 이윤수, 최충헌·최우와 교유"라고 쓰기.
   → 이런 관계 정보는 이 API에 없습니다. Sihwa 그래프의 HAS_CREATOR /
     HAS_SUBJECT_PERSON 관계에서 확인된 것만 사용.
✅ 올바른 처리: AKS 관련 부분에서는 관계 정보를 아예 언급하지 않고,
   그래프에서 확인된 관계만 별도 인용.

❌ 금지 사례 5 (저작 목록 창작):
   AKS 응답에 저작이 없는데 "『동국이상국집』, 『백운소설』을 남겼다"고 쓰기.
   → 저작 정보는 이 API에 없습니다. 시화총림 그래프의 Work 노드에서
     HAS_CREATOR 관계로 확인된 것만 사용.
✅ 올바른 처리: 그래프에서 이 인물이 HAS_CREATOR 관계인 Work·Poem·Critique만 인용.

Rule of thumb: 응답 JSON에 MUST_NOT_ADD 필드가 있으면 그 목록에 명시된 카테고리는
당신이 아무리 확실하다고 생각해도 답변에 넣지 마세요. 대신 answer_template_when_missing
같은 안내 문구를 사용자 언어로 번역해서 사용하세요.


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

Example 8 — Authority-enriched biographical request
Q (EN): "Tell me about Yi Kyubo in detail, including his international recognition."
Q (KO): "이규보에 대해 자세히 알려줘. 국제적으로는 어떻게 알려져 있어?"
Q (ZH): "详细介绍李奎报，包括他在国际上的知名度。"
Tools (in order): Sihwa Graph Query → External Authority Lookup ('wikidata:<Q-id>') → External Authority Lookup ('aks_digerati:<koreanPerson_id>')
Reason: Named person + detail request that goes beyond the graph.
        Step 1 gets nameKor/Chi/dates/works/idWikidata/idAKSdigerati.
        Step 2 uses the returned idWikidata (e.g., Q2913717) to fetch canonical
        cross-lingual names, aliases, and a one-line description.
        Step 3 uses the returned idAKSdigerati to fetch Korean-domain
        biographical detail. Only call External Authority Lookup when the
        corresponding external ID actually appears in the graph result; if
        an ID is missing or the lookup returns an error, proceed with
        graph-only information and note the gap.



# Multi-step Reasoning
## Multi-Step Reasoning
For complex queries, break them into steps rather than attempting one query:
Example A: '지봉유설에 실린 달 주제 시의 작자는 어느 시대 사람인가?'
Step 1: Sihwa Content Search -> find moon-themed entries in Jibong yusol
Step 2: Extract author names from results
Step 3: Sihwa Graph Query -> retrieve yearBirth and HAS_ERA for each author
Step 4: Synthesise and present combined result

Example B (authority-enriched biography): '이규보에 대해 자세히 알려줘'
Step 1: Sihwa Graph Query -> retrieve Person (nameKor, nameChi, yearBirth,
        yearDeath, HAS_ERA, idWikidata, idAKSdigerati, related Poems/Critiques)
Step 2: If idWikidata is present in Step 1 output → call
        External Authority Lookup with 'wikidata:<Q-id>' to get canonical
        cross-lingual names, aliases, dates, one-line authoritative summary.
Step 3: If idAKSdigerati is present → call
        External Authority Lookup with 'aks_digerati:<koreanPerson_id>' to
        retrieve the ACTUAL fields returned by that API: KoName, ChName,
        YearBirth, YearDeath, aliases (字/號/諡號), addresses (籍貫 only),
        examination entries (급제/입사 이력), canonical_link.
        The AKS Digerati API does NOT return career/office history, family
        relations, or extended biography. If the tool result includes a
        'schema_hint' field listing available fields, strictly obey that list.
Step 4: Synthesise: sihwa role/works (graph) + international identity
        (Wikidata) + Korean-domain context (AKS). Always cite each source
        explicitly. If any authority lookup returned an 'error' field, say
        that external data was unavailable and proceed with graph-only facts.
        Never fabricate external content.



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


# CRITICAL — RESPONSE LANGUAGE LOCK (highest priority, overrides everything below)
{language_directive}

This rule applies to every "Final Answer:" you produce.
- If any tool Observation returns text in a different language, you MUST
  rephrase your "Final Answer:" in the locked session language above.
- All introductions, explanations, section headings, bullet labels, and
  background notes that YOU write must be in the locked language.
- Source text fields (textChi, textKor, textEng, descEng) keep their
  original characters when quoted verbatim — but the surrounding sentences,
  labels, and commentary are in the locked language.
- The fallback note "(그래프 태그 데이터 부족으로 텍스트 유사도 검색으로 보완하였습니다.)"
  must also be translated into the locked language.
- The phrase "제공된 자료에 없습니다" must be translated into the locked
  language (e.g. "Not found in the provided data." for English,
  "在提供的资料中未找到。" for Chinese).

TOOLS:
------

You have access to the following tools:

{tools}

To use a tool, you MUST use this EXACT format. Each label (`Thought:`, `Action:`, `Action Input:`) MUST start on its OWN NEW LINE. Never concatenate them on the same line.

```
Thought: Do I need to use a tool? Yes
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
```

When you have a response to say to the Human, or if you do not need to use a tool, you MUST use this format. `Thought:` and `Final Answer:` MUST start on separate new lines:

```
Thought: Do I need to use a tool? No
Final Answer: [your response here]
```

FORMAT RULES (mandatory):
- `Thought:`, `Action:`, `Action Input:`, `Final Answer:` each begin a NEW LINE.
- Never write `Thought: ... Action: ...` on a single line.
- After `Action:` there MUST be an `Action Input:` line.
- Never mix `Action:` and `Final Answer:` in the same step — choose one or the other per step.

ACTION INPUT LANGUAGE RULE (mandatory):
- `Action Input:` MUST contain the user's question (or a faithful paraphrase) in
  the user's ORIGINAL language. Do NOT translate the user's input into another
  language before passing it to a tool. Example: if the user wrote in English,
  the Action Input is in English; if in Chinese, in Chinese.
- Translation/rewriting into the locked response language is done ONLY at the
  `Final Answer:` stage, never at `Action Input:` stage.

WRONG (will fail parsing):
`Thought: I should search. Action: Sihwa Content Search`

CORRECT:
```
Thought: I should search the content.
Action: Sihwa Content Search
Action Input: poems about food
```

REMINDER (do not skip): {language_directive}
Before writing "Final Answer:", verify your answer is in the locked language above.

Begin!

Previous conversation history:
{chat_history}

New input: {input}
{agent_scratchpad}
""")

# 3. agent 생성
agent = create_react_agent(llm, tools, agent_prompt)

def _parse_error_handler(error) -> str:
    """ReAct format 위반 시 구체적인 자기수정 안내를 Observation으로 반환.
    Gemini가 Thought/Action/Action Input을 한 줄에 합치거나 Action Input을
    누락하는 사례가 잦아 다음 iteration에서 실수를 바로잡도록 명시한다."""
    return (
        "Your last output could not be parsed. Most likely causes:\n"
        "- `Thought:` and `Action:` were on the same line (they MUST be on SEPARATE new lines).\n"
        "- Missing `Action Input:` line after `Action:`.\n"
        "- Mixing `Action:` and `Final Answer:` in the same step.\n\n"
        "Re-output using this EXACT format. Each label starts on its OWN new line:\n\n"
        "Thought: <your reasoning>\n"
        "Action: <one tool name>\n"
        "Action Input: <input string>\n\n"
        "OR, to finish:\n\n"
        "Thought: <your reasoning>\n"
        "Final Answer: <your answer>\n"
    )


# 4. agent_executor 생성
agent_executor = AgentExecutor(
    agent=agent,
    tools=tools,
    verbose=True,
    # callable로 지정하여 매 parsing 실패마다 구체적인 자기수정 instruction을
    # Observation으로 전달. 단순 문자열보다 Gemini의 회복 성공률이 높음.
    handle_parsing_errors=_parse_error_handler,
    max_iterations=15,
    # early_stopping_method는 기본값("force") 사용.
    # create_react_agent가 만드는 RunnableAgent는 "generate"를 지원하지 않아
    # ValueError를 raise하므로 명시하지 않음. placeholder 출력은 generate_response에서 후처리.
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

    # bot.py가 매 턴 결정한 적용 언어(effective_language)를 읽어 prompt에 주입.
    # - 사용자가 명시적 락 요청을 한 적이 있으면 그 락 언어
    # - 그렇지 않으면 이번 질문의 자동 감지 언어
    # session_state가 없으면(직접 호출 등) 한국어로 폴백.
    user_language = st.session_state.get("effective_language", "ko")
    language_directive = _build_language_directive(user_language)

    try:
        # session_id에 ::graphRAG suffix로 textRAG 이력과 완전 분리
        response = chat_agent.invoke(
            {"input": user_input, "language_directive": language_directive},
            {"configurable": {"session_id": f"{get_session_id()}::graphRAG"}},)
    except ValueError as e:
        # Gemini가 빈 스트림을 반환한 경우 ("No generation chunks were returned").
        # 원인 후보: safety filter false positive, 일시적 API 오류, 누적 prompt 혼란.
        # 사용자에게 빨간 에러 페이지 대신 락 언어로 안내 메시지 반환.
        if "No generation chunks were returned" in str(e):
            return ITERATION_LIMIT_FALLBACK.get(user_language, ITERATION_LIMIT_FALLBACK["ko"])
        raise

    output = response['output']
    # iteration limit placeholder를 세션 언어 친화 안내로 교체
    if ITERATION_LIMIT_PLACEHOLDER in output:
        return ITERATION_LIMIT_FALLBACK.get(user_language, ITERATION_LIMIT_FALLBACK["ko"])
    return output
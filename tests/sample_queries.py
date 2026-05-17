SAMPLE_QUERIES = [
    {
        "label": "Query 1 — Author lookup with external enrichment",
        "en": "Show me poems written by Yi Kyubo.",
        "ko": "이규보가 쓴 시를 보여주세요.",
        "zh": "请展示李奎报写的诗。",
        "expected_tool": "Sihwa Graph Query",
        "strategy": (
            "Match Person by nameKor CONTAINS '이규보' or nameChi CONTAINS '李奎報', "
            "then HAS_CREATOR -> Poem. Return textChi, textKor, textEng with full "
            "provenance. Check for idAKSdigerati and retrieve external data."
        ),
        "tests": (
            "Name matching across scripts; external API enrichment; provenance "
            "attribution; response language switching; nameEng (EN/FR), nameChi (ZH)."
        ),
    },
    {
        "label": "Query 2 — Gender-filtered topic aggregation",
        "en": "What topics did women poets mainly write about?",
        "ko": "여성 시인들이 주로 어떤 주제를 썼나요?",
        "zh": "女性诗人主要写哪些主题？",
        "expected_tool": "Sihwa Graph Query",
        "strategy": (
            "Person HAS_GENDER -> Topic(nameEng='female'), "
            "Person HAS_CREATOR -> Poem, "
            "Poem HAS_SUBJECT_TOPIC -> Topic. "
            "Aggregate by Topic.nameKor, order by count descending."
        ),
        "tests": (
            "Gender filtering via Topic node (not string property); "
            "topic aggregation; multilingual Topic name display."
        ),
    },
    {
        "label": "Query 3 — Critical term tracking for a specific person",
        "en": "What critical terms were used to evaluate Ch'oe Ch'iwon?",
        "ko": "최치원은 어떤 비평어로 평가받았나요?",
        "zh": "崔致远受到了哪些批评术语的评价？",
        "expected_tool": "Sihwa Graph Query",
        "strategy": (
            "Critique HAS_SUBJECT_PERSON -> Person(nameKor CONTAINS '최치원' OR "
            "nameChi CONTAINS '崔致遠'), "
            "Critique HAS_SUBJECT_CRITICALTERM -> CriticalTerm, "
            "Critique HAS_CREATOR -> Person (critic). "
            "Return CriticalTerm.nameKor, nameChi, nameEng and "
            "Critique.textChi, textKor, textEng with provenance."
        ),
        "tests": (
            "HAS_CREATOR (critic) vs HAS_SUBJECT_PERSON (subject) distinction; "
            "CriticalTerm retrieval; Sinitic name matching from Chinese query."
        ),
    },
    {
        "label": "Query 4 — Era-based topic trend analysis",
        "en": "What were the most common topics among Goryeo-period poets?",
        "ko": "고려 시대 시인들이 가장 많이 쓴 주제는 무엇인가요?",
        "zh": "高丽时代的诗人最常写哪些主题？",
        "expected_tool": "Sihwa Graph Query",
        "strategy": (
            "Person HAS_ERA -> Era(nameKor='고려') OR Person.yearBirth between "
            "Era.yearStart and Era.yearEnd, "
            "Person HAS_CREATOR -> Poem, "
            "Poem HAS_SUBJECT_TOPIC -> Topic. "
            "Aggregate by Topic.nameKor, order by count descending, LIMIT 20."
        ),
        "tests": (
            "Era filtering with yearBirth/yearDeath fallback to HAS_ERA; "
            "topic aggregation; correct response language."
        ),
    },
    {
        "label": "Query 5 — Cross-reference search for a Chinese historical figure",
        "en": "Which entries in the compendium mention Du Fu?",
        "ko": "시화총림에서 두보를 언급하는 항목은 어떤 것들이 있나요?",
        "zh": "诗话丛林中哪些条目提到了杜甫？",
        "expected_tool": "Sihwa Graph Query",
        "strategy": (
            "Entry HAS_SUBJECT_PERSON -> Person(nameChi CONTAINS '杜甫' OR "
            "nameKor CONTAINS '두보'). "
            "Return Entry.id, Entry.position, parent Book.nameEng, Book.id, "
            "and provenance-attributed text excerpts."
        ),
        "tests": (
            "Sinitic name matching; HAS_SUBJECT_PERSON path; provenance chain; "
            "Chinese query: prefer nameChi + namePY over nameEng."
        ),
    },
    {
        "label": "Query 6 — Thematic imagery search (content-based)",
        "en": "What imagery appears in farewell or parting poems?",
        "ko": "이별을 주제로 한 시에는 어떤 이미지가 등장하나요?",
        "zh": "送别诗中出现了哪些意象？",
        "expected_tool": "Combined Sihwa Search",
        "strategy": (
            "First: Sihwa Graph Query for HAS_SUBJECT_TOPIC -> Topic related to "
            "farewell/parting. If results < 3: Sihwa Content Search on textKor "
            "for semantic similarity to farewell imagery. Enrich all results with "
            "graph metadata (author, book, era)."
        ),
        "tests": (
            "Graph-first then vector fallback; topic tag lookup; imagery analysis "
            "from text content; graph metadata enrichment of vector results."
        ),
    },
    {
        "label": "Query 7 — Supernatural / dream poems (sparse tag fallback)",
        "en": "Are there poems said to have been written by ghosts or given in dreams?",
        "ko": "귀신이 지었다거나 꿈에서 받았다는 시가 있나요?",
        "zh": "有没有据说由鬼魂所作或在梦中获得的诗？",
        "expected_tool": "Combined Sihwa Search",
        "strategy": (
            "First: Sihwa Graph Query for HAS_SUBJECT_TOPIC related to dreams or "
            "supernatural. Expected: sparse results due to tagging gaps. "
            "Then: Sihwa Content Search on textKor for semantic similarity. "
            "Note in response if results rely on text search due to missing tags."
        ),
        "tests": (
            "Graceful fallback when graph tags are sparse; transparency note to "
            "user about tagging gaps; vector search on unusual thematic content."
        ),
    },
]

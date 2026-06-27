# Entry 노드의 textKor / textChi / textEng 속성에 대한 임베딩을 생성하여
# 각각 textEmbedding_Kor / textEmbedding_Chi / textEmbedding_Eng 속성으로 저장.
# 배치 단위로 처리하여 효율적으로 임베딩을 생성하고,
# 이미 처리된 노드는 건너뛰므로 재실행 안전(idempotent).

import re
import streamlit as st
from llm import embeddings
from graph import graph

st.title("Entry 임베딩 생성 (textKor / textChi / textEng)")

# 처리할 (소스 텍스트 속성, 저장할 벡터 속성, UI 라벨) 정의.
# 새 언어를 추가하려면 이 리스트에 항목 하나만 추가하면 됨.
EMBEDDING_TARGETS = [
    ("textKor", "textEmbedding_Kor", "한국어"),
    ("textChi", "textEmbedding_Chi", "한문"),
    ("textEng", "textEmbedding_Eng", "영어"),
]

BATCH_SIZE = 50


# 위키마크업 정제 함수 (모든 언어 공통)
def clean_text(text):
    """{{Type|ID|Name}} → Name 형태로 변환하여 임베딩 품질 향상"""
    if not text:
        return ""
    # {{Type|ID|Name}} 패턴은 Name만 남김
    cleaned = re.sub(r'\{\{[^|]+\|[^|]+\|([^}]+)\}\}', r'\1', text)
    # {{Type|ID|Chi}} 같은 placeholder는 제거
    cleaned = re.sub(r'\{\{[^}]+\}\}', '', cleaned)
    # <br>, <i> 등 HTML 태그 제거
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    # 연속 공백 정리
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def process_property(source_prop: str, target_prop: str, label: str):
    """단일 텍스트 속성 → 단일 벡터 속성 임베딩 처리.

    Cypher는 속성명을 파라미터로 받지 못해 f-string 인터폴레이션을 사용한다.
    source_prop / target_prop 은 모두 코드 상단 EMBEDDING_TARGETS의 하드코드 값이므로
    인젝션 위험 없음.
    """
    st.markdown(f"---\n### {label} ({source_prop} → {target_prop})")

    # 임베딩 대상 조회: 소스 텍스트가 있고 아직 타깃 벡터가 없는 Entry
    results = graph.query(f"""
        MATCH (e:Entry)
        WHERE e.{source_prop} IS NOT NULL AND e.{target_prop} IS NULL
        RETURN elementId(e) AS id, e.{source_prop} AS text
    """)

    st.write(f"임베딩 생성 대상: {len(results)}개")

    if len(results) == 0:
        st.success(f"이미 모든 Entry 노드에 {target_prop}가 존재합니다.")
        return

    for i in range(0, len(results), BATCH_SIZE):
        batch = results[i:i+BATCH_SIZE]
        # 정제된 텍스트로 임베딩
        texts = [clean_text(r["text"]) for r in batch]
        vectors = embeddings.embed_documents(texts)

        graph.query(f"""
            UNWIND $data AS row
            MATCH (e:Entry) WHERE elementId(e) = row.id
            SET e.{target_prop} = row.embedding
        """, params={"data": [
            {"id": r["id"], "embedding": v}
            for r, v in zip(batch, vectors)
        ]})

        st.write(f"진행: {min(i+BATCH_SIZE, len(results))}/{len(results)} 완료")

    st.success(f"{label} 임베딩 생성 완료!")


# 세 언어 순차 처리
for source_prop, target_prop, label in EMBEDDING_TARGETS:
    process_property(source_prop, target_prop, label)

st.markdown("---")
st.success("✅ 모든 언어(textKor / textChi / textEng) 임베딩 처리 완료!")

# streamlit run create_embeddings.py 로 실행

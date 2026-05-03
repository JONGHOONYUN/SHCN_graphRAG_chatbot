# Entry 노드의 textKor 속성에 대한 임베딩을 생성하여 textEmbedding 속성에 저장하는 코드입니다. 배치 단위로 처리하여 효율적으로 임베딩을 생성합니다.

import re
import streamlit as st
from llm import embeddings
from graph import graph

st.title("Entry 임베딩 생성")

# 위키마크업 정제 함수
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

# 임베딩 대상 조회
results = graph.query("""
    MATCH (e:Entry)
    WHERE e.textKor IS NOT NULL AND e.textEmbedding IS NULL
    RETURN elementId(e) AS id, e.textKor AS text
""")

st.write(f"임베딩 생성 대상: {len(results)}개")

if len(results) == 0:
    st.success("이미 모든 Entry 노드에 임베딩이 존재합니다.")
else:
    batch_size = 50
    for i in range(0, len(results), batch_size):
        batch = results[i:i+batch_size]
        # 정제된 텍스트로 임베딩
        texts = [clean_text(r["text"]) for r in batch]
        vectors = embeddings.embed_documents(texts)

        graph.query("""
            UNWIND $data AS row
            MATCH (e:Entry) WHERE elementId(e) = row.id
            SET e.textEmbedding = row.embedding
        """, params={"data": [
            {"id": r["id"], "embedding": v}
            for r, v in zip(batch, vectors)
        ]})

        st.write(f"진행: {min(i+batch_size, len(results))}/{len(results)} 완료")

    st.success("모든 Entry 임베딩 생성 완료!")\
    
    # streamlit run create_embeddings.py 로 실행
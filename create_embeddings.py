import streamlit as st
from llm import embeddings
from graph import graph

st.title("Book 임베딩 생성")

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
    texts = [r["textKor"] for r in batch]
    vectors = embeddings.embed_documents(texts)

        graph.query("""
        UNWIND $data AS row
        MATCH (e:Entry) WHERE elementId(e) = row.id
        SET e.textEmbedding = row.embedding
    """, params={"data": [{"id": r["id"], "embedding": v} for r, v in zip(batch, vectors)]})

        st.write(f"진행: {min(i+batch_size, len(results))}/{len(results)} 완료")

    st.success("모든 임베딩 생성 완료!")

import streamlit as st

# Connect to Neo4j
#
# This module holds the single, long-lived Neo4jGraph instance reused across
# the whole Streamlit process — including across a stale/defunct Bolt
# connection after an Aura maintenance blip. `Neo4jGraph.query()` already
# delegates to the underlying driver's `execute_query()`, which retries
# Neo4j-classified transient errors internally; on top of that, ALL
# READ-ONLY graph retrieval (free-form LLM Cypher and the deterministic
# ranking template) is additionally wrapped by `tools.cypher_safety`'s
# `SafeNeo4jGraph` / `_SafeNeo4jGraphSubclass`, which adds a bounded,
# application-level retry (`_read_with_retry`, default 3 attempts / doubling
# backoff from 0.5s, overridable via GRAPH_READ_RETRY_MAX_ATTEMPTS /
# GRAPH_READ_RETRY_BACKOFF_SECONDS) specifically for transport-level
# failures (ServiceUnavailable, SessionExpired, TransientError, OSError —
# e.g. "Failed to read from defunct connection"). That policy intentionally
# lives in tools/cypher_safety.py, not here, so it applies ONLY to read-only
# graph retrieval — never to Neo4jChatMessageHistory writes and never to
# external-authority HTTP fetches, neither of which goes through
# `SafeNeo4jGraph`. No `driver_config` override is added here: this
# langchain_neo4j/neo4j version pair was not verified to need one beyond
# the driver's own defaults, and the work order explicitly disallows
# guessing unsupported connection arguments.

from langchain_neo4j import Neo4jGraph

graph = Neo4jGraph(
    url=st.secrets["NEO4J_URI"],
    username=st.secrets["NEO4J_USERNAME"],
    password=st.secrets["NEO4J_PASSWORD"],
    database=st.secrets["NEO4J_DATABASE"],
)
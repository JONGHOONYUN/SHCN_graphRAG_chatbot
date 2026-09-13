"""Domain layer — the structured evidence data contract.

책임: Evidence/Entity/Provenance/NodeReference 모델, 내부 node ID 규칙,
Entity/NodeReference 병합. 입력은 순수 파이썬 값, 출력은 dataclass 인스턴스.
허용 의존성: 표준 라이브러리만 (streamlit/langchain/neo4j/requests 금지).
외부 부작용: 없음 (경고 로그만; logger 이름은 기존 운영 로그 연속성을 위해
"tools.evidence"를 유지한다).
기존 facade: tools/evidence.py가 이 계층의 모든 심볼을 re-export한다.
"""

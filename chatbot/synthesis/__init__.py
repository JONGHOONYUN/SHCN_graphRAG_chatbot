"""Synthesis layer — deterministic evidence formatting and citation building.

책임: source/conflict 정책 텍스트, 대화 이력 직렬화, evidence block 포맷과
예산(budget), 결정론적 citation 생성. LLM 호출·Neo4j 접근·Streamlit 접근은
절대 하지 않는다 — 이 계층의 출력은 순수 문자열/리스트다.
허용 의존성: 표준 라이브러리 + chatbot.domain (+ allowlist 조회를 위한
chatbot.authority.registry 지연 import — 순수 모듈).
외부 부작용: 없음.
기존 facade: tools/synthesis.py (모든 심볼 re-export).
"""

"""Provider token-usage normalization — official metadata only, never estimated.

LangChain·provider 버전에 따라 usage가 들어 있는 위치와 키 이름이 다르다. 이
모듈은 아래 위치를 순서대로 살펴 **처음으로 값을 제공한 한 곳만** 사용한다
(여러 위치를 합산하면 같은 usage를 중복 계산하게 된다).

  1. generation.message.usage_metadata          LangChain 표준
                                                (input_tokens/output_tokens/total_tokens)
  2. generation.message.response_metadata        provider 원형
       ["usage_metadata"]  Google (prompt_token_count/candidates_token_count/total_token_count)
       ["token_usage"] / ["usage"]  OpenAI 계열 (prompt_tokens/completion_tokens/total_tokens)
  3. generation.generation_info                  위 2와 같은 키
  4. LLMResult.llm_output                        위 2와 같은 키

일부 값만 제공되면 제공된 값만 기록한다(합계를 직접 계산하지 않는다). 아무 값도
없으면 세 값 모두 None, `available=False`다. 문자 수로 토큰을 추정하지 않는다.
streaming chunk는 보지 않으며, 최종 결과의 첫 generation 한 개만 사용한다.

허용 의존성: 표준 라이브러리만 (LangChain 객체는 duck-typing으로 읽는다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

_KEY_SETS = (
    ("input_tokens", "output_tokens", "total_tokens"),                 # LangChain
    ("prompt_token_count", "candidates_token_count", "total_token_count"),  # Google
    ("prompt_tokens", "completion_tokens", "total_tokens"),            # OpenAI style
)
_CONTAINER_KEYS = ("usage_metadata", "token_usage", "usage")


@dataclass(frozen=True)
class Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    @property
    def available(self) -> bool:
        return any(v is not None for v in
                   (self.input_tokens, self.output_tokens, self.total_tokens))


NO_USAGE = Usage()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _from_mapping(raw: Any) -> Usage:
    """Pick the key set that matches the MOST fields — key sets share names
    (`total_tokens` exists in both the LangChain and OpenAI shapes), so the
    first partial match must not win over a complete one."""
    if not isinstance(raw, Mapping):
        return NO_USAGE
    best, best_hits = NO_USAGE, 0
    for in_key, out_key, total_key in _KEY_SETS:
        usage = Usage(_as_int(raw.get(in_key)), _as_int(raw.get(out_key)),
                      _as_int(raw.get(total_key)))
        hits = sum(v is not None for v in
                   (usage.input_tokens, usage.output_tokens, usage.total_tokens))
        if hits > best_hits:
            best, best_hits = usage, hits
    return best


def _from_container(container: Any) -> Usage:
    """A metadata dict that may hold usage under one of the container keys."""
    if not isinstance(container, Mapping):
        return NO_USAGE
    for key in _CONTAINER_KEYS:
        usage = _from_mapping(container.get(key))
        if usage.available:
            return usage
    return NO_USAGE


def _first_generation(response: Any) -> Any:
    generations = getattr(response, "generations", None)
    if not generations:
        return None
    first = generations[0]
    if isinstance(first, (list, tuple)):
        return first[0] if first else None
    return first


def extract_usage(response: Any) -> Usage:
    """Normalize usage from an `LLMResult`-like object. Never raises."""
    try:
        generation = _first_generation(response)
        message = getattr(generation, "message", None) if generation is not None else None
        if message is not None:
            usage = _from_mapping(getattr(message, "usage_metadata", None))
            if usage.available:
                return usage
            usage = _from_container(getattr(message, "response_metadata", None))
            if usage.available:
                return usage
        if generation is not None:
            usage = _from_container(getattr(generation, "generation_info", None))
            if usage.available:
                return usage
        return _from_container(getattr(response, "llm_output", None))
    except Exception:
        return NO_USAGE


def output_chars(response: Any) -> Optional[int]:
    """Length of the first generation's text — a size, never the text."""
    try:
        generation = _first_generation(response)
        text = getattr(generation, "text", None)
        return len(text) if isinstance(text, str) else None
    except Exception:
        return None


def messages_chars(messages: Any) -> Optional[int]:
    """Total character length of chat-model input messages (content only)."""
    try:
        total = 0
        for batch in messages or []:
            for message in batch or []:
                content = getattr(message, "content", None)
                if isinstance(content, str):
                    total += len(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, str):
                            total += len(block)
                        elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                            total += len(block["text"])
        return total
    except Exception:
        return None


def prompts_chars(prompts: Any) -> Optional[int]:
    try:
        return sum(len(p) for p in prompts or [] if isinstance(p, str))
    except Exception:
        return None

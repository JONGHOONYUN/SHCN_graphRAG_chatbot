"""LangChain callback instrumentation — one `llm.completed` per real model call.

측정 기준은 `.invoke()` 횟수가 아니라 LangChain ChatModel/LLM lifecycle이다.
`on_chat_model_start`/`on_llm_start` → `on_llm_end`/`on_llm_error` 한 쌍이 실제
provider 호출 1회다. chain 시작/종료 callback은 구현하지 않으므로 호출 수에
절대 포함되지 않는다. run_id 기준으로 한 번만 종료 처리하므로 중복 종료
callback이 와도 이중 집계되지 않는다.

목적(purpose) 판별은 prompt를 읽지 않는다. 각 LLM runnable에 생성 시점에
`with_llm_purpose(llm, purpose)`로 `metadata={"llm_purpose": ...}`를 묶어 두고,
callback은 그 제한된 값만 읽는다. purpose는 **leaf LLM에만** 묶는다 — LangChain은
부모 config의 metadata가 자식의 바인딩 값을 덮어쓰므로, chain에 purpose를 두면
하위 LLM의 목적이 가려진다.

`GraphCypherQAChain.from_llm`은 공개 파라미터 `cypher_llm`과 `qa_llm`을 따로
받으므로, 내부 두 LLM(Cypher 생성 / Graph QA)을 private API 없이 구분할 수 있다.

handler 주입은 LangChain 공개 API `register_configure_hook`을 쓴다. 요청 문맥이
열릴 때 ContextVar에 요청 전용 handler를 넣으면, 그 요청 안에서 구성되는 모든
callback manager에 handler가 (포인터 비교로 중복 없이) 자동 추가된다. 따라서
모든 invoke 경로에 callbacks 인자를 전달하도록 기존 코드를 바꿀 필요가 없다.

callback 내부 오류는 모두 흡수된다(`raise_error=False` + 자체 try/except) —
모델 결과에 영향을 주지 않는다.
"""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

from chatbot.observability import events as ev
from chatbot.observability import usage as usage_mod
from chatbot.observability.context import RequestContext
from chatbot.observability.emitter import emit, report_internal

PURPOSE_METADATA_KEY = "llm_purpose"


def with_llm_purpose(llm: Any, purpose: str) -> Any:
    """Bind an LLM purpose label to a LEAF language-model runnable.

    Returns a RunnableBinding around the same model object: model, temperature,
    retries and timeouts are untouched — only callback metadata is added."""
    return llm.with_config(
        metadata={PURPOSE_METADATA_KEY: ev.normalize_llm_purpose(purpose)})


@dataclass
class _Run:
    started: float
    purpose: str
    provider: Optional[str]
    model: Optional[str]
    input_chars: Optional[int]


def _model_from(metadata: dict, kwargs: dict) -> Optional[str]:
    name = metadata.get("ls_model_name")
    if isinstance(name, str) and name:
        return name
    params = kwargs.get("invocation_params")
    if isinstance(params, dict):
        for key in ("model", "model_name"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
    return None


class LLMTelemetryHandler(BaseCallbackHandler):
    """Request-bound handler. Holds a reference to its RequestContext, so a
    callback delivered on another thread still counts against the right
    request."""

    raise_error = False
    run_inline = True

    def __init__(self, ctx: RequestContext) -> None:
        super().__init__()
        self._ctx = ctx
        self._lock = threading.Lock()
        self._runs: dict = {}
        self._done: set = set()

    # -- lifecycle start ----------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id,
                            parent_run_id=None, tags=None, metadata=None,
                            **kwargs):
        self._start(run_id, metadata, kwargs, usage_mod.messages_chars(messages))

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None,
                     tags=None, metadata=None, **kwargs):
        self._start(run_id, metadata, kwargs, usage_mod.prompts_chars(prompts))

    # -- lifecycle end ------------------------------------------------------
    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        self._finish(run_id, response=response, error=None)

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._finish(run_id, response=None, error=error)

    # -- internals ----------------------------------------------------------
    def _start(self, run_id, metadata, kwargs, input_chars) -> None:
        try:
            key = str(run_id)
            md = metadata if isinstance(metadata, dict) else {}
            run = _Run(
                started=time.perf_counter(),
                purpose=ev.normalize_llm_purpose(md.get(PURPOSE_METADATA_KEY)),
                provider=md.get("ls_provider") if isinstance(md.get("ls_provider"), str) else None,
                model=_model_from(md, kwargs if isinstance(kwargs, dict) else {}),
                input_chars=input_chars,
            )
            with self._lock:
                if key in self._runs or key in self._done:
                    return
                self._runs[key] = run
        except Exception as exc:
            report_internal("callback", exc)

    def _finish(self, run_id, *, response, error) -> None:
        try:
            key = str(run_id)
            with self._lock:
                run = self._runs.pop(key, None)
                if run is None:
                    return          # duplicate end, or end without a start
                self._done.add(key)
            duration_ms = (time.perf_counter() - run.started) * 1000.0
            usage = usage_mod.extract_usage(response) if error is None else usage_mod.NO_USAGE
            self._ctx.record_llm_call(
                run.purpose, input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                usage_available=usage.available)
            fields = {
                "purpose": run.purpose,
                "provider": run.provider or "unknown",
                "model": run.model or "unknown",
                "status": ev.STATUS_SUCCESS if error is None else ev.STATUS_ERROR,
                "attempt_count": None,     # provider-internal retries are not observable
                "duration_ms": duration_ms,
                "usage_available": usage.available,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "input_chars": run.input_chars,
                "output_chars": usage_mod.output_chars(response) if error is None else None,
            }
            if error is not None:
                fields["error_type"] = type(error).__name__
            emit(ev.LLM, _ctx=self._ctx, **fields)
        except Exception as exc:
            report_internal("callback", exc)


_handler_var: ContextVar[Optional[LLMTelemetryHandler]] = ContextVar(
    "chatbot_observability_llm_handler", default=None)

# Public LangChain API: while `_handler_var` holds a handler, every callback
# manager configured in this context gets it (inheritable → child runs too).
register_configure_hook(_handler_var, True)


def install(ctx: RequestContext) -> Token:
    return _handler_var.set(LLMTelemetryHandler(ctx))


def uninstall(token: Token) -> None:
    _handler_var.reset(token)


def active_handler() -> Optional[LLMTelemetryHandler]:
    return _handler_var.get()

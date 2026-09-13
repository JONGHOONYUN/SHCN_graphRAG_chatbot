"""Retriever/fetcher invocation adapter — signature dispatch + user-safe status.

책임: retriever를 시그니처가 선언한 arity로 정확히 1회 호출하고, 실패를
correlation id 로그 + user-safe status로 변환. 함수 body의 TypeError를
arity mismatch로 오판하지 않는다 (실패 후 다른 arity 재시도 금지).
허용 의존성: 표준 라이브러리 + chatbot.domain.
외부 부작용: 주입된 callable 호출 + 경고 로그 (logger 이름은 기존
"tools.orchestrator" 유지 — assertLogs 및 운영 로그 연속성).
기존 facade: tools/orchestrator.py.

Moved verbatim from tools/orchestrator.py (modularization work order Phase 7.2).
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Callable, Optional

from chatbot.domain.evidence_models import Evidence

logger = logging.getLogger("tools.orchestrator")


# ── Signature-based arity dispatch (Phase 4) ────────────────────────────────
# The legacy pattern `try: fn(3-args) except TypeError: fn(2-args)` conflates
# two very different failures:
#   (a) the callable does not accept 3 positional args (compatibility);
#   (b) the callable's body raised a TypeError (a real defect).
# Case (b) was being silently swallowed and the callable retried with 2 args
# — masking bugs AND double-calling network-touching mocks.
#
# The new dispatch decides arity BEFORE the call using `inspect.signature`.
# A TypeError from the callable's BODY is now surfaced as a retrieval failure
# with a correlation id, never as an arity-retry signal.


def _fn_accepts_arity(fn: Callable, target_arity: int) -> bool:
    """True iff `fn` can be called with exactly `target_arity` positional args.

    A callable that declares `*args` (e.g. MagicMock, most decorators) is
    reported as compatible with any arity. If signature introspection fails
    (some C-level callables), we return True — a genuine mismatch will
    surface as TypeError from the body, which is treated as retrieval
    failure (not retried)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    required = sum(1 for p in positional
                   if p.default is inspect.Parameter.empty)
    return required <= target_arity <= len(positional)


def _safe_retrieve(fn: Callable, question: str, language: str,
                   history_text: Optional[str], kind: str) -> tuple:
    """Run one retriever with the arity its signature actually declares.

    Signature dispatch order (Phase 4):
      1. `fn(question, language, history_text)` — canonical 3-arg
      2. `fn(question, language)` — legacy 2-arg (kept for existing tests)

    A TypeError raised from inside the callable is NOT interpreted as an
    arity mismatch — it becomes a retrieval failure with a correlation id.
    """
    if _fn_accepts_arity(fn, 3):
        args: tuple = (question, language, history_text)
    elif _fn_accepts_arity(fn, 2):
        args = (question, language)
    else:
        code = uuid.uuid4().hex[:8]
        logger.warning(
            "%s retriever signature incompatible [%s]", kind, code)
        return Evidence(kind=kind), {"source": kind,
                                     "outcome": "temporarily_unavailable"}
    try:
        ev = fn(*args)
    except Exception as e:
        code = uuid.uuid4().hex[:8]
        logger.warning("%s retrieval failed [%s]: %s: %s",
                       kind, code, type(e).__name__, e)
        return Evidence(kind=kind), {"source": kind,
                                     "outcome": "temporarily_unavailable"}
    ev = ev or Evidence(kind=kind)
    return _normalize_evidence_status(ev, kind)


def _normalize_evidence_status(ev: Evidence, kind: str) -> tuple:
    """Strip technical error claims out of Evidence (logging them instead) and
    derive the user-safe outcome. 'no_results' is NOT an error state."""
    outcome = None
    kept = []
    for claim in ev.claims or []:
        ctype = claim.get("type") if isinstance(claim, dict) else None
        if ctype == "error":
            # Legacy shape carrying raw exception text — log-only, never kept.
            code = uuid.uuid4().hex[:8]
            logger.warning("%s retrieval error claim [%s]: %s",
                           kind, code, claim.get("message"))
            outcome = outcome or "temporarily_unavailable"
            continue
        if ctype == "status":
            if claim.get("outcome") in ("temporarily_unavailable",
                                        "invalid_query", "no_results"):
                outcome = claim.get("outcome")
            continue                        # status claims are never rendered
        kept.append(claim)
    ev.claims = kept
    if outcome is None:
        outcome = "ok" if ev.documents else "no_results"
    return ev, {"source": kind, "outcome": outcome}


def _call_fetcher(fetcher: Callable, source: str, ext_id: str, language: str,
                  node_type: str) -> dict:
    """Call the injected fetcher with signature-appropriate arity.

    Introspection-based (Phase 4): a TypeError raised inside the fetcher is
    NOT interpreted as an arity mismatch — it propagates so the retriever
    layer can classify it as a real fetch failure. This prevents side-
    effectful fetchers (network calls, counters) from being invoked twice
    when their body raised for an unrelated reason."""
    if _fn_accepts_arity(fetcher, 4):
        return fetcher(source, ext_id, language, node_type)
    if _fn_accepts_arity(fetcher, 3):
        return fetcher(source, ext_id, language)
    raise TypeError(
        "authority fetcher signature incompatible — expected 3 or 4 "
        "positional arguments")

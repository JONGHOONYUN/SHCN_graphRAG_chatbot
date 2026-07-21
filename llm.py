"""LLM and embedding client construction.

Phase 5 hardening:
  * The embedding model is pinned via `GOOGLE_EMBEDDING_MODEL` in secrets.
    Auto-discovery is NOT run in the request path — it belongs to a
    one-shot admin task and is opt-in via `discover_available_model()`.
  * HTTP calls use bounded exponential backoff with jitter, honour the
    server's `Retry-After` header for 429/503, and never retry 4xx auth
    errors. The old 60-second fixed sleep is gone — a live user session is
    never blocked that long.
  * Every response is validated (HTTP status, JSON schema, numeric vector,
    expected length) before any value reaches Neo4j / the retriever.
  * `embed_documents([])` returns `[]` with ZERO network calls.

Module import performs NO network I/O; the Gemini and embedding clients are
created only when their factories are called (Phase 2 auth-gated init).
"""

from __future__ import annotations

import logging
import random
import time
from typing import List, Optional, Sequence

import requests
import streamlit as st
from langchain_core.embeddings import Embeddings
from langchain_google_genai import ChatGoogleGenerativeAI

from errors import ConfigurationError, ModelResponseError, TransientProviderError

logger = logging.getLogger(__name__)


# ── Chat LLM ────────────────────────────────────────────────────────────────
# `safety_settings` remains removed: langchain-google-genai 4.2.1 rejects the
# format we previously used with a pydantic ValidationError. The academic-
# text false-positive `No generation chunks were returned` is now handled by
# the top-level fallback policy in `agent.generate_response` (Phase 3).
llm = ChatGoogleGenerativeAI(
    google_api_key=st.secrets["GOOGLE_API_KEY"],
    model=st.secrets["GOOGLE_MODEL"],
    temperature=0,
    convert_system_message_to_human=True,
)


# ── HTTP retry policy ───────────────────────────────────────────────────────
_MAX_RETRIES = 3                     # total attempts INCLUDING the first
_BASE_BACKOFF_SECONDS = 1.0          # exp base — bounded, not the old 60s
_MAX_BACKOFF_SECONDS = 8.0           # cap so live sessions stay responsive
_REQUEST_TIMEOUT_SECONDS = (5, 30)   # (connect, read)

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


# Sane default embedding model. Chosen at import when `GOOGLE_EMBEDDING_MODEL`
# is unset — no network probing. Explicit configuration always takes priority
# and is strongly recommended (see secrets.toml.example).
_DEFAULT_EMBEDDING_MODEL = "models/gemini-embedding-001"


def _sleep_before_retry(response: Optional[requests.Response],
                        attempt: int) -> None:
    """Honour Retry-After when present; otherwise bounded exp backoff+jitter.

    `time.sleep` and `random.random` are looked up at call time (module
    attribute access, not default-argument capture) so tests can patch
    `llm.time.sleep` reliably."""
    delay: Optional[float] = None
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                delay = float(raw)
            except (TypeError, ValueError):
                delay = None
    if delay is None:
        delay = min(_MAX_BACKOFF_SECONDS,
                    _BASE_BACKOFF_SECONDS * (2 ** attempt))
        delay += random.random() * 0.5   # jitter
    time.sleep(delay)


class GoogleEmbeddings(Embeddings):
    """LangChain-compatible embedding client for Google's REST API v1.

    Model pinning: `model` is required. If not passed, it is read from
    `st.secrets["GOOGLE_EMBEDDING_MODEL"]`. If neither is set, a
    `ConfigurationError` is raised — the client refuses to auto-discover in
    the request path.

    Dimension pinning: `expected_dim`, if supplied, is checked on every
    response so a silent server-side model swap can never poison the vector
    index. When absent, the first successful response initializes it.
    """

    def __init__(self, api_key: str, model: Optional[str] = None,
                 expected_dim: Optional[int] = None,
                 session: Optional[requests.Session] = None) -> None:
        if not api_key:
            raise ConfigurationError("GOOGLE_API_KEY is empty")
        if model is None:
            # Prefer an explicit secret; fall back to the pinned default so a
            # fresh deployment does not break before the operator sets the
            # secret. This is a NAMING default — no network probing runs.
            try:
                model = st.secrets["GOOGLE_EMBEDDING_MODEL"]
            except Exception:
                logger.info(
                    "GOOGLE_EMBEDDING_MODEL not in secrets; using default %s",
                    _DEFAULT_EMBEDDING_MODEL,
                )
                model = _DEFAULT_EMBEDDING_MODEL
        if not model:
            raise ConfigurationError("embedding model name is empty")
        self.api_key = api_key
        self.model = model
        self.expected_dim = expected_dim
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        self.embed_url = f"{self.base_url}/{model}:embedContent"
        self.batch_url = f"{self.base_url}/{model}:batchEmbedContents"
        self._session = session or requests.Session()

    # ── Public API ─────────────────────────────────────────────────────────
    def embed_query(self, text: str) -> List[float]:
        """Embed a single query. Returns a list[float] of length
        `expected_dim` (or whatever the model returned on first successful
        response)."""
        response_json = self._post_with_retry(
            self.embed_url,
            {"model": self.model, "content": {"parts": [{"text": text}]}},
        )
        vector = self._extract_single_vector(response_json)
        self._check_dimension([vector])
        return vector

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        """Batch-embed. Returns `[]` with NO network call when `texts` is
        empty — the old code called the API with an empty array and could
        fail 400 or return an unexpected shape."""
        if not texts:
            return []
        payload = {
            "requests": [
                {"model": self.model, "content": {"parts": [{"text": t}]}}
                for t in texts
            ]
        }
        response_json = self._post_with_retry(self.batch_url, payload)
        vectors = self._extract_batch_vectors(response_json, len(texts))
        self._check_dimension(vectors)
        return vectors

    # ── HTTP + retry ───────────────────────────────────────────────────────
    def _post_with_retry(self, url: str, payload: dict) -> dict:
        """POST with bounded retry.

        Retryable: 429 + 5xx (subset above).
        Non-retryable: 4xx auth/validation errors (400/401/403/404), any
        response body that fails validation (invalid JSON / wrong content
        type / no numeric vector).
        """
        last_status: Optional[int] = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._session.post(
                    url,
                    params={"key": self.api_key},
                    json=payload,
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.Timeout as exc:
                logger.warning(
                    "embedding request timed out (attempt %s/%s)",
                    attempt + 1, _MAX_RETRIES,
                )
                if attempt == _MAX_RETRIES - 1:
                    raise TransientProviderError(
                        "embedding provider timed out") from exc
                _sleep_before_retry(None, attempt)
                continue
            except requests.RequestException as exc:
                # Non-timeout connection failure — bounded retry.
                logger.warning(
                    "embedding transport error (attempt %s/%s): %s",
                    attempt + 1, _MAX_RETRIES, type(exc).__name__,
                )
                if attempt == _MAX_RETRIES - 1:
                    raise TransientProviderError(
                        f"embedding transport failure: "
                        f"{type(exc).__name__}") from exc
                _sleep_before_retry(None, attempt)
                continue

            last_status = response.status_code
            if response.status_code in _RETRYABLE_STATUSES:
                if attempt == _MAX_RETRIES - 1:
                    raise TransientProviderError(
                        f"embedding retry budget exhausted "
                        f"(last status: {last_status})"
                    )
                _sleep_before_retry(response, attempt)
                continue
            if 400 <= response.status_code < 500:
                # 4xx: auth / validation / not-found — retrying is pointless
                # and only leaks attempts. Surface as ConfigurationError so
                # the operator fixes the key/model rather than the caller
                # retrying blindly.
                raise ConfigurationError(
                    f"embedding provider rejected the request "
                    f"(HTTP {response.status_code})"
                )
            response.raise_for_status()

            return self._parse_response(response)
        raise TransientProviderError(
            f"embedding request failed after {_MAX_RETRIES} attempts")

    # ── Response validation ────────────────────────────────────────────────
    @staticmethod
    def _parse_response(response: requests.Response) -> dict:
        """Return the parsed JSON body, or raise on malformed content."""
        ctype = response.headers.get("content-type", "").lower()
        if "application/json" not in ctype:
            raise ModelResponseError(
                f"unexpected embedding content-type: {ctype!r}")
        try:
            return response.json()
        except ValueError as exc:
            raise ModelResponseError(
                "embedding response body is not valid JSON") from exc

    @staticmethod
    def _extract_single_vector(response_json: dict) -> List[float]:
        embedding = response_json.get("embedding")
        if not isinstance(embedding, dict):
            raise ModelResponseError(
                "embedding response missing 'embedding' object")
        values = embedding.get("values")
        return _validate_vector(values)

    @staticmethod
    def _extract_batch_vectors(response_json: dict, expected_count: int
                               ) -> List[List[float]]:
        items = response_json.get("embeddings")
        if not isinstance(items, list):
            raise ModelResponseError(
                "batch embedding response missing 'embeddings' list")
        if len(items) != expected_count:
            raise ModelResponseError(
                f"batch embedding count mismatch: sent {expected_count}, "
                f"got {len(items)}"
            )
        return [_validate_vector(item.get("values")) for item in items]

    def _check_dimension(self, vectors: List[List[float]]) -> None:
        """Enforce dimension consistency across calls.

        If `expected_dim` was set at init, every vector must match it.
        Otherwise, the first vector we see pins the dimension for later
        calls (so an accidental model swap surfaces as an assertion, not as
        silently poisoned index writes)."""
        for v in vectors:
            if self.expected_dim is None:
                self.expected_dim = len(v)
                continue
            if len(v) != self.expected_dim:
                raise ConfigurationError(
                    f"embedding dimension mismatch: expected "
                    f"{self.expected_dim}, got {len(v)} — this usually means "
                    "the pinned model changed. Reindex before using it."
                )


def _validate_vector(values) -> List[float]:
    if not isinstance(values, list) or not values:
        raise ModelResponseError(
            "embedding vector is empty or malformed")
    for x in values:
        if not isinstance(x, (int, float)):
            raise ModelResponseError(
                "embedding vector contains non-numeric entry")
    return list(values)


# ── Admin-only: opt-in auto-discovery ───────────────────────────────────────
# Kept OUT of the request path (Phase 5 §2). Import and call this ONLY from a
# management CLI when picking a new model name; never from Streamlit.
_EMBEDDING_MODEL_CANDIDATES = (
    "models/gemini-embedding-001",
    "models/gemini-embedding-2-preview",
)


def discover_available_model(api_key: str,
                             candidates: Sequence[str] = _EMBEDDING_MODEL_CANDIDATES,
                             ) -> str:
    """Probe each candidate model with a tiny request; return the first one
    that responds 200. Admin/one-shot only — never invoked from Streamlit."""
    base = "https://generativelanguage.googleapis.com/v1beta"
    payload = {"content": {"parts": [{"text": "probe"}]}}
    for model in candidates:
        payload["model"] = model
        try:
            r = requests.post(f"{base}/{model}:embedContent",
                              params={"key": api_key}, json=payload,
                              timeout=(5, 15))
            if r.status_code == 200:
                return model
        except requests.RequestException:
            continue
    raise ConfigurationError(
        f"no embedding model candidate is available: {list(candidates)}")


# ── Module-level embedding client ───────────────────────────────────────────
# Constructed at import time INTENTIONALLY: this module is itself only imported
# after authentication succeeds (bot.py Phase 2 lazy import), so no
# unauthenticated user triggers this.
_embedding_expected_dim: Optional[int] = None
try:
    _embedding_expected_dim = int(st.secrets["EMBEDDING_DIMENSION"])
except Exception:
    _embedding_expected_dim = None

embeddings = GoogleEmbeddings(
    api_key=st.secrets["GOOGLE_API_KEY"],
    model=st.secrets.get("GOOGLE_EMBEDDING_MODEL"),
    expected_dim=_embedding_expected_dim,
)

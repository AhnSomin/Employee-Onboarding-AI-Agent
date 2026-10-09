"""Embeddings for regulation chunks and questions (instruction v2, section 6).

The embedding settings are separate from the generation model chain. Every
vector is L2-normalised before it is stored, whatever the dimension. A cache
keyed by (model, dimension, task type, input format, text hash) means the
same chunk or question is never embedded twice, across runs.

`GeminiEmbedder` sends documents in small batches (one embedding per input
for gemini-embedding-001) and checks each response: as many vectors as
inputs, the right dimension, no NaN and no zero vector. A batch that fails
the check is thrown away and the build fails; nothing partial is stored.
`HashEmbedder` is a deterministic offline stand-in for tests.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

INPUT_FORMAT = "embed_text v1: [법령명] 제n조(제목) [제m항] 헤더 + search_text"
DOC_TASK = "RETRIEVAL_DOCUMENT"
BATCH_SIZE = 32  # the API documents no per-request input count; stay well inside the old 100 limit


class EmbeddingError(RuntimeError):
    """The embedding call failed or returned unusable vectors."""


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str
    dim: int
    query_task: str
    doc_task: str = DOC_TASK
    input_format: str = INPUT_FORMAT
    normalization: str = "L2 (float32)"

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dim": self.dim,
            "doc_task": self.doc_task,
            "query_task": self.query_task,
            "input_format": self.input_format,
            "normalization": self.normalization,
        }


class Embedder(Protocol):
    config: EmbeddingConfig

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0) or not np.all(np.isfinite(vectors)):
        raise EmbeddingError("0벡터 또는 NaN이 있는 임베딩입니다.")
    return (vectors / norms).astype(np.float32)


def check_batch(vectors: list[list[float]], expected: int, dim: int) -> np.ndarray:
    """Vectors for one request, or EmbeddingError when count, dimension or values are off."""
    if len(vectors) != expected:
        raise EmbeddingError(f"입력 {expected}개에 벡터 {len(vectors)}개가 왔습니다(합쳐졌거나 빠짐).")
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim != 2 or array.shape[1] != dim:
        raise EmbeddingError(f"벡터 차원이 {dim}이 아닙니다: {array.shape}")
    return normalize_rows(array)


class EmbeddingCache:
    """One .npy file per (config, task, text) key under `root`."""

    def __init__(self, root: Path, config: EmbeddingConfig) -> None:
        self.root = root
        self.config = config

    def key(self, task: str, text: str) -> str:
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        raw = "|".join([self.config.model, str(self.config.dim), task, self.config.input_format, text_hash])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _path(self, task: str, text: str) -> Path:
        key = self.key(task, text)
        return self.root / task.lower() / key[:2] / f"{key}.npy"

    def get(self, task: str, text: str) -> np.ndarray | None:
        path = self._path(task, text)
        return np.load(path) if path.is_file() else None

    def put(self, task: str, text: str, vector: np.ndarray) -> None:
        path = self._path(task, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npy")
        np.save(tmp, vector.astype(np.float32))
        tmp.replace(path)


class GeminiEmbedder:
    """Embeds through an object with `models.embed_content` (normally a CountingGenai)."""

    def __init__(self, client: Any, config: EmbeddingConfig, cache: EmbeddingCache) -> None:
        self.client = client
        self.config = config
        self.cache = cache
        self.requests = 0
        self.embedded = 0
        self.cached = 0
        self.last_response_shape: dict[str, Any] | None = None
        self.query_purpose = "query"  # "query:<use>", e.g. query:dev_eval

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        out: list[np.ndarray | None] = [self.cache.get(self.config.doc_task, t) for t in texts]
        self.cached += sum(v is not None for v in out)
        missing = [i for i, v in enumerate(out) if v is None]
        for start in range(0, len(missing), BATCH_SIZE):
            batch = missing[start : start + BATCH_SIZE]
            vectors = self._call([texts[i] for i in batch], self.config.doc_task, purpose="document")
            for i, vector in zip(batch, vectors, strict=True):
                self.cache.put(self.config.doc_task, texts[i], vector)
                out[i] = vector
            self.embedded += len(batch)
        return np.vstack(out) if out else np.zeros((0, self.config.dim), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        cached = self.cache.get(self.config.query_task, text)
        if cached is not None:
            self.cached += 1
            return cached
        vector = self._call([text], self.config.query_task, purpose="query")[0]
        self.cache.put(self.config.query_task, text, vector)
        return vector

    def _call(self, texts: list[str], task: str, purpose: str) -> np.ndarray:
        from google.genai import types

        label = "document" if purpose == "document" else self.query_purpose
        previous = getattr(self.client, "purpose", None)
        if previous is not None:
            self.client.purpose = label  # the counting client files the call under this label
        try:
            response = self.client.models.embed_content(
                model=self.config.model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.config.dim),
            )
        except Exception as exc:
            raise EmbeddingError(f"임베딩 호출 실패: {_describe(exc)}") from exc
        finally:
            if previous is not None:
                self.client.purpose = previous
        self.requests += 1
        embeddings = list(response.embeddings or [])
        self.last_response_shape = response_shape(response)
        return check_batch([list(e.values or []) for e in embeddings], len(texts), self.config.dim)


def response_shape(response: Any) -> dict[str, Any]:
    """The response structure without the vectors, for the fixture (no secrets, no full values)."""
    dumped = response.model_dump(mode="json", exclude_none=True) if hasattr(response, "model_dump") else {}
    for embedding in dumped.get("embeddings", []):
        values = embedding.pop("values", [])
        embedding["values_len"] = len(values)
        embedding["values_head"] = [round(v, 4) for v in values[:3]]
    dumped.pop("sdk_http_response", None)
    return dumped


def _describe(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return f"{code} {getattr(exc, 'status', '') or ''}".strip() if code else type(exc).__name__


class HashEmbedder:
    """Deterministic bag-of-words vectors. Similar wording gives similar vectors; offline only."""

    def __init__(self, dim: int = 256, model: str = "hash-embedding-test") -> None:
        self.config = EmbeddingConfig(model=model, dim=dim, query_task="TEST_QUERY", doc_task="TEST_DOC")
        self.document_calls = 0
        self.query_calls = 0

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.config.dim, dtype=np.float32)
        words = re.findall(r"[가-힣A-Za-z0-9]+", text)
        grams = words + [w[i : i + 2] for w in words for i in range(len(w) - 1)]
        for gram in grams or ["empty"]:
            digest = hashlib.md5(gram.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "little") % self.config.dim] += 1.0
        return normalize_rows(vector[None, :])[0]

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        self.document_calls += 1
        return np.vstack([self._vector(t) for t in texts]) if texts else np.zeros((0, self.config.dim), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        self.query_calls += 1
        return self._vector(text)

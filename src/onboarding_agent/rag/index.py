"""Versioned regulation index on disk: chunks, normalised vectors and a manifest.

Layout under REG_INDEX_DIR (git-ignored):
    CURRENT                 name of the version in use
    <version>/chunks.jsonl  one RegChunk per line, same order as the vectors
    <version>/docs.jsonl    one SourceDoc per line
    <version>/embeddings.npy float32, L2-normalised
    <version>/manifest.json
    cache/                  embedding cache (see embed.EmbeddingCache)

A build writes a new version into a temporary folder, checks it (ids unique,
one vector per chunk, the configured dimension, finite unit vectors), renames
it into place and only then switches CURRENT. A failed build leaves the
current index untouched. Unchanged chunks come from the embedding cache, so
a rebuild of the same sources makes no embedding call. BM25 state is rebuilt
from the chunks when the index loads, so it is not stored.

Loading refuses an index whose embedding settings differ from the
configured ones (model, dimension, task types, input format): vectors of
different models are never mixed. The caller reports source_unavailable.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .embed import Embedder, EmbeddingConfig
from .models import RegChunk, SourceDoc

FORMAT = 1


class IndexUnavailable(RuntimeError):
    """No usable index: missing, half-built, or built with other embedding settings."""


@dataclass
class LoadedIndex:
    version: str
    manifest: dict
    chunks: list[RegChunk]
    docs: dict[str, SourceDoc]
    vectors: np.ndarray

    def chunk(self, chunk_id: str) -> RegChunk | None:
        return self._by_id.get(chunk_id)

    def position(self, chunk_id: str) -> int | None:
        return self._positions.get(chunk_id)

    def __post_init__(self) -> None:
        self._by_id = {c.chunk_id: c for c in self.chunks}
        self._positions = {c.chunk_id: i for i, c in enumerate(self.chunks)}


def build_index(
    root: Path,
    docs: list[SourceDoc],
    chunks: list[RegChunk],
    embedder: Embedder,
    *,
    excluded: list[dict],
    unindexed: list[dict],
    usage: dict | None = None,
    now: datetime | None = None,
) -> str:
    """Embed (through the cache), write and verify a new version, then point CURRENT at it."""
    now = now or datetime.now().astimezone()
    ids = [c.chunk_id for c in chunks]
    if len(set(ids)) != len(ids):
        raise ValueError("chunk_id가 중복됩니다.")
    vectors = embedder.embed_documents([c.embed_text for c in chunks])
    _check_vectors(vectors, len(chunks), embedder.config.dim)

    version = now.strftime("v%Y%m%dT%H%M%S")
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".tmp-{version}"
    final = root / version
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        _write_jsonl(tmp / "chunks.jsonl", [c.model_dump(mode="json") for c in chunks])
        _write_jsonl(tmp / "docs.jsonl", [d.model_dump(mode="json") for d in docs])
        np.save(tmp / "embeddings.npy", vectors.astype(np.float32))
        manifest = {
            "format": FORMAT,
            "index_version": version,
            "created_at": now.isoformat(timespec="seconds"),
            "search": "NumPy 벡터 검색(정규화 벡터 내적 = 코사인) + BM25(kiwipiepy), 별도 벡터 DB 없음",
            "embedding": embedder.config.as_dict(),
            "chunk_count": len(chunks),
            "documents": [
                {
                    "doc_id": d.doc_id,
                    "title": d.doc_title,
                    "version_label": d.version_label,
                    "effective_date": d.effective_date.isoformat() if d.effective_date else None,
                    "promulgation_date": d.promulgation_date.isoformat() if d.promulgation_date else None,
                    "source_sha256": d.source_sha256,
                    "chunks": sum(c.doc_id == d.doc_id for c in chunks),
                    "unverified": d.unverified,
                }
                for d in docs
            ],
            "excluded": excluded,
            "unindexed": unindexed,
            "usage": usage or {},
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        load_version(tmp, embedder.config)  # the new version must load before it can become CURRENT
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    pointer = root / "CURRENT.tmp"
    pointer.write_text(version, encoding="utf-8")
    pointer.replace(root / "CURRENT")
    return version


def load_index(root: Path, config: EmbeddingConfig) -> LoadedIndex:
    current = root / "CURRENT"
    if not current.is_file():
        raise IndexUnavailable("색인이 아직 없습니다.")
    return load_version(root / current.read_text(encoding="utf-8").strip(), config)


def load_version(folder: Path, config: EmbeddingConfig) -> LoadedIndex:
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        chunks = [RegChunk.model_validate_json(line) for line in _lines(folder / "chunks.jsonl")]
        docs = {d.doc_id: d for d in (SourceDoc.model_validate_json(line) for line in _lines(folder / "docs.jsonl"))}
        vectors = np.load(folder / "embeddings.npy")
    except (OSError, ValueError) as exc:
        raise IndexUnavailable(f"색인 파일을 읽지 못했습니다: {type(exc).__name__}") from None
    stored = manifest.get("embedding", {})
    wanted = config.as_dict()
    differences = [
        key for key in ("model", "dim", "doc_task", "query_task", "input_format") if stored.get(key) != wanted[key]
    ]
    if differences:
        raise IndexUnavailable("색인의 임베딩 설정이 지금 설정과 다릅니다: " + ", ".join(differences))
    if [c.chunk_id for c in chunks] and manifest.get("chunk_count") != len(chunks):
        raise IndexUnavailable("manifest의 청크 수와 실제 청크 수가 다릅니다.")
    _check_vectors(vectors, len(chunks), config.dim)
    if any(c.doc_id not in docs for c in chunks):
        raise IndexUnavailable("문서 정보가 없는 청크가 있습니다.")
    return LoadedIndex(manifest.get("index_version", folder.name), manifest, chunks, docs, vectors)


def _check_vectors(vectors: np.ndarray, count: int, dim: int) -> None:
    if vectors.shape != (count, dim):
        raise IndexUnavailable(f"벡터 모양 {vectors.shape}이 청크 {count}개 × 차원 {dim}과 다릅니다.")
    if count and (not np.all(np.isfinite(vectors)) or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-3)):
        raise IndexUnavailable("정규화되지 않았거나 NaN이 있는 벡터가 있습니다.")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

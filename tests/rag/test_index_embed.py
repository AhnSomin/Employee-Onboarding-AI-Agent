"""Index build, embedding checks, cache and budget (instruction v2, sections 3, 6 and 11)."""

import json
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from onboarding_agent.rag.embed import (
    EmbeddingCache,
    EmbeddingConfig,
    EmbeddingError,
    GeminiEmbedder,
    HashEmbedder,
    check_batch,
)
from onboarding_agent.rag.index import IndexUnavailable, build_index, load_index
from onboarding_agent.rag.usage import BudgetExceeded, CountingGenai, UsageMeter

from .conftest import LAW, RULE, docs_and_chunks, make_index


class FakeEmbedApi:
    """Stands in for google.genai: one vector per input, unless told to merge or drop."""

    def __init__(self, dim=8, merge=False, fail_code=None):
        self.dim, self.merge, self.fail_code = dim, merge, fail_code
        self.calls: list[list[str]] = []
        self.models = self

    def embed_content(self, *, model, contents, config=None):
        self.calls.append(list(contents))
        if self.fail_code:
            raise SimpleNamespaceError(self.fail_code)
        texts = contents[:1] if self.merge else contents
        vectors = [[(hash(t) % 97 + 1) / 100.0 + i * 0.01 for i in range(self.dim)] for t in texts]
        return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in vectors])


class SimpleNamespaceError(Exception):
    def __init__(self, code):
        super().__init__(f"error {code}")
        self.code = code


def config(dim=8):
    return EmbeddingConfig(model="gemini-embedding-001", dim=dim, query_task="QUESTION_ANSWERING")


def test_chunks_and_vectors_line_up_and_survive_restart(index_dir):
    index, embedder = make_index(index_dir)
    assert index.vectors.shape == (len(index.chunks), embedder.config.dim)
    assert np.allclose(np.linalg.norm(index.vectors, axis=1), 1.0)
    for position, chunk in enumerate(index.chunks):
        assert np.allclose(index.vectors[position], embedder.embed_documents([chunk.embed_text])[0])
    again = load_index(index_dir, embedder.config)  # a new process reads the same version
    assert again.version == index.version and [c.chunk_id for c in again.chunks] == [c.chunk_id for c in index.chunks]
    manifest = again.manifest
    assert manifest["embedding"]["model"] == embedder.config.model
    assert {d["doc_id"] for d in manifest["documents"]} == {"dec36728", "pmo2161"}


def test_merged_or_missing_vectors_are_rejected():
    with pytest.raises(EmbeddingError):
        check_batch([[1.0, 0.0]], expected=2, dim=2)  # two inputs came back as one vector
    with pytest.raises(EmbeddingError):
        check_batch([[1.0, 0.0, 0.0]], expected=1, dim=2)
    with pytest.raises(EmbeddingError):
        check_batch([[0.0, 0.0]], expected=1, dim=2)
    with pytest.raises(EmbeddingError):
        check_batch([[float("nan"), 1.0]], expected=1, dim=2)
    api = FakeEmbedApi(merge=True)
    embedder = GeminiEmbedder(api, config(), EmbeddingCache_tmp())
    with pytest.raises(EmbeddingError):
        embedder.embed_documents(["a", "b"])


def EmbeddingCache_tmp():  # noqa: N802 - small helper
    import tempfile
    from pathlib import Path

    return EmbeddingCache(Path(tempfile.mkdtemp()), config())


def test_rebuild_of_same_sources_embeds_nothing_and_changed_docs_drop_old_chunks(tmp_path):
    api = FakeEmbedApi()
    cache = EmbeddingCache(tmp_path / "cache", config())
    root = tmp_path / "index"
    docs, chunks = docs_and_chunks()
    first = GeminiEmbedder(api, config(), cache)
    build_index(root, docs, chunks, first, excluded=[], unindexed=[], now=datetime(2026, 10, 10, 9, 0, 0))
    assert first.embedded == len(chunks) and len(api.calls) >= 1

    calls_before = len(api.calls)
    second = GeminiEmbedder(api, config(), cache)
    build_index(root, docs, chunks, second, excluded=[], unindexed=[], now=datetime(2026, 10, 10, 9, 0, 1))
    assert len(api.calls) == calls_before and second.embedded == 0 and second.cached == len(chunks)

    changed = LAW.replace("연 60일의 범위에서", "연 50일의 범위에서")
    docs2, chunks2 = docs_and_chunks((changed, RULE))
    third = GeminiEmbedder(api, config(), cache)
    build_index(root, docs2, chunks2, third, excluded=[], unindexed=[], now=datetime(2026, 10, 10, 9, 0, 2))
    assert third.embedded == 1  # only the changed article
    docs3, chunks3 = docs_and_chunks((changed,))  # the rule document was removed
    fourth = GeminiEmbedder(api, config(), cache)
    build_index(root, docs3, chunks3, fourth, excluded=[], unindexed=[], now=datetime(2026, 10, 10, 9, 0, 3))
    current = load_index(root, config())
    assert not any(c.doc_id == "pmo2161" for c in current.chunks)
    assert "연 50일" in current.chunk("dec36728:a5").search_text


def test_failed_build_keeps_current(tmp_path):
    root = tmp_path / "index"
    make_index(root)
    current = (root / "CURRENT").read_text()
    docs, chunks = docs_and_chunks()
    broken = GeminiEmbedder(FakeEmbedApi(merge=True), config(), EmbeddingCache(tmp_path / "c2", config()))
    with pytest.raises(EmbeddingError):
        build_index(root, docs, chunks, broken, excluded=[], unindexed=[])
    assert (root / "CURRENT").read_text() == current
    assert not any(p.name.startswith(".tmp-") for p in root.iterdir())


def test_index_with_other_embedding_settings_is_refused(index_dir):
    make_index(index_dir)
    with pytest.raises(IndexUnavailable):
        load_index(index_dir, HashEmbedder(dim=128).config)
    with pytest.raises(IndexUnavailable):
        load_index(index_dir.parent / "nothing", HashEmbedder().config)


def test_budget_caps_purposes_and_429_halt(tmp_path):
    meter = UsageMeter(tmp_path / "usage.jsonl", caps={"generate": 3, "embed_query": 1, "embed_doc_tokens": 10})
    api = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: "ok"))
    counter = CountingGenai(api, meter, purpose="app_question")
    assert counter.models.generate_content(model="m", contents="q") == "ok"
    with pytest.raises(BudgetExceeded):  # the app's share is one request
        counter.models.generate_content(model="m", contents="q")
    counter.purpose = "spare"
    counter.models.generate_content(model="m", contents="q")
    assert meter.totals()["generate"] == 2 and meter.by_purpose() == {"app_question": 1, "spare": 1}

    failing = FakeEmbedApi(fail_code=429)
    query_counter = CountingGenai(failing, meter, purpose="query:test")
    with pytest.raises(SimpleNamespaceError):
        query_counter.models.embed_content(model="m", contents=["q"])
    with pytest.raises(BudgetExceeded):  # halted after the quota error, no second request
        query_counter.models.embed_content(model="m", contents=["q"])
    assert len(failing.calls) == 1
    doc_counter = CountingGenai(FakeEmbedApi(), meter, purpose="document")
    with pytest.raises(BudgetExceeded):
        doc_counter.models.embed_content(model="m", contents=["01234567890"])  # 11 estimated tokens > 10
    records = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [r["status"] for r in records] == ["ok", "ok", "quota"]


def test_budget_file_session_and_unenforced_calls_are_still_logged(tmp_path):
    log, budget = tmp_path / "usage.jsonl", tmp_path / "budget.yaml"
    log.write_text(
        json.dumps({"at": "2026-10-09T10:00:00+09:00", "kind": "generate", "purpose": "spare", "model": "m",
                    "requests": 5, "est_tokens": 0, "status": "ok"}) + "\n"
    )
    budget.write_text(
        'since: "2026-10-10T02:03:00+09:00"\ncaps: {generate: 1, embed_query: 1, embed_doc_tokens: 1}\n'
        "generate_plan: {app_question: 1}\n"
    )
    meter = UsageMeter.from_budget_file(log, budget, enforce=False)
    counter = CountingGenai(SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: "ok")), meter,
                            purpose="app_question")
    for _ in range(3):  # over the cap and the share, but not enforced
        counter.models.generate_content(model="m", contents="q")
    assert meter.totals()["generate"] == 3  # this budget session only
    assert meter.totals(lifetime=True)["generate"] == 8  # the older record counts in the lifetime view
    enforced = UsageMeter.from_budget_file(log, budget, enforce=True)
    with pytest.raises(BudgetExceeded):
        enforced.check("generate", purpose="app_question")


def test_query_embeddings_are_cached(tmp_path):
    api = FakeEmbedApi()
    embedder = GeminiEmbedder(api, config(), EmbeddingCache(tmp_path / "cache", config()))
    first = embedder.embed_query("연가는 며칠인가요?")
    second = GeminiEmbedder(api, config(), EmbeddingCache(tmp_path / "cache", config())).embed_query(
        "연가는 며칠인가요?"
    )
    assert np.allclose(first, second) and len(api.calls) == 1


def test_recorded_response_shape_is_one_vector_per_input():
    """Shape of a real gemini-embedding-001 batch response (values cut to 3; no secrets)."""
    from onboarding_agent.config import REPO_ROOT

    shape = json.loads((REPO_ROOT / "tests" / "fixtures" / "rag" / "embed_response_shape.json").read_text())
    embeddings = shape["embeddings"]
    assert len(embeddings) > 1 and all(e["values_len"] == 3072 for e in embeddings)
    assert set(shape) <= {"embeddings", "metadata"}  # no token counts came back, so tokens stay estimates

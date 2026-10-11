"""Annex files and transcribed image tables: parsing, links, retrieval, checks, cards, rebuild cost.

The annex excerpt and the table rows are public statute content (저작권법 제7조), kept short.
"""

from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from onboarding_agent.config import get_settings
from onboarding_agent.rag import service as rag_service
from onboarding_agent.rag.answer import model_text
from onboarding_agent.rag.chunker import (
    annex_source_doc,
    attach_tables,
    chunk_annex,
    chunk_law,
    chunk_transcription,
    link_refs,
    parse_transcription,
    source_doc,
    transcription_reviewed,
    transcription_source_doc,
)
from onboarding_agent.rag.embed import EmbeddingCache, GeminiEmbedder, HashEmbedder
from onboarding_agent.rag.index import build_index, load_index
from onboarding_agent.rag.models import ModelAnswer
from onboarding_agent.rag.parse_law import IMAGE_MARKER, parse_annex, read_text_lines
from onboarding_agent.rag.retrieve import Retriever
from onboarding_agent.rag.usage import UsageMeter
from onboarding_agent.rag.validate import validate_answer

from .conftest import LAW, FakeLLM, make_service, parsed
from .test_index_embed import FakeEmbedApi, config
from .test_regulations_page import PAGE, TIMEOUT, ask, page_text

# 제6조 gets an image table, as 제15조 has in the real PDF.
LAW_WITH_TABLE = LAW.replace(
    "② 연가 일수는 제5조에 따른 병가 일수와 따로 계산한다.",
    "② 연가 일수는 제5조에 따른 병가 일수와 따로 계산한다.\n③ 재직기간별 연가 일수는 다음 표와 같다.\n" + IMAGE_MARKER,
)

ANNEX = """■ 국가공무원 복무규정 [별표 1] <개정 2025. 2. 11.>
선서문(제2조제2항 관련)
본인은 법령을 준수하고 국가에 대한 충성과 국민에 대한 봉사를 다짐합니다.
"""

TRANSCRIPTION = """---
법령명: 국가공무원 복무규정
버전: 대통령령 제36728호
시행일: 2026-10-02
조문: "6"
조문제목: 연가계획 및 승인
항: "3"
PDF쪽: 7
인쇄쪽: 7
상태: 사람 검수 전
---
| 재직기간 | 연가 일수 |
|---|---|
| 1개월 이상 1년 미만 | 11 |
| 6년 이상 | 21 |
"""

NOW = datetime(2026, 10, 10, 9)


def build_chunks(transcription: str = TRANSCRIPTION):
    law = parsed(LAW_WITH_TABLE)
    parent = source_doc(law, sha256="0" * 64, source_uri=None, collected_at=NOW)
    articles = chunk_law(law, parent, {parent.doc_title: parent.doc_id})[0]
    annex = parse_annex(read_text_lines(ANNEX))
    annex_doc = annex_source_doc(annex, parent, sha256="1" * 64, source_uri=None, collected_at=NOW)
    front, table = parse_transcription(transcription)
    table_doc = transcription_source_doc(front, parent, sha256="2" * 64, source_uri="t.txt", collected_at=NOW)
    extra = chunk_annex(annex, annex_doc) + [chunk_transcription(front, table, table_doc)]
    chunks = attach_tables(link_refs(articles + extra))
    return [parent, annex_doc, table_doc], chunks


def by_id(chunks):
    return {c.chunk_id: c for c in chunks}


def test_annex_header_title_and_related_article():
    annex = parse_annex(read_text_lines(ANNEX))
    assert (annex.law_title, annex.kind, annex.number) == ("국가공무원 복무규정", "별표", "1")
    assert annex.note == "개정 2025. 2. 11." and annex.related_article == "2"
    assert parse_annex(read_text_lines(LAW)) is None  # a law file is not an annex


def test_annex_document_keeps_effective_date_unknown_and_links_to_its_article():
    docs, chunks = build_chunks()
    annex_doc = docs[1]
    assert annex_doc.doc_id == "dec36728-annex1" and annex_doc.doc_type == "annex"
    assert annex_doc.effective_date is None and "effective_date" in annex_doc.unverified
    annex = by_id(chunks)["dec36728-annex1:t1"]
    assert annex.kind == "annex" and annex.heading.startswith("[별표 1] 선서문")
    assert annex.refs == ["dec36728:a2"]
    article = by_id(chunks)["dec36728:a2"]
    assert article.refs[0] == "dec36728-annex1:t1" and "별표 1 (미색인)" not in article.refs


def test_transcription_document_chunk_and_review_state():
    docs, chunks = build_chunks()
    table_doc = docs[2]
    assert table_doc.doc_id == "dec36728-t6" and "manual_transcription" in table_doc.unverified
    table = by_id(chunks)["dec36728-t6:t1"]
    assert table.kind == "table" and table.article_no == "6" and table.paragraph_no == "3"
    assert table.location["pdf_page"] == 7 and table.location["transcribed"] is True
    assert "---" not in table.search_text and "11" in table.search_text
    assert table.embed_text.startswith("[국가공무원 복무규정] 제6조(연가계획 및 승인) 제3항의 표(전사본)")
    assert by_id(chunks)["dec36728:a6"].refs[0] == "dec36728-t6:t1"

    reviewed = TRANSCRIPTION.replace("상태: 사람 검수 전", "상태: 검수 완료(2026-10-11)")
    front, _ = parse_transcription(reviewed)
    assert transcription_reviewed(front)
    assert "manual_transcription" not in build_chunks(reviewed)[0][2].unverified


def test_model_sees_the_transcription_instead_of_the_image_gap(index_dir):
    docs, chunks = build_chunks()
    embedder = HashEmbedder()
    build_index(index_dir, docs, chunks, embedder, excluded=[], unindexed=[])
    index = load_index(index_dir, embedder.config)
    article = model_text(index.chunk("dec36728:a6"), index)
    assert IMAGE_MARKER not in article and "옮겨 적은 표 전사본: dec36728-t6:t1" in article
    assert model_text(index.chunk("dec36728-t6:t1"), index).startswith("[표 전사본")


def test_article_lookup_brings_its_table_and_annex_titles_are_not_aliases(index_dir):
    docs, chunks = build_chunks()
    embedder = HashEmbedder()
    build_index(index_dir, docs, chunks, embedder, excluded=[], unindexed=[])
    retriever = Retriever(load_index(index_dir, embedder.config), embedder)
    ids = {c.chunk_id for c in retriever.article("국가공무원 복무규정", "제6조")}
    assert ids == {"dec36728:a6", "dec36728-t6:t1"}
    assert set(retriever.aliases.values()) == {"dec36728"}
    found = retriever.search("국가공무원 복무규정 제6조 연가 일수", top_k=6, mode="bm25").ids
    assert "dec36728-t6:t1" in found


@pytest.mark.parametrize(
    ("evidence", "ok"),
    [(["dec36728-t6:t1"], True), (["dec36728:a6"], False)],
)
def test_a_bare_number_in_a_cited_table_cell_counts(evidence, ok):
    docs, chunks = build_chunks()
    retrieved = by_id(chunks)
    answer = ModelAnswer.model_validate(
        {
            "status": "answered",
            "one_line": "재직기간 6년 이상이면 21일입니다.",
            "points": [{"text": "재직기간 6년 이상이면 연가는 21일입니다.", "evidence_ids": evidence}],
        }
    )
    result = validate_answer(answer, retrieved, {d.doc_id: d.doc_title for d in docs})
    assert (result.checks["수치·날짜"] == "passed") is ok


def test_rebuild_with_new_table_and_annex_embeds_only_those(tmp_path):
    api = FakeEmbedApi()
    cache = EmbeddingCache(tmp_path / "cache", config())
    root = tmp_path / "index"
    law = parsed(LAW_WITH_TABLE)
    parent = source_doc(law, sha256="0" * 64, source_uri=None, collected_at=NOW)
    articles = link_refs(chunk_law(law, parent, {parent.doc_title: parent.doc_id})[0])
    first = GeminiEmbedder(api, config(), cache)
    build_index(root, [parent], articles, first, excluded=[], unindexed=[], now=datetime(2026, 10, 10, 9, 0, 0))

    docs, chunks = build_chunks()
    second = GeminiEmbedder(api, config(), cache)
    build_index(root, docs, chunks, second, excluded=[], unindexed=[], now=datetime(2026, 10, 10, 9, 0, 1))
    # refs changed on 제2조 and 제6조, but refs are not part of the embedded text
    assert second.embedded == 2 and second.cached == len(articles)


def test_evidence_card_badges(tmp_path, monkeypatch):
    docs, chunks = build_chunks()
    embedder = HashEmbedder()
    build_index(get_settings().reg_index_dir, docs, chunks, embedder, excluded=[], unindexed=[])
    index = load_index(get_settings().reg_index_dir, embedder.config)
    llm = FakeLLM(
        [
            {
                "status": "answered",
                "one_line": "6년 이상이면 21일입니다.",
                "points": [{"text": "재직기간 6년 이상이면 연가는 21일입니다.", "evidence_ids": ["dec36728-t6:t1"]}],
            }
        ]
    )
    service = make_service(index, embedder, llm, tmp_path)
    runtime = rag_service.Runtime(
        get_settings(), UsageMeter(tmp_path / "usage.jsonl"), None, embedder, index, service.retriever, service
    )
    monkeypatch.setattr(rag_service, "build_runtime", lambda settings, **_: runtime)
    app = ask(AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run(), "국가공무원 복무규정 제6조 연가 일수는?")
    text = page_text(app)
    assert not app.exception
    assert "표 전사본(검수 대기)" in text
    assert "옮겨 적은 표 전사본: dec36728-t6:t1" in text
    assert "원문 PDF의 이미지 표를 옮겨 적은 것입니다" in text

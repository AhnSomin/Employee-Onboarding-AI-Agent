"""Parsing and chunking of statute text (instruction v2, sections 4 and 11)."""

from datetime import date

from onboarding_agent.rag.chunker import (
    SPLIT_OVER,
    chunk_law,
    join_lines,
    normalize,
    source_doc,
)
from onboarding_agent.rag.parse_law import IMAGE_MARKER, lines_from_pages, parse_lines, read_text_lines

from .conftest import LAW, RULE, docs_and_chunks, parsed


def test_header_gives_dates_and_version_never_the_file_name():
    law = parsed(LAW)
    assert law.title == "국가공무원 복무규정"
    assert law.effective_date == date(2026, 10, 2)
    assert law.promulgation_date == date(2026, 9, 29)
    assert law.version_label == "대통령령 제36728호"
    assert law.revision_kind == "타법개정"


def test_missing_effective_date_stays_unknown():
    law = parsed(LAW.replace("[시행 2026. 10. 2.] ", ""))
    doc = source_doc(law, sha256="0" * 64, source_uri=None, collected_at=date(2026, 10, 10))
    assert law.effective_date is None and doc.effective_date is None
    assert "effective_date" in doc.unverified


def test_branch_articles_deleted_articles_addenda_and_sections():
    law = parsed(LAW)
    live = [a.article_no for a in law.articles if not a.deleted]
    assert live == ["1", "2", "2의2", "4", "5", "6"]
    kinds = [item["kind"] for item in law.excluded]
    assert "삭제 조문" in kinds and "부칙" in kinds and "머리말" in kinds
    assert any("별표 1" in item["detail"] for item in law.excluded if item["kind"] == "별표·서식")
    article4 = next(a for a in law.articles if a.article_no == "4")
    assert article4.section_path == "제2장 근무시간"  # the heading's amendment note is not part of the name
    assert law.warnings == []


def test_numbering_gap_and_backwards_reference_lines():
    text = LAW.replace("제4조(근무시간 등)", "제14조(근무시간 등)").replace("제5조(병가)", "제16조(병가)")
    text = text.replace("제6조(연가계획 및 승인)", "제17조(연가계획 및 승인)")
    law = parsed(text)
    assert any("제3조 다음 제14조" in w for w in law.warnings)
    assert any("제14조 다음 제16조" in w for w in law.warnings)
    wrapped = parse_lines(read_text_lines(LAW.replace("국\n가공무원의 복무", "\n제1조(목적)에 따른 복무")))
    assert [a.article_no for a in wrapped.articles if not a.deleted][:2] == ["1", "2"]  # not a second 제1조


def test_page_furniture_and_running_title_are_dropped_and_image_gaps_marked():
    furniture = (
        "법제처                                                            7                     국가법령정보센터"
    )
    pages = [
        [(furniture.replace("7", "1"), 30.0), ("국가공무원 복무규정", 802.0), ("제1조(목적) 목적이다.", 771.0)],
        [
            (furniture, 30.0),
            ("국가공무원 복무규정", 802.0),
            ("제15조(연가 일수) ①재직기간별 연가 일수는 다음과 같다.", 683.0),
            ("다만, 유사경력이 있는 경우에는", 667.0),
            ("각각 3일을 더한다.", 651.0),
            ("12. 11.>", 635.0),
            ("② 제1항에서 “재직기간”이란", 495.5),  # 140pt below: a table printed as an image
            ("계산한 재직기간을 말한다.", 479.5),
            ("1. 법령에 따른 의무 수행으로 인한 휴직", 463.5),
        ],
    ]
    lines = lines_from_pages(pages, [False, True])
    texts = [line.text for line in lines]
    assert (
        furniture not in texts and texts.count("국가공무원 복무규정") == 1
    )  # page 1 title kept, page 2 running title dropped
    assert texts[texts.index("12. 11.>") + 1] == IMAGE_MARKER
    assert lines[-1].pdf_page == 2 and lines[-1].printed_page == 7


def test_one_article_one_chunk_with_location_and_header():
    docs, chunks = docs_and_chunks()
    by_id = {c.chunk_id: c for c in chunks}
    assert {"dec36728:a1", "dec36728:a2", "dec36728:a2-2", "dec36728:a4", "dec36728:a5", "dec36728:a6"} <= set(by_id)
    assert "dec36728:a3" not in by_id  # deleted
    chunk = by_id["dec36728:a2-2"]
    assert chunk.article_no == "2의2" and chunk.article_title == "책임 완수"
    assert chunk.embed_text.startswith("[국가공무원 복무규정] 제2조의2(책임 완수)\n")
    assert chunk.location == {"lines": [chunk.location["lines"][0], chunk.location["lines"][1]]}
    assert chunk.location["lines"][0] < chunk.location["lines"][1]


def test_search_text_joins_print_lines_and_drops_amendment_notes():
    docs, chunks = docs_and_chunks()
    by_id = {c.chunk_id: c for c in chunks}
    assert "국가공무원의 복무에" in by_id["dec36728:a1"].search_text  # "국\n가공무원" broke inside a word
    assert "원칙으로 한다" in by_id["dec36728:a4"].search_text
    assert "병가를 승인할" in by_id["dec36728:a5"].search_text
    assert "<개정" not in by_id["dec36728:a2"].search_text and "[전문개정" not in by_id["dec36728:a1"].search_text
    assert "<개정 2013. 3. 23." in by_id["dec36728:a2"].text  # the source text keeps them
    assert join_lines(["제10호에", "따라 임용된"]) == "제10호에 따라 임용된"
    assert join_lines(["2013.", "12. 11."]) == "2013. 12. 11."
    assert join_lines(["공ㆍ사(公", "ㆍ私) 생활"]) == "공ㆍ사(公ㆍ私) 생활"
    assert IMAGE_MARKER not in join_lines(["앞", IMAGE_MARKER, "뒤"])
    assert IMAGE_MARKER in join_lines(["앞", IMAGE_MARKER, "뒤"], keep_marker=True)
    assert normalize("Ａ１　<개정 2020. 1. 1.> 다만, 6일") == "A1 다만, 6일"  # numbers and provisos untouched


def test_proviso_flag_and_refs():
    docs, chunks = docs_and_chunks()
    by_id = {c.chunk_id: c for c in chunks}
    assert by_id["dec36728:a4"].has_proviso  # "다만,"
    assert not by_id["dec36728:a2-2"].has_proviso
    assert "「국가공무원법」 제55조" in by_id["dec36728:a2"].refs
    assert "별표 1 (미색인)" in by_id["dec36728:a2"].refs
    assert by_id["dec36728:a6"].refs == ["dec36728:a5"]
    # 「국가공무원 복무규정」 제2조제3항 및 제4조 — the list continues in the other document
    assert by_id["pmo2161:a1"].refs == ["dec36728:a2", "dec36728:a4"]
    assert by_id["pmo2161:a3"].refs == ["pmo2161:a2"]


def test_long_article_splits_at_paragraphs_with_repeated_header_and_proviso_kept():
    filler = "가" * (SPLIT_OVER // 2)
    long_law = LAW.replace(
        "③ 병가 일수가 연간 6일을 초과하는 경우에는 의사의 진단서를 첨부하여야 한다.",
        f"③ {filler}\n다만, 예외로 한다.\n④ {filler}\n⑤ 삭제<2015. 10. 6.>",
    )
    law = parsed(long_law)
    doc = source_doc(law, sha256="1" * 64, source_uri=None, collected_at=date(2026, 10, 10))
    chunks, dropped = chunk_law(law, doc)
    pieces = [c for c in chunks if c.article_no == "5"]
    assert len(pieces) >= 2 and [c.chunk_id for c in pieces][0] == "dec36728:a5:p1"
    assert all(c.embed_text.startswith("[국가공무원 복무규정] 제5조(병가)") for c in pieces)
    third = next(c for c in pieces if c.paragraph_no == "3")
    assert "다만, 예외로 한다." in third.text and third.embed_text.startswith("[국가공무원 복무규정] 제5조(병가) 제3항")
    assert dropped == [{"kind": "삭제 항", "detail": "국가공무원 복무규정 제5조 제5항"}]


def test_same_article_number_in_two_documents_stays_distinct():
    docs, chunks = docs_and_chunks()
    firsts = [c for c in chunks if c.article_no == "1"]
    assert {c.chunk_id for c in firsts} == {"dec36728:a1", "pmo2161:a1"}
    assert len({d.doc_id for d in docs}) == 2
    assert parsed(RULE).effective_date == date(2026, 10, 2)


def test_garbled_paragraph_numbers_after_fifteen_are_restored_but_item_markers_stay():
    # The 법제처 PDF font prints ⑯-⑳ as "1^", "1&", "1*", "1(", "2)".
    text = LAW.replace(
        "② 연가 일수는 제5조에 따른 병가 일수와 따로 계산한다.",
        "② 연가 일수는 제5조에 따른 병가 일수와 따로 계산한다.\n"
        + "\n".join(f"{chr(0x2460 + n - 1)} 제{n}항이다." for n in range(3, 16))
        + "\n1^ 여성공무원은 임신기간 중 검진을 위해 10일의 범위에서 임신검진휴가를 사용할 수 있다."
        + "\n1& 제17항이다.\n1) 제17항의 항목이다.\n2) 다른 항목이다.",
    )
    law = parsed(text)
    article = next(a for a in law.articles if a.article_no == "6")
    starts = [line.text[:2] for line in article.lines]
    assert "⑯ " in starts and "⑰ " in starts and "1^" not in starts
    assert "1)" in starts and "2)" in starts  # not the next paragraph number, so left alone
    assert any("1^→⑯" in w for w in law.warnings)

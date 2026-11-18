"""本文の取得(read_text)・目次・ページの先頭行のテスト。"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_highlight as ph  # noqa: E402
import pdf_highlight_tools as tools  # noqa: E402
from test_pdf_highlight import TEXT1, TEXT2, make_pdf  # noqa: E402

pytest.importorskip("rapidfuzz")


@pytest.fixture()
def work(tmp_path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture()
def cfg(work) -> tools.ToolConfig:
    return tools.ToolConfig(allowed_dirs=(work,))


@pytest.fixture()
def sample(work) -> Path:
    return make_pdf(work / "sample.pdf")


def latin_pdf(path: Path) -> Path:
    """語の間に空白の文字が無く、位置の隙間だけで語を分けている PDF(LaTeX などに多い)。"""
    c = canvas.Canvas(str(path))
    c.setFont("Helvetica", 12)
    c.drawString(100, 700, "Hello")
    c.drawString(142, 700, "World.")
    c.drawString(100, 680, "Second")
    c.drawString(150, 680, "line")
    c.showPage()
    c.save()
    return path


# --------------------------------------------------------------------------- #
# read_text
# --------------------------------------------------------------------------- #
def test_read_text_format(sample):
    with ph.PdfDocument.open(sample) as doc:
        r = doc.read_text([1, 2])
    assert r.pages == [1, 2] and r.next_offset is None and r.offset == 0
    lines = r.text.split("\n")
    assert lines[0] == "[p.1]" and "[p.2]" in lines
    body1 = "".join(lines[1 : lines.index("[p.2]")])
    assert body1 == TEXT1, "日本語は行をまたいでも空白が入らず、元の文章に戻る"
    assert r.total_chars == len(r.text)


def test_read_text_normalizes_page_order_and_duplicates(sample):
    with ph.PdfDocument.open(sample) as doc:
        assert doc.read_text([2, 1, 2]).pages == [1, 2]


def test_continuation_reassembles_the_full_text(tmp_path):
    p = make_pdf(tmp_path / "long.pdf", pages=tuple(TEXT1 * 3 for _ in range(4)))
    with ph.PdfDocument.open(p) as doc:
        full = doc.read_text([1, 2, 3, 4], max_chars=20000)
        assert full.next_offset is None
        got, offset, calls = "", 0, 0
        while offset is not None:
            r = doc.read_text([1, 2, 3, 4], max_chars=150, offset=offset)
            assert len(r.text) <= 150 and r.offset == (len(got))
            got += r.text
            offset, calls = r.next_offset, calls + 1
            assert calls < 100
    assert got == full.text, "続きを順に取ると、欠落も重複もなく全体に戻る"
    assert calls > 5


def test_chunks_end_on_line_boundaries_when_possible(tmp_path):
    p = make_pdf(tmp_path / "long.pdf", pages=(TEXT1 * 6,))
    with ph.PdfDocument.open(p) as doc:
        offset, chunks = 0, []
        while offset is not None:
            r = doc.read_text([1], max_chars=200, offset=offset)
            chunks.append(r)
            offset = r.next_offset
    assert len(chunks) > 2
    assert all(c.text.endswith("\n") for c in chunks[:-1]), "最後以外は、行の切れ目で終わる"


def test_only_requested_pages_are_extracted(tmp_path, monkeypatch):
    p = make_pdf(tmp_path / "ten.pdf", pages=tuple(f"ページ{i}の本文です。" for i in range(10)))
    calls = []
    real = ph._extract_page
    monkeypatch.setattr(ph, "_extract_page", lambda pdf, i, mb: (calls.append(i), real(pdf, i, mb))[1])
    with ph.PdfDocument.open(p) as doc:
        assert "ページ7の本文" in doc.read_text([8]).text
    assert calls == [7]


def test_empty_page_is_marked(tmp_path):
    w = PdfWriter()
    w.add_blank_page(200, 200)
    p = tmp_path / "blank.pdf"
    with open(p, "wb") as f:
        w.write(f)
    with ph.PdfDocument.open(p) as doc:
        r = doc.read_text([1])
    assert r.text == "[p.1] (テキストなし)" and r.empty_pages == [1]


@pytest.mark.parametrize("kwargs", [
    {"pages": []}, {"pages": [3]}, {"pages": [1], "max_chars": 0}, {"pages": [1], "max_chars": 20001},
    {"pages": [1], "offset": -1}, {"pages": [1], "offset": 10**6}, {"pages": [True]},
])
def test_read_text_rejects_bad_arguments(sample, kwargs):
    kwargs = dict(kwargs)
    pages = kwargs.pop("pages")
    with ph.PdfDocument.open(sample) as doc:
        with pytest.raises(ValueError):
            doc.read_text(pages, **kwargs)


def test_read_text_removes_control_characters():
    assert ph._clean("a\x00b‮c") == "abc"  # 制御文字・双方向制御文字(Cf)を除く


# --------------------------------------------------------------------------- #
# 語の間に空白が無い PDF(位置の隙間で補う)
# --------------------------------------------------------------------------- #
def test_spaces_are_inferred_from_gaps(work):
    p = latin_pdf(work / "latin.pdf")
    with ph.PdfDocument.open(p) as doc:
        assert [g.text for g in doc._page(0)[0]][:6] == list("HelloW")[:6], "前提: 空白の文字は無い"
        text = doc.read_text([1]).text
        assert text == "[p.1]\nHello World.\nSecond line"
        # 検索の候補・ref も同じ綴りで返り、往復できる
        res = doc.search("helloworld", ignore_case=True)
        assert [c.text for c in res.candidates] == ["Hello World"]
        c = res.candidates[0]
        assert doc.resolve_ref(c.ref).ids == c.glyph_ids
        assert doc.search("secondline", ignore_case=True).candidates[0].text == "Second line"


def test_no_space_is_inserted_inside_japanese_or_words(sample, work):
    with ph.PdfDocument.open(sample) as doc:
        assert "吾輩は猫である。名前はまだ無い。" in doc.read_text([1]).text
    p = latin_pdf(work / "latin.pdf")
    with ph.PdfDocument.open(p) as doc:
        assert "Hello" in doc.read_text([1]).text and "H ello" not in doc.read_text([1]).text


# --------------------------------------------------------------------------- #
# 目次・ページの先頭行
# --------------------------------------------------------------------------- #
def outlined_pdf(path: Path, src: Path) -> Path:
    w = PdfWriter(clone_from=PdfReader(str(src)))
    ch1 = w.add_outline_item(title="第1章 はじめに\x00", page_number=0)
    w.add_outline_item(title="1.1 概要", page_number=0, parent=ch1)
    w.add_outline_item(title="第2章 英語", page_number=1)
    with open(path, "wb") as f:
        w.write(f)
    return path


def test_outline(sample, work):
    p = outlined_pdf(work / "outlined.pdf", sample)
    with ph.PdfDocument.open(p) as doc:
        entries, truncated = doc.outline()
    assert entries == [
        {"title": "第1章 はじめに", "page": 1, "level": 1},
        {"title": "1.1 概要", "page": 1, "level": 2},
        {"title": "第2章 英語", "page": 2, "level": 1},
    ] and truncated is False


def test_outline_limit_and_missing(sample, work):
    p = outlined_pdf(work / "outlined.pdf", sample)
    with ph.PdfDocument.open(p) as doc:
        entries, truncated = doc.outline(limit=2)
    assert len(entries) == 2 and truncated is True
    with ph.PdfDocument.open(sample) as doc:
        assert doc.outline() == ([], False)


def test_scan_pages_heads(sample):
    with ph.PdfDocument.open(sample) as doc:
        empty, heads, truncated = doc.scan_pages(heads=True)
    assert empty == [] and truncated is False
    assert [h["page"] for h in heads] == [1, 2]
    assert heads[0]["head"].startswith("吾輩は猫である") and heads[1]["head"].startswith("Hello World.")
    assert all(len(h["head"]) <= 40 for h in heads)


def test_scan_pages_does_not_keep_pages_in_memory(sample):
    with ph.PdfDocument.open(sample) as doc:
        doc.scan_pages(heads=True)
        assert doc._pages == {}


def test_shorten_respects_the_limit():
    assert ph._shorten("あ" * 10, 20) == "あ" * 10
    s = ph._shorten("あ" * 100, 20)
    assert len(s) <= 20 and "…" in s
    assert len(ph._shorten("x" * 500)) == 81  # 既定(82 文字以内): 40 + … + 40


# --------------------------------------------------------------------------- #
# ツール層
# --------------------------------------------------------------------------- #
def call(cfg, name, **args) -> dict:
    return tools.dispatch(name, args, cfg)


def test_tool_read_text_basic_and_continuation(cfg, work):
    p = make_pdf(work / "long.pdf", pages=tuple(TEXT1 * 3 for _ in range(3)))
    r = call(cfg, "pdf_read_text", input=str(p), pages=[1, 2, 3], max_chars=400)
    assert r["ok"] and r["pages"] == [1, 2, 3] and r["offset"] == 0 and r["next_offset"] > 0
    assert len(r["text"]) <= 400 and r["total_chars"] > 400
    got = r["text"]
    while "next_offset" in r:
        r = call(cfg, "pdf_read_text", input=str(p), pages=[1, 2, 3], max_chars=400, offset=r["next_offset"])
        got += r["text"]
    assert len(got) == r["total_chars"]


def test_tool_read_text_requires_pages(cfg, sample):
    r = call(cfg, "pdf_read_text", input=str(sample))
    assert r["ok"] is False and r["error"]["code"] == "invalid_argument" and "outline" in r["error"]["hint"]
    r = call(cfg, "pdf_read_text", input=str(sample), pages=list(range(1, 52)))
    assert r["error"]["code"] == "invalid_argument"
    r = call(cfg, "pdf_read_text", input=str(sample), pages=[1], max_chars=10)
    assert r["error"]["code"] == "invalid_argument"
    r = call(cfg, "pdf_read_text", input=str(sample), pages=[9])
    assert r["error"]["code"] == "invalid_argument"
    r = call(cfg, "pdf_read_text", input=str(sample), pages=[1], bogus=1)
    assert r["error"]["code"] == "invalid_argument"


def test_tool_read_text_no_text_layer_and_policy(cfg, work, tmp_path):
    w = PdfWriter()
    w.add_blank_page(200, 200)
    blank = work / "blank.pdf"
    with open(blank, "wb") as f:
        w.write(f)
    assert call(cfg, "pdf_read_text", input=str(blank), pages=[1])["error"]["code"] == "no_text_layer"
    outside = make_pdf(tmp_path / "outside.pdf")
    assert call(cfg, "pdf_read_text", input=str(outside), pages=[1])["error"]["code"] == "path_not_allowed"


def test_tool_inspect_outline_and_heads_are_opt_in(cfg, sample, work):
    plain = call(cfg, "pdf_inspect", input=str(sample))["document"]
    assert "outline" not in plain and "page_heads" not in plain
    p = outlined_pdf(work / "outlined.pdf", sample)
    d = call(cfg, "pdf_inspect", input=str(p), outline=True, page_heads=True)["document"]
    assert [e["page"] for e in d["outline"]] == [1, 1, 2] and len(d["page_heads"]) == 2


def test_navigation_is_much_smaller_than_full_text(cfg, work):
    """目次・先頭行で絞って必要なページだけ読む方が、全文より遥かに小さい(文字数の比較)。"""
    pages = tuple(f"第{i}章 見出し{i}\n" + ("本文です。" * 120) for i in range(1, 41))
    p = make_pdf(work / "doc.pdf", pages=tuple(t.replace("\n", " ") for t in pages))
    nav = tools.dumps(call(cfg, "pdf_inspect", input=str(p), page_heads=True))
    one = tools.dumps(call(cfg, "pdf_read_text", input=str(p), pages=[7], max_chars=4000))
    full = tools.dumps(call(cfg, "pdf_read_text", input=str(p), pages=list(range(1, 41)), max_chars=20000))
    assert len(nav) + len(one) < len(full) / 3
    assert "第7章" in nav

"""pdf_highlight のテスト。

テスト用 PDF は reportlab の組み込み日本語 CID フォント(HeiseiMin-W3)で生成するため、
OS のフォントに依存しない。注釈位置は pypdfium2 で描画して画素レベルで検証する。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_highlight as ph  # noqa: E402

pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))

TEXT1 = (
    "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。"
    "何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。"
    "吾輩はここで始めて人間というものを見た。"
)
TEXT2 = "Hello World. 英数字 ABC と日本語が混在する行です。ｈｅｌｌｏ　ｗｏｒｌｄ（全角）。"


def make_pdf(path: Path, *, pages: tuple[str, ...] = (TEXT1, TEXT2)) -> Path:
    style = ParagraphStyle("j", fontName="HeiseiMin-W3", fontSize=12, leading=20, wordWrap="CJK")
    story = []
    for i, t in enumerate(pages):
        if i:
            story.append(PageBreak())
        story += [Paragraph(t, style), Spacer(1, 10)]
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=120, rightMargin=120).build(story)
    return path


@pytest.fixture()
def sample(tmp_path) -> Path:
    return make_pdf(tmp_path / "sample.pdf")


def annots(pdf: Path | bytes, page: int = 0) -> list:
    src = io.BytesIO(pdf) if isinstance(pdf, bytes) else str(pdf)
    reader = PdfReader(src)
    return [a.get_object() for a in reader.pages[page].get("/Annots", [])]


def render(pdf: Path | bytes, page: int = 0, scale: float = 2.0):
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf if isinstance(pdf, bytes) else str(pdf))
    return doc[page].render(scale=scale).to_pil().convert("RGB")


def yellow_bbox(img):
    """黄色(ハイライト)画素の外接矩形と、その中の暗色(文字)画素数を返す。"""
    px = img.load()
    xs, ys = [], []
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            if r > 200 and g > 200 and b < 120:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None, 0
    box = (min(xs), min(ys), max(xs), max(ys))
    dark = sum(
        1
        for y in range(box[1], box[3] + 1)
        for x in range(box[0], box[2] + 1)
        if sum(px[x, y]) < 200
    )
    return box, dark


# --------------------------------------------------------------------------- #
# 基本動作
# --------------------------------------------------------------------------- #
def test_basic_counts_and_annotations(sample, tmp_path):
    out = tmp_path / "out.pdf"
    res = ph.highlight_pdf(sample, out, ["吾輩", "存在しない"])
    assert res.counts() == {"吾輩": 2, "存在しない": 0}
    assert res.total == 2
    assert out.exists()
    a = annots(out)
    assert len(a) == 2 and all(x["/Subtype"] == "/Highlight" for x in a)


def test_match_across_line_break_gives_one_annotation_with_multiple_quads(sample, tmp_path):
    kw = "何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している"
    res = ph.highlight_pdf(sample, tmp_path / "o.pdf", kw)
    assert res.counts()[kw] == 1
    (m,) = res.matches
    assert len(m.rects) >= 2, "改行をまたぐので複数行の矩形になるはず"
    (a,) = annots(tmp_path / "o.pdf")
    assert len(a["/QuadPoints"]) == 8 * len(m.rects)


def test_whitespace_and_fullwidth_normalization(sample, tmp_path):
    res = ph.highlight_pdf(sample, tmp_path / "o.pdf", ["Hello World", "hello world"], ignore_case=True, cross_page=False)
    # 半角 "Hello World" と全角 "ｈｅｌｌｏ　ｗｏｒｌｄ" の両方に一致する(NFKC + 空白無視 + 大小無視)
    assert res.counts()["Hello World"] == 2
    res2 = ph.highlight_pdf(sample, tmp_path / "o2.pdf", "Hello World")  # 大小区別あり
    assert res2.total == 1  # 全角小文字の方は一致しない


def test_dakuten_not_confused(tmp_path):
    p = make_pdf(tmp_path / "d.pdf", pages=("かがきぎ",))
    assert ph.highlight_pdf(p, tmp_path / "o.pdf", "が").total == 1
    assert ph.highlight_pdf(p, tmp_path / "o2.pdf", "か").total == 1


def test_cross_page(tmp_path):
    p = make_pdf(tmp_path / "c.pdf", pages=("あいうえお", "かきくけこ"))
    assert ph.highlight_pdf(p, tmp_path / "a.pdf", "おか").total == 0
    res = ph.highlight_pdf(p, tmp_path / "b.pdf", "おか", cross_page=True)
    assert res.total == 1  # ページをまたいでも 1 件
    assert sorted(m.page for m in res.matches) == [1, 2]
    assert len(annots(tmp_path / "b.pdf", 0)) == 1 and len(annots(tmp_path / "b.pdf", 1)) == 1


def test_highlight_is_drawn_over_the_keyword(sample, tmp_path):
    out = tmp_path / "o.pdf"
    ph.highlight_pdf(sample, out, "どこで生れたか")
    box, dark = yellow_bbox(render(out))
    assert box is not None
    assert dark > 50, "黄色領域の中に文字が重なっているはず"


# --------------------------------------------------------------------------- #
# キーワードごとの色指定
# --------------------------------------------------------------------------- #
def _is_yellow(p):
    return p[0] > 200 and p[1] > 200 and p[2] < 120


def _is_blue(p):  # 00BFFF を白地に重ねた色
    return p[0] < 80 and p[1] > 150 and p[2] > 200


def color_xs(img, pred):
    """条件に合う画素の x 座標の (最小, 最大)。無ければ None。"""
    px = img.load()
    xs = [x for y in range(img.height) for x in range(img.width) if pred(px[x, y])]
    return (min(xs), max(xs)) if xs else None


def _highlight_colors(annotations) -> list[list[float]]:
    return [[round(float(v), 2) for v in a["/C"]] for a in annotations]


def test_per_keyword_colors_with_dict(sample, tmp_path):
    out = tmp_path / "o.pdf"
    res = ph.highlight_pdf(sample, out, {"どこで生れたか": "blue", "吾輩": "yellow", "存在しない": "red"})
    assert res.counts() == {"どこで生れたか": 1, "吾輩": 2, "存在しない": 0}
    assert res.colors == {"どこで生れたか": "00bfff", "吾輩": "ffff00", "存在しない": "ff0000"}
    assert {m.color for m in res.matches if m.keyword == "どこで生れたか"} == {"00bfff"}

    a = annots(out)
    assert sorted(_highlight_colors(a)) == [[0.0, 0.75, 1.0], [1.0, 1.0, 0.0], [1.0, 1.0, 0.0]]

    img = render(out)
    blue, yellow = color_xs(img, _is_blue), color_xs(img, _is_yellow)
    assert blue is not None and yellow is not None, "青と黄の両方が描画されているはず"


def test_mixed_list_uses_default_color_for_plain_keywords(sample, tmp_path):
    res = ph.highlight_pdf(sample, tmp_path / "o.pdf", [("吾輩", "blue"), "どこで生れたか"], color="green")
    assert res.colors == {"吾輩": "00bfff", "どこで生れたか": "00ff00"}
    assert {m.color for m in res.matches} == {"00bfff", "00ff00"}


def test_same_keyword_same_color_is_deduplicated(sample, tmp_path):
    res = ph.highlight_pdf(sample, tmp_path / "o.pdf", [("吾輩", "blue"), ("吾輩", "#00bfff")])
    assert res.counts() == {"吾輩": 2}


def test_same_keyword_with_different_colors_is_rejected(sample, tmp_path):
    with pytest.raises(ValueError):
        ph.highlight_pdf(sample, tmp_path / "o.pdf", [("吾輩", "blue"), ("吾輩", "red")])


@pytest.mark.parametrize("bad", [[("吾輩", 1)], [("吾輩", "blue", "x")], [1], {"吾輩": "zzz"}])
def test_invalid_color_specs(sample, tmp_path, bad):
    with pytest.raises((ValueError, TypeError)):
        ph.highlight_pdf(sample, tmp_path / "o.pdf", bad)


def test_color_is_in_json_dict(sample, tmp_path):
    d = ph.highlight_pdf(sample, tmp_path / "o.pdf", {"吾輩": "blue"}).to_dict()
    assert d["colors"] == {"吾輩": "00bfff"}
    assert {m["color"] for m in d["matches"]} == {"00bfff"}


def test_cli_highlight_option_repeatable(sample, tmp_path, capsys):
    out = tmp_path / "o.pdf"
    code = ph.main([
        str(sample), "Hello World", "-s", "blue", "吾輩", "-s", "FF69B4", "どこで生れたか",
        "-c", "green", "-o", str(out), "--json",
    ])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["colors"] == {"Hello World": "00ff00", "吾輩": "00bfff", "どこで生れたか": "ff69b4"}
    assert len(annots(out, 0)) == 3  # 1 ページ目: 吾輩 2 + どこで生れたか 1
    assert len(annots(out, 1)) == 1  # 2 ページ目: Hello World


def test_cli_only_highlight_option_needs_no_positional_keyword(sample, tmp_path):
    assert ph.main([str(sample), "-s", "blue", "吾輩", "-o", str(tmp_path / "o.pdf")]) == 0


def test_cli_keywords_file_with_tab_separated_color(sample, tmp_path, capsys):
    kf = tmp_path / "kw.txt"
    kf.write_text("吾輩\tblue\n\nHello World\nどこで生れたか\t\n", encoding="utf-8")
    code = ph.main([str(sample), "-k", str(kf), "-o", str(tmp_path / "o.pdf"), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["colors"] == {"吾輩": "00bfff", "Hello World": "ffff00", "どこで生れたか": "ffff00"}


def test_cli_conflicting_colors_is_error(sample, tmp_path):
    argv = [str(sample), "-s", "blue", "吾輩", "-s", "red", "吾輩", "-o", str(tmp_path / "o.pdf")]
    assert ph.main(argv) == 1


# --------------------------------------------------------------------------- #
# 座標系
# --------------------------------------------------------------------------- #
def _modified(sample: Path, tmp_path: Path, *, rotate: int = 0, shift: tuple[float, float] = (0, 0)) -> Path:
    reader = PdfReader(str(sample))
    writer = PdfWriter(clone_from=reader)
    pg = writer.pages[0]
    if shift != (0, 0):
        x0, y0, x1, y1 = (float(v) for v in pg.mediabox)
        # 内容は動かさず MediaBox の原点だけずらす(= 内容は用紙内で平行移動して見える)
        pg.mediabox = RectangleObject((x0 - shift[0], y0 - shift[1], x1 - shift[0], y1 - shift[1]))
        pg.cropbox = pg.mediabox
    if rotate:
        pg.rotate(rotate)
    out = tmp_path / "mod.pdf"
    with open(out, "wb") as f:
        writer.write(f)
    return out


def test_nonzero_mediabox_origin(sample, tmp_path):
    src = _modified(sample, tmp_path, shift=(-50, -30))
    out = tmp_path / "o.pdf"
    assert ph.highlight_pdf(src, out, "どこで生れたか").total == 1
    box, dark = yellow_bbox(render(out))
    assert box is not None and dark > 50


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_rotated_page(sample, tmp_path, angle):
    src = _modified(sample, tmp_path, rotate=angle)
    out = tmp_path / "o.pdf"
    assert ph.highlight_pdf(src, out, "どこで生れたか").total == 1
    box, dark = yellow_bbox(render(out))
    assert box is not None and dark > 50


@pytest.mark.parametrize("angle", [90, 270])
def test_rotated_page_with_shifted_mediabox(sample, tmp_path, angle):
    src = _modified(sample, tmp_path, rotate=angle, shift=(-50, -30))
    out = tmp_path / "o.pdf"
    assert ph.highlight_pdf(src, out, "どこで生れたか").total == 1
    box, dark = yellow_bbox(render(out))
    assert box is not None and dark > 50


# --------------------------------------------------------------------------- #
# 入出力・エラー
# --------------------------------------------------------------------------- #
def test_bytes_and_stream_io(sample, tmp_path):
    data = sample.read_bytes()
    res = ph.highlight_pdf(data, None, "吾輩")
    assert res.data and res.output_path is None and len(annots(res.data)) == 2

    buf = io.BytesIO()
    with open(sample, "rb") as f:
        ph.highlight_pdf(f, buf, "吾輩")
    assert len(annots(buf.getvalue())) == 2


def test_input_is_not_modified(sample, tmp_path):
    before = sample.read_bytes()
    ph.highlight_pdf(sample, tmp_path / "o.pdf", "吾輩")
    assert sample.read_bytes() == before


def test_refuses_overwrite_and_same_path(sample, tmp_path):
    out = tmp_path / "o.pdf"
    ph.highlight_pdf(sample, out, "吾輩")
    with pytest.raises(FileExistsError):
        ph.highlight_pdf(sample, out, "吾輩")
    ph.highlight_pdf(sample, out, "吾輩", overwrite=True)
    with pytest.raises(ValueError):
        ph.highlight_pdf(sample, sample, "吾輩", overwrite=True)


@pytest.mark.parametrize("bad", ["", "   ", [], ["ok", " "]])
def test_invalid_keywords(sample, tmp_path, bad):
    with pytest.raises(ValueError):
        ph.highlight_pdf(sample, tmp_path / "o.pdf", bad)


def test_invalid_color(sample, tmp_path):
    with pytest.raises(ValueError):
        ph.highlight_pdf(sample, tmp_path / "o.pdf", "吾輩", color="zzz")


def test_missing_and_broken_input(tmp_path):
    with pytest.raises(FileNotFoundError):
        ph.highlight_pdf(tmp_path / "nope.pdf", tmp_path / "o.pdf", "x")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(ph.PdfHighlightError):
        ph.highlight_pdf(bad, tmp_path / "o.pdf", "x")


def test_encrypted_pdf(sample, tmp_path):
    w = PdfWriter(clone_from=PdfReader(str(sample)))
    w.encrypt("secret")
    enc = tmp_path / "enc.pdf"
    with open(enc, "wb") as f:
        w.write(f)
    with pytest.raises(ph.PdfPasswordError):
        ph.highlight_pdf(enc, tmp_path / "o.pdf", "吾輩")
    with pytest.raises(ph.PdfPasswordError):
        ph.highlight_pdf(enc, tmp_path / "o.pdf", "吾輩", password="wrong")
    assert ph.highlight_pdf(enc, tmp_path / "o.pdf", "吾輩", password="secret").total == 2


def test_pages_without_text_reported(tmp_path):
    w = PdfWriter()
    w.add_blank_page(200, 200)
    p = tmp_path / "blank.pdf"
    with open(p, "wb") as f:
        w.write(f)
    res = ph.highlight_pdf(p, tmp_path / "o.pdf", "x")
    assert res.total == 0 and res.pages_without_text == [1]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_default_output_and_exit_code(sample, capsys):
    assert ph.main([str(sample), "吾輩"]) == 0
    assert (sample.parent / "sample_highlighted.pdf").exists()
    assert "2 件" in capsys.readouterr().out


def test_cli_json_and_keywords_file(sample, tmp_path, capsys):
    kf = tmp_path / "kw.txt"
    kf.write_text("吾輩\n\nHello World\n", encoding="utf-8")
    code = ph.main([str(sample), "-k", str(kf), "-o", str(tmp_path / "o.pdf"), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"] == {"吾輩": 2, "Hello World": 1}


def test_cli_errors(sample, tmp_path, capsys):
    assert ph.main([str(tmp_path / "nope.pdf"), "x"]) == 1
    assert ph.main([str(sample), "x", "-c", "zzz", "-o", str(tmp_path / "o.pdf")]) == 1
    with pytest.raises(SystemExit) as e:
        ph.main([str(sample)])  # キーワード無し
    assert e.value.code == 2


def test_cli_fail_on_no_match(sample, tmp_path):
    out = tmp_path / "o.pdf"
    assert ph.main([str(sample), "存在しない", "-o", str(out), "--fail-on-no-match"]) == 3
    assert out.exists()

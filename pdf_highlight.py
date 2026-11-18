#!/usr/bin/env python3
"""PDF 内の指定文字列をハイライト(蛍光ペン注釈)するモジュール兼 CLI。

依存ライブラリはすべて商用利用可能な寛容ライセンス:
    * pdfplumber (MIT)  ... 文字ごとの座標の取得
    * pypdf (BSD-3)     ... ハイライト注釈の書き込み

特徴:
    * 日本語対応。全角/半角・合成文字(濁点の分離など)を NFKC で正規化して照合する。
    * 空白・改行を無視して検索するため、検索語が行をまたいでいても一致する。
      ハイライトは行ごとの矩形(QuadPoints)に分けて 1 つの注釈として付与する。
    * 縦書きは非対応(横書き専用)。
    * ``cross_page=True`` でページをまたぐ文字列も検索できる。
    * キーワードごとに色を変えられる(青・黄など複数色を 1 回の実行で付与できる)。

他スクリプトからの利用例::

    from pdf_highlight import highlight_pdf

    result = highlight_pdf("in.pdf", "out.pdf", ["吾輩", "Hello World"], color="FFFF00")
    print(result.counts())      # {'吾輩': 3, 'Hello World': 1}

    # キーワードごとに色を指定する(dict、または (キーワード, 色) を含むリスト)
    highlight_pdf("in.pdf", "out.pdf", {"吾輩": "blue", "猫": "yellow"})

コマンドライン例::

    python pdf_highlight.py in.pdf 吾輩 "Hello World" -o out.pdf
    python pdf_highlight.py in.pdf -s blue 吾輩 -s yellow 猫 -o out.pdf
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, NamedTuple, Optional, Sequence, Union

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, FloatObject

__version__ = "0.2.0"

__all__ = [
    "HighlightMatch",
    "HighlightResult",
    "PdfHighlightError",
    "PdfPasswordError",
    "highlight_pdf",
    "main",
]

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())  # ライブラリとして使われる際にログ設定を強制しない

PdfSource = Union[str, "os.PathLike[str]", bytes, bytearray, BinaryIO]
PdfSink = Union[str, "os.PathLike[str]", BinaryIO, None]
Rect = tuple[float, float, float, float]  # (x0, y0, x1, y1) PDF 座標系(左下原点, pt)

DEFAULT_COLOR = "FFFF00"
_NAMED_COLORS = {
    "yellow": "FFFF00",
    "green": "00FF00",
    "cyan": "00FFFF",
    "pink": "FF69B4",
    "orange": "FFA500",
    "red": "FF0000",
    "blue": "00BFFF",  # 純色の青(0000FF)だと文字が読めなくなるため、明るい青にしている
    "purple": "DA70D6",
}
# 同一行とみなす際、直前の文字より x が戻ってよい許容量(pt)
_LINE_X_TOLERANCE = 1.0


# --------------------------------------------------------------------------- #
# 例外・結果型
# --------------------------------------------------------------------------- #
class PdfHighlightError(Exception):
    """このモジュールが送出する、PDF の読み書きに関する例外の基底クラス。"""


class PdfPasswordError(PdfHighlightError):
    """暗号化 PDF でパスワードが未指定、または誤っている。"""


@dataclass(frozen=True)
class HighlightMatch:
    """ハイライトを付与した 1 注釈分の情報。

    ページをまたぐ一致は、ページごとに別の HighlightMatch になる
    (同じ ``keyword`` と ``occurrence`` を共有する)。
    """

    keyword: str
    occurrence: int  # keyword ごとの出現番号(0 始まり)
    page: int  # 1 始まり
    rects: tuple[Rect, ...]  # 行ごとの矩形
    text: str  # PDF から取り出した一致部分の文字列
    color: str = DEFAULT_COLOR  # 付与した色(6 桁小文字 16 進)


@dataclass
class HighlightResult:
    """highlight_pdf() の戻り値。"""

    keywords: tuple[str, ...]
    matches: list[HighlightMatch] = field(default_factory=list)
    output_path: Optional[Path] = None
    data: Optional[bytes] = None  # output=None のとき、書き出した PDF のバイト列
    pages_without_text: list[int] = field(default_factory=list)  # テキスト層の無いページ(1 始まり)
    colors: dict[str, str] = field(default_factory=dict)  # キーワード -> 色(6 桁小文字 16 進)

    def counts(self) -> dict[str, int]:
        """キーワードごとの一致件数(ページまたぎは 1 件として数える)。"""
        seen = {(m.keyword, m.occurrence) for m in self.matches}
        return {k: sum(1 for kw, _ in seen if kw == k) for k in self.keywords}

    @property
    def total(self) -> int:
        return sum(self.counts().values())

    def to_dict(self) -> dict:
        """JSON 化しやすい dict(PDF 本体のバイト列は含まない)。"""
        return {
            "output": str(self.output_path) if self.output_path else None,
            "total": self.total,
            "counts": self.counts(),
            "colors": self.colors,
            "pages_without_text": self.pages_without_text,
            "matches": [
                {
                    "keyword": m.keyword,
                    "occurrence": m.occurrence,
                    "page": m.page,
                    "rects": [list(r) for r in m.rects],
                    "text": m.text,
                    "color": m.color,
                }
                for m in self.matches
            ],
        }


# --------------------------------------------------------------------------- #
# 内部ユーティリティ
# --------------------------------------------------------------------------- #
class _Glyph(NamedTuple):
    """1 文字。座標は表示上(回転適用後)のページ左上を原点とし、y は下向き(pt)。"""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    page: int  # 0 始まり


def _parse_color(color: str) -> str:
    """'FFFF00' / '#ffff00' / 'yellow' を pypdf 用の 6 桁小文字 16 進に変換する。"""
    c = str(color).strip().lstrip("#").lower()
    c = _NAMED_COLORS.get(c, c).lower()
    if len(c) != 6 or any(ch not in "0123456789abcdef" for ch in c):
        raise ValueError(
            f"色は 6 桁の 16 進数(例: FFFF00)か {sorted(_NAMED_COLORS)} のいずれかで指定してください: {color!r}"
        )
    return c


def _fold_unit(text: str, ignore_case: bool) -> str:
    """1 文字(単位)を照合用に正規化する。空白は除去する。"""
    s = unicodedata.normalize("NFKC", text)
    if ignore_case:
        s = s.casefold()
    return "".join(ch for ch in s if not ch.isspace())


def _fold_keyword(keyword: str, ignore_case: bool) -> str:
    s = unicodedata.normalize("NFKC", keyword)
    if ignore_case:
        s = s.casefold()
    return "".join(ch for ch in s if not ch.isspace())


def _read_source(src: PdfSource) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    if hasattr(src, "read"):
        return src.read()  # type: ignore[union-attr]  # ストリームの現在位置から読む
    return Path(src).read_bytes()  # type: ignore[arg-type]


KeywordsArg = Union[str, Mapping[str, Optional[str]], Iterable[Union[str, tuple]]]


def _normalize_keywords(keywords: KeywordsArg, default_color: str) -> list[tuple[str, str]]:
    """キーワード指定を ``[(キーワード, 色の 6 桁 16 進), ...]`` に正規化する。

    受け付ける形: ``"語"`` / ``["語", ...]`` / ``{"語": "blue", ...}`` /
    ``[("語", "blue"), "語2", ...]``。色が省略(または None)の語は ``default_color`` になる。
    同じ語の重複は除去する(順序は保持)。同じ語に異なる色を指定した場合は ValueError。
    """
    if isinstance(keywords, str):
        items: list = [keywords]
    elif isinstance(keywords, Mapping):
        items = list(keywords.items())
    else:
        items = list(keywords)

    default_hex = _parse_color(default_color)
    result: dict[str, str] = {}
    for item in items:
        if isinstance(item, str):
            kw, col = item, None
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            kw, col = item
        else:
            raise TypeError(f"キーワードは str か (str, 色) で指定してください: {item!r}")
        if not isinstance(kw, str):
            raise TypeError(f"キーワードは str で指定してください: {kw!r}")
        if col is not None and not isinstance(col, str):
            raise TypeError(f"色は str で指定してください: {col!r}")
        if not any(not ch.isspace() for ch in kw):
            raise ValueError(f"空白のみ、または空のキーワードは指定できません: {kw!r}")
        color_hex = _parse_color(col) if col is not None else default_hex
        if result.setdefault(kw, color_hex) != color_hex:
            raise ValueError(f"同じキーワードに異なる色が指定されています: {kw!r}")
    if not result:
        raise ValueError("キーワードが 1 つも指定されていません")
    return list(result.items())


class _MediaBox(NamedTuple):
    """ページ本来(回転前)の MediaBox。x0, y0 は左下、w, h は幅と高さ。"""

    x0: float
    y0: float
    w: float
    h: float


class _PageGeometry(NamedTuple):
    """表示座標 → 注釈用の元の PDF 座標への変換に必要なページ情報。"""

    mediabox: _MediaBox
    rotation: int  # 0 / 90 / 180 / 270
    disp_h: float  # 表示上のページ高さ(回転適用後)


def _displayed_to_user_space(geo: _PageGeometry, x0: float, top: float, x1: float, bottom: float) -> Rect:
    """表示座標(左上原点・y 下向き)の矩形を、注釈で使う元の PDF 座標系(左下原点)に変換する。"""
    mb = geo.mediabox
    pts = []
    for dx, dy in ((x0, top), (x1, bottom)):
        X, Y = dx, geo.disp_h - dy  # 表示ページの左下原点・y 上向き
        if geo.rotation == 90:
            u, v = mb.w - Y, X
        elif geo.rotation == 180:
            u, v = mb.w - X, mb.h - Y
        elif geo.rotation == 270:
            u, v = Y, mb.h - X
        else:
            u, v = X, Y
        pts.append((mb.x0 + u, mb.y0 + v))
    (xa, ya), (xb, yb) = pts
    return (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))


def _extract_glyphs(
    pdf_bytes: bytes, password: Optional[str], mediaboxes: Sequence[_MediaBox]
) -> tuple[list[list[_Glyph]], list[_PageGeometry]]:
    """pdfplumber でページごとの文字を、表示座標(左上原点)で取り出す。

    pdfplumber の座標は、ページ回転(/Rotate)を適用した表示上の座標である。さらに MediaBox の
    原点がずれた PDF では、x と y で補正の有無が異なる(pdfplumber の仕様)ため、
    ページの左端・上端(page.bbox)からの相対値にして、原点のずれを打ち消している。
    """
    pages: list[list[_Glyph]] = []
    geometries: list[_PageGeometry] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
        if len(pdf.pages) != len(mediaboxes):
            raise PdfHighlightError(
                f"ページ数が一致しません(pdfplumber={len(pdf.pages)}, pypdf={len(mediaboxes)})"
            )
        for index, page in enumerate(pdf.pages):
            rotation = int(page.rotation) % 360
            if rotation not in (0, 90, 180, 270):
                rotation = 0
            geometries.append(_PageGeometry(mediaboxes[index], rotation, float(page.height)))
            left, top0 = float(page.bbox[0]), float(page.bbox[1])
            pages.append(
                [
                    _Glyph(
                        c["text"],
                        float(c["x0"]) - left,
                        float(c["top"]) - top0,
                        float(c["x1"]) - left,
                        float(c["bottom"]) - top0,
                        index,
                    )
                    for c in page.chars
                ]
            )
            page.flush_cache()
    return pages, geometries


class _Sequence(NamedTuple):
    """検索対象。text の i 文字目は glyphs[owners[i]] 由来。"""

    text: str
    owners: list[int]
    glyphs: list[_Glyph]


def _build_sequence(pages: Sequence[Sequence[_Glyph]], ignore_case: bool) -> _Sequence:
    glyphs: list[_Glyph] = []
    chunks: list[str] = []
    owners: list[int] = []
    for page_glyphs in pages:
        # 結合文字(濁点だけが別グリフになっている等)は直前の文字に併合して正規化する
        units: list[tuple[str, int]] = []  # (文字, glyphs内のindex)
        for g in page_glyphs:
            idx = len(glyphs)
            glyphs.append(g)
            if units and g.text and all(unicodedata.combining(ch) for ch in g.text):
                prev_text, prev_idx = units[-1]
                units[-1] = (prev_text + g.text, prev_idx)
            else:
                units.append((g.text, idx))
        for text, idx in units:
            folded = _fold_unit(text, ignore_case)
            for ch in folded:
                chunks.append(ch)
                owners.append(idx)
    return _Sequence("".join(chunks), owners, glyphs)


def _line_rects(glyphs: Sequence[_Glyph], geo: _PageGeometry) -> list[Rect]:
    """読み順に並んだ文字を行ごとにまとめ、行ごとの外接矩形(PDF 座標)を返す(横書き前提)。

    行のまとめは表示座標で行い、最後に注釈用の座標系へ変換する。
    """
    lines: list[list[float]] = []  # [x0, top, x1, bottom, last_x0]
    for g in glyphs:
        cy = (g.top + g.bottom) / 2
        if lines:
            ln = lines[-1]
            if ln[1] <= cy <= ln[3] and g.x0 >= ln[4] - _LINE_X_TOLERANCE:
                ln[0] = min(ln[0], g.x0)
                ln[1] = min(ln[1], g.top)
                ln[2] = max(ln[2], g.x1)
                ln[3] = max(ln[3], g.bottom)
                ln[4] = g.x0
                continue
        lines.append([g.x0, g.top, g.x1, g.bottom, g.x0])
    return [_displayed_to_user_space(geo, ln[0], ln[1], ln[2], ln[3]) for ln in lines]


def _find_matches(
    seq: _Sequence,
    keyword: str,
    key: str,
    color: str,
    start_occurrence: int,
    geometries: Sequence[_PageGeometry],
) -> tuple[list[HighlightMatch], int]:
    """seq 内の key の全出現(重複しない)を探す。"""
    found: list[HighlightMatch] = []
    occurrence = start_occurrence
    pos = 0
    while (i := seq.text.find(key, pos)) != -1:
        pos = i + len(key)
        # 一致範囲の元グリフ(重複除去・順序保持)をページごとに分ける
        owner_ids = list(dict.fromkeys(seq.owners[i : i + len(key)]))
        by_page: dict[int, list[_Glyph]] = {}
        for gi in owner_ids:
            g = seq.glyphs[gi]
            by_page.setdefault(g.page, []).append(g)
        for page_index, gs in by_page.items():
            found.append(
                HighlightMatch(
                    keyword=keyword,
                    occurrence=occurrence,
                    page=page_index + 1,
                    rects=tuple(_line_rects(gs, geometries[page_index])),
                    text="".join(g.text for g in gs),
                    color=color,
                )
            )
        occurrence += 1
    return found, occurrence


def _add_annotation(writer: PdfWriter, match: HighlightMatch) -> None:
    quads: list[FloatObject] = []
    for x0, y0, x1, y1 in match.rects:
        # QuadPoints の順序: 左上, 右上, 左下, 右下(Acrobat 等の慣例)
        quads += [FloatObject(v) for v in (x0, y1, x1, y1, x0, y0, x1, y0)]
    bbox = (
        min(r[0] for r in match.rects),
        min(r[1] for r in match.rects),
        max(r[2] for r in match.rects),
        max(r[3] for r in match.rects),
    )
    annotation = Highlight(
        rect=bbox,
        quad_points=ArrayObject(quads),
        highlight_color=match.color,
        printing=True,
    )
    writer.add_annotation(page_number=match.page - 1, annotation=annotation)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (b.exists() and os.path.samefile(a, b))
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 公開 API
# --------------------------------------------------------------------------- #
def highlight_pdf(
    input_pdf: PdfSource,
    output_pdf: PdfSink,
    keywords: KeywordsArg,
    *,
    color: str = DEFAULT_COLOR,
    ignore_case: bool = False,
    cross_page: bool = False,
    password: Optional[str] = None,
    overwrite: bool = False,
) -> HighlightResult:
    """PDF 内の ``keywords`` をハイライトして保存する。

    Args:
        input_pdf: 入力 PDF。パス、バイト列、バイナリのファイルオブジェクトのいずれか。
            ファイルオブジェクトは現在位置から最後まで読み込む。
        output_pdf: 出力先。パス、書き込み可能なバイナリのファイルオブジェクト、
            または ``None``(``result.data`` にバイト列を返す)。
        keywords: 検索する文字列。次のいずれか。

            * 1 つの str、または str の反復可能オブジェクト(すべて ``color`` で付与)
            * ``{"語": "blue", "語2": "yellow"}`` のようなキーワード→色の dict
            * ``[("語", "blue"), "語2"]`` のような str と (str, 色) の混在リスト
              (色の無い語は ``color`` で付与)

            空白・改行は無視して照合するため、行をまたぐ文字列も検索できる。
            同じ語に異なる色を指定すると ValueError。
        color: 色を個別指定しなかったキーワードの色。``"FFFF00"`` / ``"#FFFF00"`` /
            ``"yellow"`` など(色名は yellow/green/cyan/pink/orange/red/blue/purple)。
        ignore_case: True なら英字の大文字小文字を区別しない。
        cross_page: True ならページをまたぐ文字列も検索する。
            既定(False)はページ単位で検索する。
        password: 暗号化 PDF のパスワード。出力は暗号化されない。
        overwrite: 出力先パスが既に存在する場合に上書きするか(既定: False)。
            入力ファイルと同じパスへの出力は常にエラー。

    Returns:
        HighlightResult: 一致情報。一致が 0 件でも例外にはならず、出力 PDF は作成される。

    Raises:
        ValueError: キーワードや色の指定が不正。
        TypeError: キーワードが str でない。
        FileNotFoundError: 入力ファイルが存在しない。
        FileExistsError: 出力先が存在し、``overwrite=False``。
        PdfPasswordError: パスワードが必要、または誤っている。
        PdfHighlightError: PDF として読み込めない場合など。
    """
    specs = _normalize_keywords(keywords, color)

    out_path: Optional[Path] = None
    if isinstance(output_pdf, (str, os.PathLike)):
        out_path = Path(output_pdf)
        if isinstance(input_pdf, (str, os.PathLike)) and _same_file(Path(input_pdf), out_path):
            raise ValueError("出力先に入力ファイルと同じパスは指定できません")
        if out_path.exists() and not overwrite:
            raise FileExistsError(f"出力先が既に存在します(上書きするには overwrite=True): {out_path}")

    pdf_bytes = _read_source(input_pdf)

    # --- pypdf で開く(暗号化の判定もここで行う) ---
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except PdfReadError as e:
        raise PdfHighlightError(f"PDF を読み込めません: {e}") from e
    if reader.is_encrypted and reader.decrypt(password or "") == 0:
        raise PdfPasswordError(
            "パスワードが誤っています" if password else "PDF が暗号化されています。パスワードを指定してください"
        )

    # --- 文字座標の取得 ---
    mediaboxes = [
        _MediaBox(
            float(p.mediabox.left), float(p.mediabox.bottom),
            float(p.mediabox.width), float(p.mediabox.height),
        )
        for p in reader.pages
    ]
    try:
        page_glyphs, geometries = _extract_glyphs(pdf_bytes, password, mediaboxes)
    except PdfHighlightError:
        raise
    except Exception as e:  # pdfminer は多様な例外を出すためここでまとめて変換する
        raise PdfHighlightError(f"PDF のテキストを解析できません: {e}") from e

    result = HighlightResult(keywords=tuple(kw for kw, _ in specs), colors=dict(specs))
    result.pages_without_text = [i + 1 for i, gs in enumerate(page_glyphs) if not gs]
    if result.pages_without_text:
        logger.warning(
            "テキスト層の無いページがあります(スキャン画像の可能性。OCR が必要): %s",
            result.pages_without_text,
        )

    # --- 検索 ---
    sequences = (
        [_build_sequence(page_glyphs, ignore_case)]
        if cross_page
        else [_build_sequence([gs], ignore_case) for gs in page_glyphs]
    )

    for kw, kw_color in specs:
        key = _fold_keyword(kw, ignore_case)
        occurrence = 0
        for seq in sequences:
            found, occurrence = _find_matches(seq, kw, key, kw_color, occurrence, geometries)
            result.matches.extend(found)
        logger.debug("keyword=%r: %d 件", kw, occurrence)

    # --- 注釈の書き込み ---
    writer = PdfWriter(clone_from=reader)
    for m in result.matches:
        _add_annotation(writer, m)

    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()

    if out_path is not None:
        tmp = out_path.with_name(out_path.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, out_path)
        finally:
            if tmp.exists():
                tmp.unlink()
        result.output_path = out_path
    elif output_pdf is None:
        result.data = data
    else:
        output_pdf.write(data)  # type: ignore[union-attr]

    logger.info("ハイライト完了: %s", result.counts())
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_MATCH = 3  # --fail-on-no-match 指定時のみ。(2 は argparse の使い方エラー)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf_highlight",
        description="PDF 内の指定文字列をハイライトします(改行またぎ・日本語対応、横書きのみ)。",
    )
    p.add_argument("input", help="入力 PDF ファイル")
    p.add_argument("keywords", nargs="*", metavar="keyword",
                   help="ハイライトする文字列(複数可)。色は -c で指定した色")
    p.add_argument("-s", "--highlight", nargs=2, action="append", default=[],
                   metavar=("COLOR", "KEYWORD"),
                   help="色を指定してハイライトする文字列。何度でも指定可(例: -s blue 吾輩 -s yellow 猫)")
    p.add_argument("-k", "--keywords-file", metavar="FILE",
                   help="キーワードを 1 行 1 語で書いたテキストファイル(UTF-8)。空行は無視。"
                        "「キーワード<TAB>色」の行はその色で付与")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="出力 PDF。省略時は <入力名>_highlighted.pdf")
    p.add_argument("-c", "--color", default=DEFAULT_COLOR,
                   help="色を個別指定しないキーワードの色。16 進 6 桁か色名(既定: FFFF00)")
    p.add_argument("-i", "--ignore-case", action="store_true", help="英字の大文字小文字を区別しない")
    p.add_argument("--cross-page", action="store_true", help="ページをまたぐ文字列も検索する")
    p.add_argument("--password", help="暗号化 PDF のパスワード")
    p.add_argument("--overwrite", action="store_true", help="出力先が存在する場合に上書きする")
    p.add_argument("--fail-on-no-match", action="store_true",
                   help="一致が 1 件も無ければ終了コード 3 で終了する(出力 PDF は作成される)")
    p.add_argument("--json", action="store_true", help="結果を JSON で標準出力に出す")
    p.add_argument("-v", "--verbose", action="store_true", help="詳細ログを標準エラーに出す")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _load_keywords(args: argparse.Namespace) -> list[tuple[str, Optional[str]]]:
    """CLI 引数から (キーワード, 色 or None) のリストを作る。None は -c の色を使う。"""
    specs: list[tuple[str, Optional[str]]] = [(kw, None) for kw in args.keywords]
    specs += [(kw, col) for col, kw in args.highlight]
    if args.keywords_file:
        text = Path(args.keywords_file).read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            if not line.strip():
                continue
            kw, sep, col = line.partition("\t")
            specs.append((kw, (col.strip() or None) if sep else None))
    return specs


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI エントリポイント。終了コードを返す(sys.exit は呼ばない)。

    終了コード: 0=成功 / 1=エラー / 2=引数エラー(argparse) / 3=一致なし(--fail-on-no-match)
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Windows のコンソール(cp932 等)で表現できない文字があっても落ちないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        keywords = _load_keywords(args)
        if not keywords:
            parser.error("キーワードを 1 つ以上指定してください(引数または --keywords-file)")
        src = Path(args.input)
        output = Path(args.output) if args.output else src.with_name(f"{src.stem}_highlighted.pdf")
        result = highlight_pdf(
            src,
            output,
            keywords,
            color=args.color,
            ignore_case=args.ignore_case,
            cross_page=args.cross_page,
            password=args.password,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, TypeError, PdfHighlightError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
    else:
        print(f"出力: {result.output_path}")
        for kw, n in result.counts().items():
            print(f"  {kw!r} ({result.colors[kw]}): {n} 件")
        if result.pages_without_text:
            print(f"注意: テキスト層の無いページ(OCR が必要): {result.pages_without_text}", file=sys.stderr)

    if args.fail_on_no_match and result.total == 0:
        return EXIT_NO_MATCH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

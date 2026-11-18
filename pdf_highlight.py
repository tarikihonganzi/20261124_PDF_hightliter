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
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, NamedTuple, Optional, Sequence, Union

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

__version__ = "0.3.0"

__all__ = [
    "HighlightMatch",
    "HighlightResult",
    "PdfHighlightError",
    "PdfPasswordError",
    "PdfPasswordRequired",
    "PdfPasswordWrong",
    "PdfRefError",
    "PdfTimeoutError",
    "PdfDocument",
    "Candidate",
    "SearchResult",
    "HighlightReport",
    "ReadResult",
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


class PdfPasswordRequired(PdfPasswordError):
    """暗号化 PDF だが、パスワードが指定されていない。"""


class PdfPasswordWrong(PdfPasswordError):
    """パスワードが誤っている。"""


class PdfRefError(PdfHighlightError):
    """位置指定 ``ref`` の形式が不正。"""


class PdfTimeoutError(PdfHighlightError):
    """処理時間の上限を超えた。"""


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
        for index in range(len(pdf.pages)):
            glyphs, geo = _extract_page(pdf, index, mediaboxes[index])
            pages.append(glyphs)
            geometries.append(geo)
    return pages, geometries


def _extract_page(pdf: "pdfplumber.PDF", index: int, mediabox: _MediaBox) -> tuple[list[_Glyph], _PageGeometry]:
    """1 ページ分の文字と、座標変換用のページ情報を取り出す(``_extract_glyphs`` の 1 ページ版)。"""
    page = pdf.pages[index]
    rotation = int(page.rotation) % 360
    if rotation not in (0, 90, 180, 270):
        rotation = 0
    geo = _PageGeometry(mediabox, rotation, float(page.height))
    left, top0 = float(page.bbox[0]), float(page.bbox[1])
    glyphs = [
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
    page.flush_cache()
    return glyphs, geo


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
    return [
        _displayed_to_user_space(
            geo,
            min(g.x0 for g in ln), min(g.top for g in ln), max(g.x1 for g in ln), max(g.bottom for g in ln),
        )
        for ln in _group_lines(glyphs)
    ]


def _group_lines(glyphs: Sequence[_Glyph]) -> list[list[_Glyph]]:
    """読み順に並んだ文字を行ごとにまとめる(横書き前提)。

    直前の行と縦位置が重なり、かつ x が戻っていなければ同じ行。矩形の計算と本文の取得で共通。
    """
    lines: list[list[_Glyph]] = []
    bounds: list[list[float]] = []  # 行ごとの [top, bottom, 最後の x0]
    for g in glyphs:
        cy = (g.top + g.bottom) / 2
        if lines:
            b = bounds[-1]
            if b[0] <= cy <= b[1] and g.x0 >= b[2] - _LINE_X_TOLERANCE:
                lines[-1].append(g)
                b[0], b[1], b[2] = min(b[0], g.top), max(b[1], g.bottom), g.x0
                continue
        lines.append([g])
        bounds.append([g.top, g.bottom, g.x0])
    return lines


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


def _add_annotation(
    writer: PdfWriter,
    match: HighlightMatch,
    *,
    name: Optional[str] = None,
    author: Optional[str] = None,
    comment: Optional[str] = None,
) -> None:
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
    if name:
        annotation[NameObject("/NM")] = TextStringObject(name)
    if author:
        annotation[NameObject("/T")] = TextStringObject(author)
    if comment:
        annotation[NameObject("/Contents")] = TextStringObject(comment)
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
        if password:
            raise PdfPasswordWrong("パスワードが誤っています")
        raise PdfPasswordRequired("PDF が暗号化されています。パスワードを指定してください")

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
# PdfDocument: 検索・位置指定(ref)・書き込み
#
# LLM の Tool call から使うための API(仕様: docs/SPEC-llm-tool-support.md)。
# 呼び出し間で状態を持たない: PdfDocument は 1 回の呼び出しの間だけ使い、結果は
# 「引数と PDF の内容」だけで決まる。検索結果の位置は、自己完結の ref で表す。
# --------------------------------------------------------------------------- #
_FUZZY_MIN_LEN = 4  # 正規化後の長さがこれ未満の語は、あいまい一致をしない(ノイズが多い)
_FUZZY_MARGIN = 5  # select="best" で、最良と 2 位の点差がこれ未満なら ambiguous(書き込まない)
_SUGGEST_MIN_SCORE = 70  # 一致 0 件のときの「もしかして」候補の下限
_MAX_QUERY_CHARS = 500
_MAX_COMMENT_CHARS = 500
_DISPLAY_MAX = 82  # 候補の表示用文字列の最大長(先頭 40 + … + 末尾 40)


def _find_all(text: str, key: str, overlap: bool) -> list[int]:
    """text 内の key の出現位置。overlap=True なら重なる出現(1 文字ずつずらす)も数える。"""
    out: list[int] = []
    pos = 0
    while (i := text.find(key, pos)) != -1:
        out.append(i)
        pos = i + 1 if overlap else i + len(key)
    return out


def _clean(text: str) -> str:
    """表示用に整える: 制御文字を除き、連続する空白を 1 つにする。

    PDF 由来の文字列は信頼できない入力なので、LLM に返す前に必ずこれを通す。
    """
    text = "".join(ch for ch in text if ch.isspace() or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", text).strip()


def _shorten(text: str, limit: int = _DISPLAY_MAX) -> str:
    """limit 文字を超えるとき、先頭と末尾を残して中略する(結果は limit 文字以内)。"""
    if len(text) <= limit:
        return text
    half = max((limit - 1) // 2, 1)
    return text[:half] + "…" + text[-half:]


_WORD_GAP_RATIO = 0.2  # 文字間の隙間が文字の高さのこの割合を超えたら、語の区切りとして空白を補う


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def _line_text(line: Sequence[_Glyph]) -> str:
    """1 行分の文字を本文にする。

    PDF によっては語の間に空白の文字が無く、位置の隙間だけで表している。そのため隙間が大きいところには
    空白を補う(日本語など幅の広い文字の間には補わない)。
    """
    out: list[str] = []
    prev: Optional[_Glyph] = None
    for g in line:
        if (prev is not None and prev.text and g.text and not prev.text[-1].isspace() and not g.text[0].isspace()
                and not _is_wide(prev.text[-1]) and not _is_wide(g.text[0])
                and g.x0 - prev.x1 > _WORD_GAP_RATIO * max(g.bottom - g.top, 0.0)):
            out.append(" ")
        out.append(g.text)
        prev = g
    return "".join(out)


def _join_glyphs(glyphs: Sequence[_Glyph]) -> str:
    """文字の列を 1 本の文字列にする。行のつなぎ目は、英文など幅の狭い文字どうしのときだけ空白を入れる
    (日本語は行をまたいでも空白を入れない)。制御文字の除去などは ``_clean`` で行う。"""
    out = ""
    for ln in _group_lines(glyphs):
        text = _line_text(ln)
        if out and text and not _is_wide(out[-1]) and not _is_wide(text[0]) \
                and not out[-1].isspace() and not text[0].isspace():
            out += " "
        out += text
    return out


def _span_text(glyphs: Sequence[_Glyph], ids: Sequence[int]) -> str:
    """一致範囲の表示用文字列。範囲の途中にある空白も含める(照合では無視される空白)。"""
    return _clean(_join_glyphs(glyphs[ids[0] : ids[-1] + 1]))


def _fuzzy_spans(text: str, key: str, min_score: int) -> list[tuple[int, int, int]]:
    """text 内で key に近い範囲 ``(開始, 終了, 類似度 0-100)`` を、類似度の高い順に返す。

    窓に分けて rapidfuzz で探し、範囲の両端のうち key と対応しない文字を除いてから類似度を
    計算し直す(素の結果は端に余計な文字が付くため)。重なる範囲の整理は呼び出し側で行う。
    """
    try:
        from rapidfuzz import fuzz
        from rapidfuzz.distance import Levenshtein
    except ImportError as e:  # pragma: no cover - 環境依存
        raise PdfHighlightError("あいまい一致には rapidfuzz が必要です(pip install rapidfuzz)") from e

    n, m = len(text), len(key)
    if m < _FUZZY_MIN_LEN or n < m:
        return []
    if fuzz.partial_ratio(key, text, score_cutoff=min_score) < min_score:
        return []  # このページに近い箇所は無い(安価な足切り)
    window = max(int(m * 1.5), m + 4)
    stride = max(m // 2, 1)
    starts = list(range(0, max(n - window, 0) + 1, stride))
    if starts[-1] + window < n:
        starts.append(n - window)
    found: dict[tuple[int, int], int] = {}
    for st in starts:
        al = fuzz.partial_ratio_alignment(key, text[st : st + window], score_cutoff=min_score)
        if al is None or al.score < min_score:
            continue
        s, e = st + al.dest_start, st + al.dest_end
        equal = [op for op in Levenshtein.opcodes(key, text[s:e]) if op.tag == "equal"]
        if not equal:
            continue
        s, e = s + equal[0].dest_start, s + equal[-1].dest_end
        score = int(round(fuzz.ratio(key, text[s:e])))
        if score >= min_score and score > found.get((s, e), -1):
            found[(s, e)] = score
    return sorted(((s, e, sc) for (s, e), sc in found.items()), key=lambda x: (-x[2], x[0]))


class _Hit(NamedTuple):
    """1 件の一致。照合列上の範囲と、元グリフ(ページ内の添字)で表す。"""

    page: int  # 0 始まり
    fs: int  # 照合列上の範囲 [fs, fe)
    fe: int
    ids: tuple[int, ...]  # 元グリフの添字(読み順・重複なし)
    score: int
    match: str  # "exact" / "fuzzy"


@dataclass(frozen=True)
class Candidate:
    """検索候補 1 件。``ref`` はそのまま highlight の項目に渡せる(自己完結の位置指定)。"""

    ref: dict
    query: int  # queries の添字
    page: int  # 1 始まり
    score: int
    match: str
    text: str  # PDF 上の実際の文字列(表示用。長いときは省略)
    length: int
    before: str
    after: str
    position: str  # "upper" / "middle" / "lower"
    glyph_ids: tuple[int, ...] = ()  # 内部用(テスト・デバッグ)。to_dict には含めない

    def to_dict(self, brief: bool = False) -> dict:
        d = {"ref": self.ref, "page": self.page, "score": self.score, "text": self.text,
             "before": self.before, "after": self.after}
        if not brief:
            d.update({"query": self.query, "match": self.match, "length": self.length,
                      "position": self.position})
        return d


@dataclass
class SearchResult:
    candidates: list[Candidate] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)  # [{index, found, best_score}]
    total_found: int = 0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    searched_pages: int = 0  # 検索対象にしたページ数
    empty_pages: list[int] = field(default_factory=list)  # そのうちテキスト層の無いページ(1 始まり)


@dataclass
class ReadResult:
    text: str
    pages: list[int]  # 対象にしたページ(1 始まり、昇順)
    total_chars: int  # 選んだページ全体の文字数
    offset: int
    next_offset: Optional[int] = None  # 続きがあるとき、次に渡す offset
    empty_pages: list[int] = field(default_factory=list)  # テキスト層の無いページ


@dataclass
class HighlightReport:
    results: list[dict] = field(default_factory=list)  # 項目ごとの結果(status 付き)
    applied: int = 0  # 注釈を付けた項目数
    annotations: int = 0  # 追加した注釈の数
    data: Optional[bytes] = None  # 書き出した PDF。1 件も付けなかった / dry_run のときは None
    run_id: str = ""
    fingerprint: str = ""
    warnings: list[str] = field(default_factory=list)


class PdfDocument:
    """1 回の呼び出しの間だけ使う PDF の窓口。文字の抽出は、必要になったページだけ行う。"""

    def __init__(self, source: PdfSource, password: Optional[str] = None, *, deadline: Optional[float] = None):
        data = _read_source(source)
        self._data = data
        self._password = password
        self.deadline = deadline  # time.monotonic() の値。超えると PdfTimeoutError
        self.fingerprint = hashlib.sha256(data).hexdigest()[:12]
        try:
            reader = PdfReader(io.BytesIO(data))
        except PdfReadError as e:
            raise PdfHighlightError(f"PDF を読み込めません: {e}") from e
        except Exception as e:
            raise PdfHighlightError(f"PDF を読み込めません: {e}") from e
        self.encrypted = bool(reader.is_encrypted)
        if self.encrypted and reader.decrypt(password or "") == 0:
            if password:
                raise PdfPasswordWrong("パスワードが誤っています")
            raise PdfPasswordRequired("PDF が暗号化されています。パスワードを指定してください")
        self._reader = reader
        try:
            self._mediaboxes = [
                _MediaBox(float(p.mediabox.left), float(p.mediabox.bottom),
                          float(p.mediabox.width), float(p.mediabox.height))
                for p in reader.pages
            ]
        except Exception as e:
            raise PdfHighlightError(f"PDF のページ情報を読み込めません: {e}") from e
        self.page_count = len(self._mediaboxes)
        self._plumber: Optional["pdfplumber.PDF"] = None
        self._pages: dict[int, tuple[list[_Glyph], _PageGeometry]] = {}
        self._seqs: dict[tuple[int, bool], _Sequence] = {}  # 1 回の呼び出しの中だけのメモ

    @classmethod
    def open(cls, source: PdfSource, password: Optional[str] = None, *, deadline: Optional[float] = None) -> "PdfDocument":
        return cls(source, password, deadline=deadline)

    def close(self) -> None:
        if self._plumber is not None:
            self._plumber.close()
            self._plumber = None

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 内部: ページの抽出 ---------------------------------------------- #
    def _check_deadline(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise PdfTimeoutError("処理時間の上限を超えました")

    def _page(self, index: int, keep: bool = True) -> tuple[list[_Glyph], _PageGeometry]:
        """ページの文字と座標情報。``keep=False`` なら、抽出結果をメモに残さない(全ページを順に走査する用)。"""
        self._check_deadline()
        if index not in self._pages:
            try:
                if self._plumber is None:
                    self._plumber = pdfplumber.open(io.BytesIO(self._data), password=self._password)
                    if len(self._plumber.pages) != self.page_count:
                        raise PdfHighlightError(
                            f"ページ数が一致しません(pdfplumber={len(self._plumber.pages)}, pypdf={self.page_count})"
                        )
                page = _extract_page(self._plumber, index, self._mediaboxes[index])
            except PdfHighlightError:
                raise
            except Exception as e:  # pdfminer は多様な例外を出すためここでまとめて変換する
                raise PdfHighlightError(f"PDF のテキストを解析できません: {e}") from e
            if not keep:
                return page
            self._pages[index] = page
        return self._pages[index]

    def _seq(self, index: int, ignore_case: bool) -> _Sequence:
        key = (index, ignore_case)
        if key not in self._seqs:
            self._seqs[key] = _build_sequence([self._page(index)[0]], ignore_case)
        return self._seqs[key]

    def _page_indexes(self, pages: Optional[Iterable[int]]) -> list[int]:
        if pages is None:
            return list(range(self.page_count))
        out: list[int] = []
        for p in pages:
            if isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= self.page_count:
                raise ValueError(f"ページ番号は 1〜{self.page_count} の整数で指定してください: {p!r}")
            out.append(p - 1)
        return sorted(set(out))

    def scan_pages(self, heads: bool = False, heads_limit: int = 200) -> tuple[list[int], list[dict], bool]:
        """全ページを順に走査して、(テキスト層の無いページ, 各ページの先頭行, 先頭行が省略されたか)を返す。

        全ページを抽出するため、大きな PDF では時間がかかる(結果はメモに残さず、メモリは増やさない)。
        先頭行は、そのページの最初の行(見出しやヘッダのことが多い)。目次が無い PDF で、読むページを選ぶ手がかりになる。
        """
        empty: list[int] = []
        head_list: list[dict] = []
        truncated = False
        for i in range(self.page_count):
            glyphs, _ = self._page(i, keep=i in self._pages)
            if not glyphs:
                empty.append(i + 1)
                continue
            if heads:
                first = next((t for ln in _group_lines(glyphs) if (t := _clean(_line_text(ln)))), "")
                if first:
                    if len(head_list) < heads_limit:
                        head_list.append({"page": i + 1, "head": _shorten(first, 40)})
                    else:
                        truncated = True
        return empty, head_list, truncated

    def pages_without_text(self) -> list[int]:
        """テキスト層の無いページ(1 始まり)。全ページを抽出するため、大きな PDF では時間がかかる。"""
        return self.scan_pages()[0]

    def outline(self, limit: int = 200) -> tuple[list[dict], bool]:
        """PDF の目次(しおり)。``[{title, page, level}]`` と、``limit`` で省略したか。無ければ空。"""
        entries: list[dict] = []
        truncated = False

        def walk(items, level: int) -> None:
            nonlocal truncated
            for it in items:
                if truncated:
                    return
                if isinstance(it, list):
                    walk(it, level + 1)
                    continue
                try:
                    page = self._reader.get_destination_page_number(it) + 1
                except Exception:
                    continue  # 行き先が壊れている項目は飛ばす
                title = _shorten(_clean(str(getattr(it, "title", "") or "")), 60)
                if not title or not 1 <= page <= self.page_count:
                    continue
                if len(entries) >= limit:
                    truncated = True
                    return
                entries.append({"title": title, "page": page, "level": level})

        try:
            walk(self._reader.outline, 1)
        except Exception:
            return [], False
        return entries, truncated

    def read_text(self, pages: Iterable[int], *, max_chars: int = 4000, offset: int = 0) -> "ReadResult":
        """指定ページの本文を、行ごとに改行した文字列で返す。全文を一度に返さず、文字数の予算で区切る。

        各ページの先頭に ``[p.N]`` の行を付ける。``max_chars`` に収まらないときは行の切れ目で区切り、
        ``next_offset`` を返す(続きは、同じ ``pages`` と ``offset=next_offset`` で取る)。
        ページの並びは昇順・重複なしにそろえる。
        """
        idx = self._page_indexes(pages)
        if not idx:
            raise ValueError("pages は 1 つ以上指定してください")
        if not 1 <= max_chars <= 20000:
            raise ValueError("max_chars は 1〜20000 で指定してください")
        parts: list[str] = []
        empty: list[int] = []
        for p in idx:
            glyphs, _ = self._page(p, keep=p in self._pages)
            lines = [t for ln in _group_lines(glyphs) if (t := _clean(_line_text(ln)))]
            if lines:
                parts.append(f"[p.{p + 1}]\n" + "\n".join(lines))
            else:
                empty.append(p + 1)
                parts.append(f"[p.{p + 1}] (テキストなし)")
        full = "\n".join(parts)
        total = len(full)
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= total:
            raise ValueError(f"offset は 0〜{total}(選んだページの本文の長さ)で指定してください")
        end = min(offset + max_chars, total)
        if end < total:
            cut = full.rfind("\n", offset, end)
            if cut >= offset + max_chars * 0.6:
                end = cut + 1  # 行の途中で切らない(予算の 6 割以上を使えるときだけ)
        return ReadResult(
            text=full[offset:end], pages=[p + 1 for p in idx], total_chars=total, offset=offset,
            next_offset=end if end < total else None, empty_pages=empty,
        )

    def highlight_counts(self) -> dict[str, int]:
        """既存のハイライト注釈の数。``ours`` はこのモジュールが付けたもの(/NM が pdfhl: で始まる)。"""
        total = ours = 0
        for page in self._reader.pages:
            for ref in page.get("/Annots", None) or []:
                a = ref.get_object()
                if a.get("/Subtype") == "/Highlight":
                    total += 1
                    if str(a.get("/NM", "")).startswith("pdfhl:"):
                        ours += 1
        return {"total": total, "ours": ours}

    # ---- 検索 -------------------------------------------------------------- #
    def _hits(self, page: int, key: str, ignore_case: bool, mode: str, min_score: int) -> list[_Hit]:
        seq = self._seq(page, ignore_case)
        n = len(key)
        hits = [
            _Hit(page, i, i + n, tuple(dict.fromkeys(seq.owners[i : i + n])), 100, "exact")
            for i in _find_all(seq.text, key, overlap=False)
        ]
        if mode == "fuzzy" and n >= _FUZZY_MIN_LEN:
            taken = [(h.fs, h.fe) for h in hits]
            for s, e, score in _fuzzy_spans(seq.text, key, min_score):
                if any(s < te and ts < e for ts, te in taken):
                    continue  # 既に採用した範囲(exact を含む)と重なるものは捨てる
                taken.append((s, e))
                hits.append(_Hit(page, s, e, tuple(dict.fromkeys(seq.owners[s:e])), score, "fuzzy"))
        return hits

    def _make_ref(self, hit: _Hit, raw: str) -> Optional[dict]:
        """hit を指す ref を作る。解決規則で同じ範囲に戻ることを検証し、戻らなければ None。"""
        key = _fold_keyword(raw, False)
        if not key:
            return None
        seq = self._seq(hit.page, False)
        for nth, i in enumerate(_find_all(seq.text, key, overlap=True), 1):
            if tuple(dict.fromkeys(seq.owners[i : i + len(key)])) == hit.ids:
                return {"text": raw, "page": hit.page + 1, "nth": nth}
        return None

    def _candidate(self, hit: _Hit, query: int, context_chars: int) -> Optional[Candidate]:
        glyphs, geo = self._page(hit.page)
        raw = _span_text(glyphs, hit.ids)
        ref = self._make_ref(hit, raw)
        if ref is None:
            return None
        first, last, n = hit.ids[0], hit.ids[-1], context_chars
        before = after = ""
        if n > 0:
            before = _clean(_join_glyphs(glyphs[max(0, first - 3 * n - 3) : first]))[-n:]
            after = _clean(_join_glyphs(glyphs[last + 1 : last + 1 + 3 * n + 3]))[:n]
        ratio = glyphs[first].top / geo.disp_h if geo.disp_h else 0.0
        position = "upper" if ratio < 1 / 3 else "middle" if ratio < 2 / 3 else "lower"
        return Candidate(
            ref=ref, query=query, page=hit.page + 1, score=hit.score, match=hit.match,
            text=_shorten(raw), length=len(raw), before=before, after=after, position=position,
            glyph_ids=hit.ids,
        )

    def search(
        self,
        queries: Union[str, Sequence[str]],
        *,
        mode: str = "exact",
        min_score: int = 80,
        ignore_case: bool = False,
        pages: Optional[Iterable[int]] = None,
        limit: int = 10,
        context_chars: int = 20,
    ) -> SearchResult:
        """文字列を検索し、文脈付きの候補を返す(PDF は変更しない)。

        mode="exact": 正規化(NFKC・空白除去)後の部分文字列一致。
        mode="fuzzy": exact に加え、誤字・一部欠落など近い箇所も返す(``min_score`` 以上)。
        """
        if mode not in ("exact", "fuzzy"):
            raise ValueError(f"mode は exact か fuzzy を指定してください: {mode!r}")
        qs = [queries] if isinstance(queries, str) else list(queries)
        if not qs:
            raise ValueError("検索する文字列が 1 つも指定されていません")
        for q in qs:
            if not isinstance(q, str):
                raise TypeError(f"検索する文字列は str で指定してください: {q!r}")
            if not any(not ch.isspace() for ch in q):
                raise ValueError(f"空白のみ、または空の文字列は指定できません: {q!r}")
            if len(q) > _MAX_QUERY_CHARS:
                raise ValueError(f"検索する文字列は {_MAX_QUERY_CHARS} 文字以内にしてください")
        if not 0 < limit <= 50:
            raise ValueError("limit は 1〜50 で指定してください")
        page_idx = self._page_indexes(pages)

        result = SearchResult()
        for qi, q in enumerate(qs):
            key = _fold_keyword(q, ignore_case)
            if mode == "fuzzy" and len(key) < _FUZZY_MIN_LEN:
                result.warnings.append(f"fuzzy_skipped_short_query: 「{_shorten(q, 20)}」は短いため完全一致のみ")
            hits: list[_Hit] = []
            for p in page_idx:
                hits += self._hits(p, key, ignore_case, mode, min_score)
            hits.sort(key=lambda h: (-h.score, h.page, h.fs))
            result.queries.append({"index": qi, "found": len(hits), "best_score": hits[0].score if hits else 0})
            result.total_found += len(hits)
            if len(hits) > limit:
                result.truncated = True
            for h in hits[:limit]:
                cand = self._candidate(h, qi, context_chars)
                if cand is None:
                    result.warnings.append("ref_unresolvable: 位置を一意に指定できない候補を 1 件除きました")
                else:
                    result.candidates.append(cand)
        empty = [p + 1 for p in page_idx if not self._page(p)[0]]
        result.searched_pages, result.empty_pages = len(page_idx), empty
        if empty:
            result.warnings.append(f"no_text_layer: テキスト層の無いページ(OCR が必要): {empty[:20]}")
        return result

    # ---- ref ---------------------------------------------------------------- #
    def resolve_ref(self, ref: Mapping) -> Optional[_Hit]:
        """ref を、PDF 上の位置に解決する。解決できなければ None(ファイルが変わった場合など)。

        解決規則(照合設定に依存しない): 大小区別あり・NFKC・空白無視の exact で、
        ``page`` のページ内の全出現(重なりを許す、読み順)のうち ``nth`` 番目。
        """
        if not isinstance(ref, Mapping):
            raise PdfRefError("ref はオブジェクトで指定してください")
        if "start" in ref or "end" in ref:
            raise PdfRefError("start/end 形式の ref には未対応です(text 形式を使ってください)")
        text, page, nth = ref.get("text"), ref.get("page"), ref.get("nth")
        if not isinstance(text, str) or not any(not ch.isspace() for ch in text):
            raise PdfRefError("ref.text は空でない文字列で指定してください")
        for name, v in (("page", page), ("nth", nth)):
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise PdfRefError(f"ref.{name} は 1 以上の整数で指定してください: {v!r}")
        if page > self.page_count:
            return None
        key = _fold_keyword(text, False)
        seq = self._seq(page - 1, False)
        occ = _find_all(seq.text, key, overlap=True)
        if nth > len(occ):
            return None
        i = occ[nth - 1]
        return _Hit(page - 1, i, i + len(key), tuple(dict.fromkeys(seq.owners[i : i + len(key)])), 100, "exact")

    # ---- 書き込み ------------------------------------------------------------ #
    def _plan_item(
        self, item: Mapping, ignore_case: bool, max_matches: int
    ) -> tuple[str, dict, list[_Hit]]:
        """項目 1 つを解決する。戻り値は (status, 追加情報, 一致)。status が "ready" のときだけ書き込む。"""
        if not isinstance(item, Mapping):
            raise TypeError(f"items の各要素はオブジェクトで指定してください: {item!r}")
        if ("ref" in item) == ("text" in item):
            raise ValueError("各項目には ref か text のどちらか一方だけを指定してください")

        if "ref" in item:
            hit = self.resolve_ref(item["ref"])
            label = item["ref"].get("text", "") if isinstance(item["ref"], Mapping) else ""
            if hit is None:
                return "not_found", self._suggest(label, ignore_case, None), []
            return "ready", {}, [hit]

        text = item["text"]
        if not isinstance(text, str) or not any(not ch.isspace() for ch in text):
            raise ValueError("text は空でない文字列で指定してください")
        if len(text) > _MAX_QUERY_CHARS:
            raise ValueError(f"text は {_MAX_QUERY_CHARS} 文字以内にしてください")
        mode = item.get("mode", "exact")
        if mode not in ("exact", "fuzzy"):
            raise ValueError(f"mode は exact か fuzzy を指定してください: {mode!r}")
        pages = item.get("pages")
        page_idx = self._page_indexes(pages)
        key = _fold_keyword(text, ignore_case)
        occurrence = item.get("occurrence")

        if mode == "exact":
            min_score = 100
        else:
            if occurrence is not None:
                raise ValueError("occurrence は mode=exact のときだけ指定できます")
            min_score = item.get("min_score", 85)
            if isinstance(min_score, bool) or not isinstance(min_score, int) or not 70 <= min_score <= 100:
                raise ValueError("min_score は 70〜100 の整数で指定してください")
        select = item.get("select", "best")
        if select not in ("best", "all"):
            raise ValueError(f"select は best か all を指定してください: {select!r}")

        hits: list[_Hit] = []
        for p in page_idx:
            hits += self._hits(p, key, ignore_case, mode, min_score)

        if mode == "exact":
            if occurrence is not None:
                if (not isinstance(occurrence, list) or not occurrence
                        or any(isinstance(o, bool) or not isinstance(o, int) or o < 1 for o in occurrence)):
                    raise ValueError("occurrence は 1 以上の整数の配列で指定してください")
                hits = [hits[o - 1] for o in sorted(set(occurrence)) if o <= len(hits)]
        else:
            hits.sort(key=lambda h: (-h.score, h.page, h.fs))
            if select == "best" and hits:
                if len(hits) >= 2 and hits[0].score - hits[1].score < _FUZZY_MARGIN:
                    cands = [c for h in hits[:5] if (c := self._candidate(h, 0, 15))]
                    return "ambiguous", {
                        "hint": "最良候補と 2 位の差が小さいため書き込んでいません。candidates の ref で 1 か所を指定してください",
                        "candidates": [c.to_dict(brief=True) for c in cands],
                    }, []
                hits = hits[:1]

        if not hits:
            return "not_found", self._suggest(text, ignore_case, pages), []
        if len(hits) > max_matches:
            dist = Counter(h.page + 1 for h in hits)
            return "too_many_matches", {
                "count": len(hits),
                "pages_distribution": {str(p): dist[p] for p in sorted(dist)[:20]},
                "hint": "pages か occurrence で絞るか、より長い語を指定してください",
            }, []
        return "ready", {}, hits

    def _suggest(self, text: str, ignore_case: bool, pages: Optional[Iterable[int]]) -> dict:
        """一致 0 件のときの「もしかして」候補(近い箇所の上位 3 件)。書き込みには使われない。"""
        info: dict = {}
        if len(_fold_keyword(text, ignore_case)) >= _FUZZY_MIN_LEN:
            res = self.search([text], mode="fuzzy", min_score=_SUGGEST_MIN_SCORE, ignore_case=ignore_case,
                              pages=pages, limit=3, context_chars=15)
            if res.candidates:
                info["suggestions"] = [c.to_dict(brief=True) for c in res.candidates]
        if "suggestions" not in info:
            info["hint"] = "一致する箇所も、近い箇所も見つかりませんでした。pdf_search で語を確かめてください"
        return info

    def highlight(
        self,
        items: Sequence[Mapping],
        *,
        color: str = DEFAULT_COLOR,
        ignore_case: bool = True,
        max_matches_per_item: int = 50,
        max_highlights: int = 200,
        author: str = "pdf_highlight",
        dry_run: bool = False,
    ) -> HighlightReport:
        """項目ごとにハイライトを付けた PDF(bytes)を作る。入力は変更しない。

        項目は ``{"ref": {...}}``(pdf_search の候補)か ``{"text": "...", ...}``。
        一致が多すぎる項目や曖昧な項目は書き込まず、理由を ``status`` で返す(部分成功を許す)。
        """
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence) or not items:
            raise ValueError("items は 1 件以上の配列で指定してください")
        default_hex = _parse_color(color)
        run_id = hashlib.sha256(
            json.dumps([self.fingerprint, default_hex, ignore_case, list(items)], sort_keys=True,
                       ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:8]

        report = HighlightReport(run_id=run_id, fingerprint=self.fingerprint)
        plan: list[tuple[int, list[_Hit], str, Optional[str]]] = []
        used = 0
        for idx, item in enumerate(items):
            status, info, hits = self._plan_item(item, ignore_case, max_matches_per_item)
            item_hex = _parse_color(item["color"]) if item.get("color") else default_hex
            comment = item.get("comment")
            if comment is not None and (not isinstance(comment, str) or len(comment) > _MAX_COMMENT_CHARS):
                raise ValueError(f"comment は {_MAX_COMMENT_CHARS} 文字以内の文字列で指定してください")
            res = {"item": idx, "status": status, **info}
            if status == "ready":
                if used + len(hits) > max_highlights:
                    res = {"item": idx, "status": "too_many_matches", "count": len(hits),
                           "hint": f"1 回の書き込み上限({max_highlights} 件)を超えるため書き込みません。項目を分けて呼び出してください"}
                else:
                    used += len(hits)
                    plan.append((idx, hits, item_hex, _clean(comment) if comment else None))
            report.results.append(res)

        if not plan:
            return report

        writer = None if dry_run else PdfWriter(clone_from=self._reader)
        serial = 0
        for idx, hits, item_hex, comment in plan:
            res = report.results[idx]
            added = dup = 0
            pages_hit: list[int] = []
            matched: list[str] = []
            for hit in hits:
                glyphs, geo = self._page(hit.page)
                gs = [glyphs[i] for i in hit.ids]
                raw = _span_text(glyphs, hit.ids)
                if len(matched) < 5:
                    matched.append(_shorten(raw, 40))
                if hit.page + 1 not in pages_hit:
                    pages_hit.append(hit.page + 1)
                if writer is None:
                    added += 1
                    continue
                match = HighlightMatch(keyword=raw, occurrence=0, page=hit.page + 1,
                                       rects=tuple(_line_rects(gs, geo)), text=raw, color=item_hex)
                if _has_same_highlight(writer, hit.page, match.rects, item_hex):
                    dup += 1
                    continue
                serial += 1
                _add_annotation(writer, match, name=f"pdfhl:{run_id}:{serial}", author=author, comment=comment)
                added += 1
            if added:
                res.update(status="would_apply" if dry_run else "applied", count=added, pages=sorted(pages_hit),
                           matched=matched)
                report.applied += 1
                report.annotations += added
            else:
                res.update(status="skipped_duplicate", count=0)
            if dup and added:
                res["duplicates_skipped"] = dup

        if writer is not None and report.annotations:
            buf = io.BytesIO()
            writer.write(buf)
            report.data = buf.getvalue()
        return report


def _has_same_highlight(writer: PdfWriter, page0: int, rects: Sequence[Rect], color_hex: str) -> bool:
    """同じページに、同じ位置・同じ色のハイライトが既にあるか(再実行で重複させないため)。"""
    quads: list[float] = []
    for x0, y0, x1, y1 in rects:
        quads += [x0, y1, x1, y1, x0, y0, x1, y0]
    want_c = [int(color_hex[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    for ref in writer.pages[page0].get("/Annots", None) or []:
        a = ref.get_object()
        if a.get("/Subtype") != "/Highlight":
            continue
        try:
            qp = [float(v) for v in a.get("/QuadPoints", [])]
            c = [float(v) for v in a.get("/C", [])]
        except (TypeError, ValueError):
            continue
        if (len(qp) == len(quads) and all(abs(p - q) <= 0.5 for p, q in zip(qp, quads))
                and len(c) == 3 and all(abs(p - q) <= 0.02 for p, q in zip(c, want_c))):
            return True
    return False


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

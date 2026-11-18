"""PdfDocument(検索・ref・書き込み)のテスト。仕様: docs/SPEC-llm-tool-support.md"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pdf_highlight as ph  # noqa: E402
from test_pdf_highlight import TEXT1, TEXT2, make_pdf, render, yellow_bbox  # noqa: E402

pytest.importorskip("rapidfuzz")


@pytest.fixture()
def sample(tmp_path) -> Path:
    return make_pdf(tmp_path / "sample.pdf")


def annots(data: bytes, page: int = 0) -> list:
    return [a.get_object() for a in PdfReader(io.BytesIO(data)).pages[page].get("/Annots", [])]


# --------------------------------------------------------------------------- #
# 検索
# --------------------------------------------------------------------------- #
def test_exact_search_returns_context_and_ref(sample):
    with ph.PdfDocument.open(sample) as doc:
        res = doc.search("吾輩", mode="exact")
    assert res.total_found == 2 and not res.truncated
    first = res.candidates[0]
    assert first.page == 1 and first.score == 100 and first.match == "exact"
    assert first.ref == {"text": "吾輩", "page": 1, "nth": 1}
    assert first.after.startswith("は猫である")
    assert [c.ref["nth"] for c in res.candidates] == [1, 2]


def test_fuzzy_finds_typo_and_trims_edges(sample):
    with ph.PdfDocument.open(sample) as doc:
        res = doc.search("どこで生まれたかとんと見当がつかぬ", mode="fuzzy")
    assert res.candidates, "1 文字違いは候補になるはず"
    c = res.candidates[0]
    assert c.match == "fuzzy" and c.score >= 90
    assert c.text == "どこで生れたかとんと見当がつかぬ", "端の余計な文字(「。」)は除かれる"


def test_exact_mode_does_not_return_fuzzy_candidates(sample):
    with ph.PdfDocument.open(sample) as doc:
        assert doc.search("どこで生まれたかとんと見当がつかぬ", mode="exact").candidates == []


def test_unrelated_query_gives_nothing(sample):
    with ph.PdfDocument.open(sample) as doc:
        assert doc.search("契約の解除に関する条項", mode="fuzzy").candidates == []


def test_short_query_skips_fuzzy_with_warning(sample):
    with ph.PdfDocument.open(sample) as doc:
        res = doc.search("吾は", mode="fuzzy")
    assert any("fuzzy_skipped_short_query" in w for w in res.warnings)


def test_limit_and_truncated(tmp_path):
    p = make_pdf(tmp_path / "many.pdf", pages=("猫" * 30,))
    with ph.PdfDocument.open(p) as doc:
        res = doc.search("猫", limit=5)
    assert res.total_found == 30 and len(res.candidates) == 5 and res.truncated


def test_pages_filter(sample):
    with ph.PdfDocument.open(sample) as doc:
        assert doc.search("hello", ignore_case=True, pages=[1]).total_found == 0
        assert doc.search("hello", ignore_case=True, pages=[2]).total_found == 2
        with pytest.raises(ValueError):
            doc.search("hello", pages=[3])


def test_control_characters_are_removed_from_output():
    assert ph._clean("a\x00b​c\nd\te") == "abc d e"


# --------------------------------------------------------------------------- #
# ref: 検索 → ref → 解決 で必ず同じ箇所に戻る(性質テスト)
# --------------------------------------------------------------------------- #
ROUND_TRIP_CASES = [
    ("吾輩", "exact", False),
    ("吾輩", "fuzzy", True),
    ("どこで生まれたかとんと見当がつかぬ", "fuzzy", False),
    ("ニャー", "exact", False),
    ("hello world", "exact", True),
    ("HELLO WORLD", "fuzzy", True),
    ("ABC", "exact", False),
    ("は", "exact", False),
    ("で", "exact", True),
]


@pytest.mark.parametrize("query,mode,ignore_case", ROUND_TRIP_CASES)
def test_ref_round_trip(sample, query, mode, ignore_case):
    with ph.PdfDocument.open(sample) as doc:
        res = doc.search(query, mode=mode, ignore_case=ignore_case, limit=50, context_chars=0)
    assert res.candidates, "テスト条件の誤り: 候補が無い"
    # 新しい PdfDocument(= 新しい呼び出し)で解決しても、同じグリフ範囲に戻る
    with ph.PdfDocument.open(sample) as doc2:
        for c in res.candidates:
            hit = doc2.resolve_ref(c.ref)
            assert hit is not None and hit.ids == c.glyph_ids, c.ref


def test_ref_round_trip_with_overlapping_and_repeated_text(tmp_path):
    p = make_pdf(tmp_path / "rep.pdf", pages=("あああ いいい あああ", "あああ"))
    with ph.PdfDocument.open(p) as doc:
        res = doc.search("ああ", limit=50)
        assert res.total_found == 3  # 非重複で数える(あああ=1 件、×3)
        for c in res.candidates:
            hit = doc.resolve_ref(c.ref)
            assert hit is not None and hit.ids == c.glyph_ids
        # 重なる出現も nth で区別できる(あああ には「ああ」が 2 通りある)
        assert doc.resolve_ref({"text": "ああ", "page": 1, "nth": 2}).ids != \
            doc.resolve_ref({"text": "ああ", "page": 1, "nth": 1}).ids


def test_resolve_ref_is_independent_of_case_setting(sample):
    with ph.PdfDocument.open(sample) as doc:
        res = doc.search("hello world", ignore_case=True)
        assert any(c.text == "Hello World" for c in res.candidates)
        c = next(c for c in res.candidates if c.text == "Hello World")
        # ref は大小区別ありで解決される(text は PDF 上の実際の綴り)
        assert doc.resolve_ref(c.ref).ids == c.glyph_ids
        assert doc.resolve_ref({"text": "hello world", "page": 2, "nth": 1}) is None or \
            doc.resolve_ref({"text": "hello world", "page": 2, "nth": 1}).ids != c.glyph_ids


def test_resolve_ref_not_found_is_none_not_error(sample):
    with ph.PdfDocument.open(sample) as doc:
        assert doc.resolve_ref({"text": "存在しない", "page": 1, "nth": 1}) is None
        assert doc.resolve_ref({"text": "吾輩", "page": 1, "nth": 3}) is None
        assert doc.resolve_ref({"text": "吾輩", "page": 99, "nth": 1}) is None


@pytest.mark.parametrize("bad", [
    "x", {"page": 1, "nth": 1}, {"text": "", "page": 1, "nth": 1}, {"text": "a", "page": 0, "nth": 1},
    {"text": "a", "page": 1, "nth": 0}, {"text": "a", "page": True, "nth": 1}, {"text": "a", "page": 1, "nth": "1"},
    {"start": "a", "end": "b", "page": 1, "nth": 1},
])
def test_resolve_ref_rejects_malformed(sample, bad):
    with ph.PdfDocument.open(sample) as doc:
        with pytest.raises(ph.PdfRefError):
            doc.resolve_ref(bad)


def test_resolving_a_ref_extracts_only_that_page(tmp_path):
    p = make_pdf(tmp_path / "ten.pdf", pages=tuple(f"ページ{i}の本文です。" for i in range(10)))
    with ph.PdfDocument.open(p) as doc:
        assert doc.resolve_ref({"text": "ページ7の本文", "page": 8, "nth": 1}) is not None
        assert list(doc._pages) == [7], "ref の解決は該当ページだけを抽出するはず(遅延抽出)"


# --------------------------------------------------------------------------- #
# 書き込み
# --------------------------------------------------------------------------- #
def test_highlight_by_ref_matches_search_position(sample):
    with ph.PdfDocument.open(sample) as doc:
        c = doc.search("どこで生れたか").candidates[0]
        rep = doc.highlight([{"ref": c.ref}])
    assert rep.applied == 1 and rep.annotations == 1 and rep.data
    assert rep.results[0]["status"] == "applied" and rep.results[0]["matched"] == ["どこで生れたか"]
    box, dark = yellow_bbox(render(rep.data))
    assert box is not None and dark > 50


def test_highlight_by_ref_in_a_fresh_process_like_call(sample):
    """検索と書き込みを別の PdfDocument(別の呼び出し)で行っても同じ結果になる。"""
    with ph.PdfDocument.open(sample) as d1:
        ref = d1.search("吾輩").candidates[1].ref
    with ph.PdfDocument.open(sample) as d2:
        rep = d2.highlight([{"ref": ref}], color="blue")
    (a,) = annots(rep.data)
    assert [round(float(v), 2) for v in a["/C"]] == [0.0, 0.75, 1.0]
    assert str(a["/NM"]).startswith("pdfhl:") and str(a["/T"]) == "pdf_highlight"


def test_highlight_text_exact_all_and_occurrence(sample):
    with ph.PdfDocument.open(sample) as doc:
        rep = doc.highlight([{"text": "吾輩"}, {"text": "は", "occurrence": [2], "color": "green"}])
    assert [r["status"] for r in rep.results] == ["applied", "applied"]
    assert rep.results[0]["count"] == 2 and rep.results[1]["count"] == 1
    assert rep.annotations == 3


def test_highlight_item_needs_exactly_one_of_ref_or_text(sample):
    with ph.PdfDocument.open(sample) as doc:
        for bad in ({}, {"text": "a", "ref": {"text": "a", "page": 1, "nth": 1}}):
            with pytest.raises(ValueError):
                doc.highlight([bad])


def test_not_found_gives_suggestions_and_writes_nothing(sample):
    with ph.PdfDocument.open(sample) as doc:
        rep = doc.highlight([{"text": "どこで生まれたかとんと見当がつかぬ"}])
    r = rep.results[0]
    assert r["status"] == "not_found" and r["suggestions"][0]["ref"]["text"] == "どこで生れたかとんと見当がつかぬ"
    assert rep.data is None and rep.annotations == 0


def test_fuzzy_best_applies_and_reports_matched_text(sample):
    with ph.PdfDocument.open(sample) as doc:
        rep = doc.highlight([{"text": "どこで生まれたかとんと見当がつかぬ", "mode": "fuzzy"}])
    r = rep.results[0]
    assert r["status"] == "applied" and r["matched"] == ["どこで生れたかとんと見当がつかぬ"]


def test_fuzzy_ambiguous_does_not_write(tmp_path):
    p = make_pdf(tmp_path / "amb.pdf", pages=("第5条の2に定める損害賠償。第5条の3に定める損害賠償。",))
    with ph.PdfDocument.open(p) as doc:
        rep = doc.highlight([{"text": "第5条の4に定める損害賠償", "mode": "fuzzy"}])
        r = rep.results[0]
        assert r["status"] == "ambiguous" and len(r["candidates"]) == 2 and rep.data is None
        # 候補の ref で 1 か所を指定すれば付けられる
        rep2 = doc.highlight([{"ref": r["candidates"][0]["ref"]}])
    assert rep2.results[0]["status"] == "applied"


def test_fuzzy_select_all(tmp_path):
    p = make_pdf(tmp_path / "amb.pdf", pages=("第5条の2に定める損害賠償。第5条の3に定める損害賠償。",))
    with ph.PdfDocument.open(p) as doc:
        rep = doc.highlight([{"text": "第5条の4に定める損害賠償", "mode": "fuzzy", "select": "all"}])
    assert rep.results[0]["status"] == "applied" and rep.results[0]["count"] == 2


def test_too_many_matches_is_refused(tmp_path):
    p = make_pdf(tmp_path / "many.pdf", pages=("猫" * 80,))
    with ph.PdfDocument.open(p) as doc:
        rep = doc.highlight([{"text": "猫"}, {"text": "猫", "occurrence": [1, 2]}])
    assert rep.results[0]["status"] == "too_many_matches" and rep.results[0]["count"] == 80
    assert rep.results[0]["pages_distribution"] == {"1": 80}
    assert rep.results[1]["status"] == "applied", "部分成功: 絞った項目は付く"
    assert rep.annotations == 2


def test_total_limit(tmp_path):
    p = make_pdf(tmp_path / "many.pdf", pages=("猫" * 40,))
    with ph.PdfDocument.open(p) as doc:
        rep = doc.highlight([{"text": "猫", "occurrence": list(range(1, 31))}, {"text": "猫", "occurrence": [31, 32]}],
                            max_highlights=31)
    assert [r["status"] for r in rep.results] == ["applied", "too_many_matches"]


def test_dry_run_writes_nothing(sample):
    with ph.PdfDocument.open(sample) as doc:
        rep = doc.highlight([{"text": "吾輩"}], dry_run=True)
    assert rep.results[0]["status"] == "would_apply" and rep.results[0]["count"] == 2 and rep.data is None


def test_rerun_on_output_is_idempotent(sample):
    with ph.PdfDocument.open(sample) as doc:
        first = doc.highlight([{"text": "吾輩"}])
    assert len(annots(first.data)) == 2
    with ph.PdfDocument.open(first.data) as doc2:
        again = doc2.highlight([{"text": "吾輩"}])
        assert again.results[0]["status"] == "skipped_duplicate" and again.data is None
        # 別の色なら別のハイライトとして付く
        other = doc2.highlight([{"text": "吾輩", "color": "blue"}])
    assert len(annots(other.data)) == 4


def test_same_item_twice_in_one_call_is_deduplicated(sample):
    with ph.PdfDocument.open(sample) as doc:
        rep = doc.highlight([{"text": "吾輩"}, {"text": "吾輩"}])
    assert rep.results[1]["status"] == "skipped_duplicate" and len(annots(rep.data)) == 2


def test_run_id_is_deterministic(sample):
    with ph.PdfDocument.open(sample) as d1, ph.PdfDocument.open(sample) as d2:
        a = d1.highlight([{"text": "吾輩"}])
        b = d2.highlight([{"text": "吾輩"}])
        c = d2.highlight([{"text": "吾輩", "color": "red"}])
    assert a.run_id == b.run_id != c.run_id
    assert [str(x["/NM"]) for x in annots(a.data)] == [str(x["/NM"]) for x in annots(b.data)]


def test_comment_is_stored_and_cleaned(sample):
    with ph.PdfDocument.open(sample) as doc:
        rep = doc.highlight([{"text": "吾輩", "occurrence": [1], "comment": "主語\x00です"}])
    assert str(annots(rep.data)[0]["/Contents"]) == "主語です"


def test_input_bytes_are_not_modified(sample):
    before = sample.read_bytes()
    with ph.PdfDocument.open(sample) as doc:
        doc.highlight([{"text": "吾輩"}])
    assert sample.read_bytes() == before


def test_highlight_counts_and_pages_without_text(tmp_path):
    p = make_pdf(tmp_path / "s.pdf", pages=("吾輩は猫である",))
    with ph.PdfDocument.open(p) as doc:
        assert doc.highlight_counts() == {"total": 0, "ours": 0}
        assert doc.pages_without_text() == []
        data = doc.highlight([{"text": "吾輩"}]).data
    with ph.PdfDocument.open(data) as doc:
        assert doc.highlight_counts() == {"total": 1, "ours": 1}


def test_encrypted_error_types(sample, tmp_path):
    from pypdf import PdfWriter

    w = PdfWriter(clone_from=PdfReader(str(sample)))
    w.encrypt("secret")
    enc = tmp_path / "enc.pdf"
    with open(enc, "wb") as f:
        w.write(f)
    with pytest.raises(ph.PdfPasswordRequired):
        ph.PdfDocument.open(enc)
    with pytest.raises(ph.PdfPasswordWrong):
        ph.PdfDocument.open(enc, "bad")
    with ph.PdfDocument.open(enc, "secret") as doc:
        assert doc.search("吾輩").total_found == 2


def test_deadline_raises_timeout(sample):
    import time

    with ph.PdfDocument.open(sample, deadline=time.monotonic() - 1) as doc:
        with pytest.raises(ph.PdfTimeoutError):
            doc.search("吾輩")

"""ツール層(dispatch)のテスト。シナリオ(S1〜S8)・安全装置・ステートレス性。"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

import pdf_highlight_tools as tools  # noqa: E402
from test_pdf_highlight import make_pdf, render, yellow_bbox  # noqa: E402

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


def call(cfg, name, **args) -> dict:
    return tools.dispatch(name, args, cfg)


def annots(path: Path, page: int = 0) -> list:
    return [a.get_object() for a in PdfReader(str(path)).pages[page].get("/Annots", [])]


# --------------------------------------------------------------------------- #
# シナリオ
# --------------------------------------------------------------------------- #
def test_inspect(cfg, sample):
    r = call(cfg, "pdf_inspect", input=str(sample))
    d = r["document"]
    assert r["ok"] and d["pages"] == 2 and d["text_layer"] == "full" and d["encrypted"] is False
    assert d["highlights"] == {"total": 0, "ours": 0} and len(d["fingerprint"]) == 12


def test_S1_exact_word_in_one_call(cfg, sample, work):
    r = call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}], output=str(work / "o.pdf"))
    assert r["ok"] and r["output_written"] and r["applied"] == 1 and r["annotations"] == 2
    assert len(annots(work / "o.pdf")) == 2


def test_S2_search_then_highlight_with_ref(cfg, sample, work):
    s = call(cfg, "pdf_search", input=str(sample), queries=["どこで生まれたかとんと見当がつかぬ"])
    assert s["ok"] and s["total_found"] == 1
    cand = s["candidates"][0]
    assert cand["match"] == "fuzzy" and cand["text"] == "どこで生れたかとんと見当がつかぬ"
    r = call(cfg, "pdf_highlight", input=str(sample), items=[{"ref": cand["ref"]}],
             expect_fingerprint=s["document"]["fingerprint"], output=str(work / "o.pdf"))
    assert r["ok"] and r["results"][0]["status"] == "applied"
    box, dark = yellow_bbox(render(work / "o.pdf"))
    assert box is not None and dark > 50


def test_S2b_fuzzy_in_a_single_call(cfg, sample, work):
    r = call(cfg, "pdf_highlight", input=str(sample), output=str(work / "o.pdf"),
             items=[{"text": "どこで生まれたかとんと見当がつかぬ", "mode": "fuzzy"}])
    assert r["results"][0]["status"] == "applied"
    assert r["results"][0]["matched"] == ["どこで生れたかとんと見当がつかぬ"]


def test_S6_too_many_matches_is_refused(cfg, work):
    p = make_pdf(work / "many.pdf", pages=("猫" * 80,))
    r = call(cfg, "pdf_highlight", input=str(p), items=[{"text": "猫"}])
    assert r["ok"] and r["results"][0]["status"] == "too_many_matches"
    assert r["output_written"] is False and r["output"] is None and "hint" in r
    assert not (work / "many_highlighted.pdf").exists(), "書き込まれなかった場合は出力ファイルを作らない"


def test_S7_chain_output_as_input_is_idempotent(cfg, sample, work):
    call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}], output=str(work / "a.pdf"))
    r = call(cfg, "pdf_highlight", input=str(work / "a.pdf"), items=[{"text": "吾輩"}, {"text": "猫"}],
             output=str(work / "b.pdf"))
    assert [x["status"] for x in r["results"]] == ["skipped_duplicate", "applied"]
    assert len(annots(work / "b.pdf")) == 3  # 吾輩 2(元から)+ 猫 1


def test_not_found_returns_suggestions(cfg, sample):
    r = call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "どこで生まれたかとんと見当がつかぬ"}])
    res = r["results"][0]
    assert res["status"] == "not_found" and res["suggestions"][0]["ref"]["page"] == 1
    assert r["output_written"] is False


def test_dry_run(cfg, sample, work):
    r = call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}], dry_run=True)
    assert r["dry_run"] and r["results"][0]["status"] == "would_apply" and not r["output_written"]
    assert not (work / "sample_highlighted.pdf").exists()


def test_default_output_name(cfg, sample, work):
    r = call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}])
    assert Path(r["output"]) == (work / "sample_highlighted.pdf").resolve() and (work / "sample_highlighted.pdf").exists()


# --------------------------------------------------------------------------- #
# ステートレス(S8): 検索と書き込みを別プロセスで行い、ref は「文字列」だけを受け渡す
# --------------------------------------------------------------------------- #
def run_in_fresh_process(work: Path, name: str, args: dict) -> dict:
    code = (
        "import json,sys; sys.path.insert(0, sys.argv[1]); import pdf_highlight_tools as t;"
        "a=json.loads(sys.stdin.read()); "
        "print(t.dumps(t.dispatch(sys.argv[2], a, t.ToolConfig(allowed_dirs=(__import__('pathlib').Path(sys.argv[3]),)))))"
    )
    p = subprocess.run([sys.executable, "-X", "utf8", "-I", "-c", code, str(ROOT), name, str(work)],
                       input=json.dumps(args), capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_S8_search_and_highlight_in_separate_processes(sample, work):
    s = run_in_fresh_process(work, "pdf_search", {"input": str(sample), "queries": ["吾輩"], "mode": "exact"})
    # 要約に入った ref だけを文字列で受け渡す(応答オブジェクトは引き継がない)
    ref_text = json.dumps(s["candidates"][1]["ref"], ensure_ascii=False)
    r = run_in_fresh_process(work, "pdf_highlight", {
        "input": str(sample), "output": str(work / "o.pdf"), "items": [{"ref": json.loads(ref_text)}]})
    assert r["ok"] and r["results"][0]["status"] == "applied" and r["results"][0]["count"] == 1
    assert len(annots(work / "o.pdf")) == 1


def test_results_do_not_depend_on_call_history(cfg, sample, work):
    first = call(cfg, "pdf_search", input=str(sample), queries=["吾輩", "hello"], mode="fuzzy")
    call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "猫"}], output=str(work / "x.pdf"))
    call(cfg, "pdf_inspect", input=str(sample))
    again = call(cfg, "pdf_search", input=str(sample), queries=["吾輩", "hello"], mode="fuzzy")
    assert first == again


def test_highlight_writes_only_the_output_file(cfg, sample, work):
    before = {p.name for p in work.iterdir()}
    call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}], output=str(work / "o.pdf"))
    assert {p.name for p in work.iterdir()} - before == {"o.pdf"}


# --------------------------------------------------------------------------- #
# 出力形式
# --------------------------------------------------------------------------- #
def test_dumps_is_compact_and_not_ascii_escaped(cfg, sample):
    s = tools.dumps(call(cfg, "pdf_search", input=str(sample), queries=["吾輩"], mode="exact"))
    assert "吾輩" in s and "\\u" not in s and ": " not in s and ", " not in s


def test_search_response_fields(cfg, sample):
    s = call(cfg, "pdf_search", input=str(sample), queries=["吾輩"], mode="exact", context_chars=5)
    c = s["candidates"][0]
    assert set(c) == {"ref", "page", "score", "text", "before", "after", "query", "match", "length", "position"}
    assert s["queries"] == [{"index": 0, "found": 2, "best_score": 100}]
    assert "rects" not in json.dumps(s)


# --------------------------------------------------------------------------- #
# 安全装置・エラー
# --------------------------------------------------------------------------- #
def code_of(r: dict) -> str:
    assert r["ok"] is False, r
    return r["error"]["code"]


def test_path_outside_allowed_dirs_is_rejected(cfg, sample, tmp_path):
    outside = make_pdf(tmp_path / "outside.pdf")
    assert code_of(call(cfg, "pdf_search", input=str(outside), queries=["吾輩"])) == "path_not_allowed"
    assert code_of(call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}],
                        output=str(tmp_path / "o.pdf"))) == "path_not_allowed"
    traversal = str(sample.parent / ".." / "outside.pdf")
    assert code_of(call(cfg, "pdf_inspect", input=traversal)) == "path_not_allowed"


def test_symlink_escape_is_rejected(cfg, work, tmp_path):
    outside = make_pdf(tmp_path / "outside.pdf")
    link = work / "link.pdf"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("シンボリックリンクを作れない環境")
    assert code_of(call(cfg, "pdf_inspect", input=str(link))) == "path_not_allowed"


def test_extension_and_missing_input(cfg, work):
    (work / "a.txt").write_text("x")
    assert code_of(call(cfg, "pdf_inspect", input=str(work / "a.txt"))) == "path_not_allowed"
    assert code_of(call(cfg, "pdf_inspect", input=str(work / "none.pdf"))) == "file_not_found"


def test_input_equals_output_is_rejected(cfg, sample):
    assert code_of(call(cfg, "pdf_highlight", input=str(sample), output=str(sample),
                        items=[{"text": "吾輩"}], overwrite=True)) == "path_not_allowed"


def test_overwrite_policy(cfg, sample, work):
    out = work / "o.pdf"
    call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}], output=str(out))
    again = dict(input=str(sample), items=[{"text": "猫"}], output=str(out))
    assert code_of(call(cfg, "pdf_highlight", **again)) == "output_exists"
    # overwrite=true でも、ホスト設定が許可していなければ拒否
    assert code_of(call(cfg, "pdf_highlight", **again, overwrite=True)) == "path_not_allowed"
    allowed = tools.ToolConfig(allowed_dirs=cfg.allowed_dirs, allow_overwrite=True)
    r = call(allowed, "pdf_highlight", **again, overwrite=True)
    assert r["ok"] and r["output_written"]
    assert not list(work.glob("*.tmp")), "一時ファイルが残らない"


def test_exclusive_create_never_clobbers(work):
    out = work / "o.pdf"
    out.write_bytes(b"keep")
    with pytest.raises(tools.ToolError) as e:
        tools._write_output(b"new", out, overwrite=False)
    assert e.value.code == "output_exists" and out.read_bytes() == b"keep"


def test_limits(cfg, work, sample):
    small = tools.ToolConfig(allowed_dirs=cfg.allowed_dirs, max_file_bytes=100)
    assert code_of(call(small, "pdf_inspect", input=str(sample))) == "limit_exceeded"
    few = tools.ToolConfig(allowed_dirs=cfg.allowed_dirs, max_pages=1)
    assert code_of(call(few, "pdf_inspect", input=str(sample))) == "limit_exceeded"
    zero = tools.ToolConfig(allowed_dirs=cfg.allowed_dirs, timeout_s=-1)
    assert code_of(call(zero, "pdf_search", input=str(sample), queries=["吾輩"])) == "timeout"


def test_invalid_pdf_and_encryption(cfg, work, sample):
    bad = work / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert code_of(call(cfg, "pdf_inspect", input=str(bad))) == "invalid_pdf"
    w = PdfWriter(clone_from=PdfReader(str(sample)))
    w.encrypt("secret")
    enc = work / "enc.pdf"
    with open(enc, "wb") as f:
        w.write(f)
    assert code_of(call(cfg, "pdf_inspect", input=str(enc))) == "password_required"
    assert code_of(call(cfg, "pdf_inspect", input=str(enc), password="x")) == "wrong_password"
    assert call(cfg, "pdf_inspect", input=str(enc), password="secret")["ok"]
    with_env = tools.ToolConfig(allowed_dirs=cfg.allowed_dirs, password="secret")
    assert call(with_env, "pdf_inspect", input=str(enc))["ok"]
    assert "secret" not in tools.dumps(call(cfg, "pdf_inspect", input=str(enc), password="secret"))


def test_no_text_layer(cfg, work):
    w = PdfWriter()
    w.add_blank_page(200, 200)
    p = work / "blank.pdf"
    with open(p, "wb") as f:
        w.write(f)
    assert call(cfg, "pdf_inspect", input=str(p))["document"]["text_layer"] == "none"
    assert code_of(call(cfg, "pdf_search", input=str(p), queries=["吾輩"])) == "no_text_layer"


def test_stale_document(cfg, sample):
    r = call(cfg, "pdf_highlight", input=str(sample), items=[{"text": "吾輩"}], expect_fingerprint="000000000000")
    assert code_of(r) == "stale_document"


def test_ref_after_file_change_is_not_found_not_an_error(cfg, work):
    a = make_pdf(work / "a.pdf", pages=("吾輩は猫である",))
    s = call(cfg, "pdf_search", input=str(a), queries=["吾輩"], mode="exact")
    b = make_pdf(work / "b.pdf", pages=("全く別の文章です",))
    r = call(cfg, "pdf_highlight", input=str(b), items=[{"ref": s["candidates"][0]["ref"]}])
    assert r["ok"] and r["results"][0]["status"] == "not_found"


@pytest.mark.parametrize("name,args", [
    ("pdf_search", {"queries": ["a"]}),                                   # input なし
    ("pdf_search", {"input": "x.pdf", "queries": []}),
    ("pdf_search", {"input": "x.pdf", "queries": ["a"], "mode": "regex"}),
    ("pdf_search", {"input": "x.pdf", "queries": ["a"], "limit": 0}),
    ("pdf_search", {"input": "x.pdf", "queries": ["a"], "bogus": 1}),
    ("pdf_highlight", {"input": "x.pdf", "items": []}),
    ("pdf_highlight", {"input": "x.pdf", "items": [{"text": "a", "occurence": [1]}]}),  # typo
    ("pdf_highlight", {"input": "x.pdf", "items": ["a"]}),
    ("nope", {}),
])
def test_invalid_arguments(cfg, name, args):
    assert code_of(tools.dispatch(name, args, cfg)) == "invalid_argument"


def test_invalid_item_values_and_ref(cfg, sample):
    f = lambda items, **kw: tools.dispatch("pdf_highlight", {"input": str(sample), "items": items, **kw}, cfg)  # noqa: E731
    assert code_of(f([{}])) == "invalid_argument"
    assert code_of(f([{"text": "吾輩", "color": "zzz"}])) == "invalid_argument"
    assert code_of(f([{"text": "吾輩", "pages": [9]}])) == "invalid_argument"
    assert code_of(f([{"text": "吾輩", "mode": "fuzzy", "occurrence": [1]}])) == "invalid_argument"
    assert code_of(f([{"ref": {"text": "吾輩", "page": 0, "nth": 1}}])) == "invalid_ref"
    assert code_of(f([{"ref": "吾輩"}])) == "invalid_ref"


def test_dispatch_never_raises(cfg, monkeypatch):
    import pdf_highlight as core

    def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(core.PdfDocument, "search", boom)
    p = make_pdf(Path(cfg.allowed_dirs[0]) / "s.pdf")
    r = call(cfg, "pdf_search", input=str(p), queries=["吾輩"])
    assert code_of(r) == "internal_error" and "secret" not in json.dumps(r)
    assert tools.dispatch("pdf_search", None, cfg)["ok"] is False


def test_config_from_env(tmp_path):
    c = tools.ToolConfig.from_env({
        "PDF_HIGHLIGHT_ALLOWED_DIRS": f"{tmp_path}{__import__('os').pathsep}{tmp_path / 'b'}",
        "PDF_HIGHLIGHT_ALLOW_OVERWRITE": "1", "PDF_HIGHLIGHT_MAX_MB": "1.5",
        "PDF_HIGHLIGHT_MAX_PAGES": "7", "PDF_HIGHLIGHT_TIMEOUT": "3", "PDF_HIGHLIGHT_PASSWORD": "pw"})
    assert c.allowed_dirs == (tmp_path, tmp_path / "b") and c.allow_overwrite
    assert c.max_file_bytes == int(1.5 * 1024 * 1024) and c.max_pages == 7 and c.timeout_s == 3 and c.password == "pw"
    d = tools.ToolConfig.from_env({})
    assert d.allowed_dirs == () and d.allow_overwrite is False and d.password is None


def test_tool_definitions_are_wellformed_and_flat():
    names = [t["name"] for t in tools.TOOL_DEFINITIONS]
    assert names == ["pdf_inspect", "pdf_read_text", "pdf_search", "pdf_highlight"]
    assert set(names) == set(tools._HANDLERS)
    blob = json.dumps(tools.TOOL_DEFINITIONS)
    assert "oneOf" not in blob and "anyOf" not in blob, "ホスト互換のため oneOf/anyOf を使わない"
    for t in tools.TOOL_DEFINITIONS:
        assert t["input_schema"]["type"] == "object" and t["description"]
        assert set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"])
    props = {t["name"]: set(t["input_schema"]["properties"]) for t in tools.TOOL_DEFINITIONS}
    assert props["pdf_search"] == tools._SEARCH_KEYS and props["pdf_highlight"] == tools._HIGHLIGHT_KEYS
    assert props["pdf_read_text"] == tools._READ_KEYS
    assert tools.TOOL_DEFINITIONS[1]["input_schema"]["required"] == ["input", "pages"], "pages は必須(全文ダンプを防ぐ)"
    item_props = set(tools._ITEM_SCHEMA["properties"])
    assert item_props == tools._ITEM_KEYS

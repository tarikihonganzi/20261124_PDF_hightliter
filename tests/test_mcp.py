"""MCP サーバ(stdio)のエンドツーエンドのテスト。

実際にサーバを子プロセスとして起動し、MCP の公式クライアントで接続して、
ツール一覧の取得・検索・ハイライト・エラーの返り方を確認する。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

pytest.importorskip("rapidfuzz")
pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from test_pdf_highlight import make_pdf, render, yellow_bbox  # noqa: E402


def run_session(work: Path, scenario, extra_env: dict | None = None):
    """サーバを起動して scenario(session) を実行する。"""
    env = {"PDF_HIGHLIGHT_ALLOWED_DIRS": str(work), **(extra_env or {})}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-X", "utf8", str(ROOT / "pdf_highlight_mcp.py")],
        env=env,
        cwd=str(work),
        encoding="utf-8",
    )

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await scenario(session)

    return asyncio.run(asyncio.wait_for(main(), timeout=120))


def parse(result) -> dict:
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


@pytest.fixture()
def work(tmp_path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    make_pdf(d / "sample.pdf")
    return d


def test_list_tools(work):
    async def scenario(s):
        return await s.list_tools()

    res = run_session(work, scenario)
    by_name = {t.name: t for t in res.tools}
    assert set(by_name) == {"pdf_inspect", "pdf_search", "pdf_highlight"}
    assert by_name["pdf_search"].annotations.read_only_hint is True
    assert by_name["pdf_inspect"].annotations.read_only_hint is True
    assert by_name["pdf_highlight"].annotations.read_only_hint is False
    assert by_name["pdf_highlight"].annotations.destructive_hint is False
    schema = by_name["pdf_highlight"].input_schema
    assert schema["required"] == ["input", "items"] and "ref" in schema["properties"]["items"]["items"]["properties"]


def test_search_then_highlight_over_mcp(work):
    """日本語の往復・相対パス・ref の受け渡し・ファイル生成まで、MCP 越しに通す。"""

    async def scenario(s):
        found = await s.call_tool("pdf_search", {"input": "sample.pdf", "queries": ["どこで生まれたかとんと見当がつかぬ"]})
        ref = parse(found)["candidates"][0]["ref"]
        done = await s.call_tool("pdf_highlight", {"input": "sample.pdf", "output": "out.pdf", "items": [{"ref": ref}]})
        return found, done

    found, done = run_session(work, scenario)
    f, d = parse(found), parse(done)
    assert not found.is_error and f["ok"] and f["candidates"][0]["text"] == "どこで生れたかとんと見当がつかぬ"
    assert not done.is_error and d["ok"] and d["output_written"] and d["results"][0]["status"] == "applied"
    out = work / "out.pdf"
    assert out.exists() and len(PdfReader(str(out)).pages[0]["/Annots"]) == 1
    box, dark = yellow_bbox(render(out))
    assert box is not None and dark > 50


def test_errors_are_flagged_and_structured(work, tmp_path):
    outside = make_pdf(tmp_path / "outside.pdf")

    async def scenario(s):
        return [
            await s.call_tool("pdf_inspect", {"input": str(outside)}),
            await s.call_tool("pdf_search", {"input": "sample.pdf", "queries": []}),
            await s.call_tool("pdf_highlight", {"input": "sample.pdf", "items": [{"text": "吾輩", "bogus": 1}]}),
            await s.call_tool("no_such_tool", {}),
        ]

    results = run_session(work, scenario)
    bodies = [json.loads(r.content[0].text) for r in results if r.content and r.content[0].type == "text"]
    assert all(r.is_error for r in results)
    assert [b["error"]["code"] for b in bodies[:3]] == ["path_not_allowed", "invalid_argument", "invalid_argument"]
    assert bodies[3]["error"]["code"] == "invalid_argument"


def test_overwrite_setting_comes_from_server_environment(work):
    """上書きの可否はホスト(サーバ起動時の環境変数)が決め、LLM の引数では変えられない。"""
    overwrite_call = {"input": "sample.pdf", "output": "o.pdf", "overwrite": True, "items": [{"text": "猫"}]}

    async def create_then_overwrite(s):
        first = await s.call_tool("pdf_highlight", {"input": "sample.pdf", "output": "o.pdf", "items": [{"text": "吾輩"}]})
        second = await s.call_tool("pdf_highlight", overwrite_call)
        return parse(first), parse(second)

    first, second = run_session(work, create_then_overwrite)
    assert first["ok"] and first["output_written"]
    assert second["ok"] is False and second["error"]["code"] == "path_not_allowed"

    async def overwrite_only(s):
        return parse(await s.call_tool("pdf_highlight", overwrite_call))

    allowed = run_session(work, overwrite_only, {"PDF_HIGHLIGHT_ALLOW_OVERWRITE": "1"})
    assert allowed["ok"] and allowed["output_written"]
    assert len(PdfReader(str(work / "o.pdf")).pages[0]["/Annots"]) == 1  # 「猫」の 1 件で置き換わった

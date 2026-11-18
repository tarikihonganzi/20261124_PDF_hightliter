# pdf_highlight

PDF 内の指定文字列をハイライト(蛍光ペン注釈)します。日本語・改行またぎに対応(横書きのみ)。

## ライセンス

依存ライブラリはすべて商用利用可能で、有料のものはありません。

| ライブラリ | ライセンス | 用途 |
|---|---|---|
| pdfplumber | MIT | 文字ごとの座標取得 |
| pypdf | BSD-3-Clause | ハイライト注釈の書き込み |
| rapidfuzz | MIT | あいまい一致(LLM ツール用) |
| mcp | MIT | MCP サーバ(LLM ツール用) |

(テスト用: pytest = MIT / reportlab = BSD-3 / pypdfium2 = Apache-2.0 または BSD-3)

## セットアップ

```powershell
pip install -r requirements.txt        # CLI / Python API のみ
pip install -r requirements-tools.txt  # LLM ツール(MCP サーバ)も使う場合
pip install -r requirements-dev.txt    # テストも行う場合
```

Python 3.9 以上を想定していますが、動作確認は 3.12 のみです。

## コマンドライン

```powershell
python pdf_highlight.py 入力.pdf 吾輩 "Hello World" -o 出力.pdf

# 語ごとに色を変える(-s を何度でも指定できる)
python pdf_highlight.py 入力.pdf -s blue 吾輩 -s yellow 猫 -s FF69B4 "Hello World" -o 出力.pdf
```

| オプション | 説明 |
|---|---|
| `keyword ...` | ハイライトする文字列(複数可)。色は `-c`(既定は黄) |
| `-s, --highlight COLOR KEYWORD` | 色を指定してハイライトする文字列。何度でも指定可 |
| `-k, --keywords-file FILE` | キーワードを 1 行 1 語で書いた UTF-8 テキスト。`キーワード<TAB>色` の行はその色で付与 |
| `-o, --output FILE` | 出力先。省略時は `<入力名>_highlighted.pdf` |
| `-c, --color` | 色を個別指定しない語の色。16 進 6 桁か色名。既定 `FFFF00` |
| `-i, --ignore-case` | 英字の大文字小文字を区別しない |
| `--cross-page` | ページをまたぐ文字列も検索する |
| `--password` | 暗号化 PDF のパスワード(出力は暗号化されない) |
| `--overwrite` | 出力先が既にある場合に上書きする |
| `--fail-on-no-match` | 一致 0 件なら終了コード 3 にする |
| `--json` | 結果を JSON で標準出力に出す |
| `-v` | 詳細ログ |

色名: `yellow`(FFFF00)/ `green`(00FF00)/ `cyan`(00FFFF)/ `pink`(FF69B4)/ `orange`(FFA500)/
`red`(FF0000)/ `blue`(00BFFF)/ `purple`(DA70D6)。`blue` は文字が読めるよう明るい青にしています。
濃い青などは 16 進で指定してください。同じ語に異なる色を指定するとエラーになります。

キーワードファイルの例(色の区切りは TAB。色の無い行は `-c` の色):

```text
吾輩	blue
猫	yellow
Hello World
```

終了コード: `0` 成功 / `1` エラー / `2` 引数エラー / `3` 一致なし(`--fail-on-no-match` 時のみ)

## 他のスクリプトから呼び出す

```python
from pdf_highlight import highlight_pdf, PdfHighlightError

try:
    result = highlight_pdf("in.pdf", "out.pdf", ["吾輩", "Hello World"], color="FFFF00")
except PdfHighlightError as e:
    ...
print(result.counts())   # {'吾輩': 3, 'Hello World': 1}
print(result.total)      # 4
for m in result.matches: # 注釈ごとの情報(ページ番号は 1 始まり、rects は PDF 座標)
    print(m.keyword, m.page, m.rects, m.color)

# 語ごとに色を指定する(dict、または str と (str, 色) の混在リスト)
highlight_pdf("in.pdf", "out.pdf", {"吾輩": "blue", "猫": "yellow"})
highlight_pdf("in.pdf", "out.pdf", [("吾輩", "blue"), "猫"], color="green")  # 猫は green
print(result.colors)     # {'吾輩': '00bfff', '猫': '00ff00'}
```

- 入力は **パス / bytes / バイナリのファイルオブジェクト**、出力は **パス / ファイルオブジェクト / `None`**
  (`None` なら `result.data` に PDF のバイト列が入る)。
- ライブラリとして使っても `print` や `sys.exit` は行わず、ログは `logging`(`pdf_highlight` ロガー)に出す。
- 一致が 0 件でも例外にはならない。入力ファイルは変更しない。出力先が既にある場合は
  `FileExistsError`(`overwrite=True` で上書き)。入力と同じパスへの出力は `ValueError`。
- 例外: `PdfHighlightError`(PDF が読めない等)/ `PdfPasswordError`(パスワード関連)/
  引数不正は `ValueError` / `TypeError`。

## LLM から使う(MCP サーバ)

MCP 対応のホスト(Claude Code / Claude Desktop など)から、PDF のハイライトを Tool call で使えます。
設計の詳細は [docs/SPEC-llm-tool-support.md](docs/SPEC-llm-tool-support.md)。

**ツール(4 つ)**

| ツール | 種別 | 用途 |
|---|---|---|
| `pdf_inspect` | 読み取り | ページ数・暗号化・テキスト層の有無・既存ハイライト数。`outline`(目次)、`page_heads`(各ページの最初の行)も取れる |
| `pdf_read_text` | 読み取り | **指定したページだけ**の本文を返す(`pages` 必須・1 回 4000 文字まで・続きは `next_offset`) |
| `pdf_search` | 読み取り | 語を検索し、前後の文脈付きで候補を返す(`mode=fuzzy` で誤字・一部欠落にも近い箇所を返す) |
| `pdf_highlight` | **書き込み** | 語、または検索候補の `ref` にハイライトを付け、新しい PDF に保存する |

典型的な流れ:
- 語が PDF にそのまま在るなら `pdf_highlight` を 1 回。
- 確信がなければ `pdf_search` で候補を見て、`ref` を `pdf_highlight` に渡す(`items` に `{"text": "…", "mode": "fuzzy"}` を指定すれば 1 回で済み、曖昧なときは書き込まず候補を返す)。
- PDF の内容が分からないとき(「重要そうな箇所に付けて」など)は、`pdf_inspect` の `outline` / `page_heads` で読むページを絞り、
  `pdf_read_text` でそのページだけ読んでから、引用した語で `pdf_highlight` する。全文を読む経路は作っていない(トークンの無駄になるため)。
  100 ページ(本文約 5.7 万文字)の合成 PDF では、各ページの最初の行 + 1 ページで約 6.3 千文字(全文の約 11%)。

**完全ステートレス**: どのツールも、引数・入力 PDF・設定だけで結果が決まり、呼び出し間で何も覚えていない。
検索結果の位置は、PDF 上の実際の文字列+ページ+出現番号という自己完結の `ref`
(例 `{"text": "吾輩", "page": 1, "nth": 2}`)で表すので、検索と書き込みを別の主体(sub agent や別の呼び出し)が行っても成り立つ。

**設定例**(ホストの MCP 設定。パスは環境に合わせる)

```json
{
  "mcpServers": {
    "pdf-highlight": {
      "command": "D:\\20261117_PDF_hilight\\.venv\\Scripts\\python.exe",
      "args": ["D:\\20261117_PDF_hilight\\pdf_highlight_mcp.py"],
      "env": { "PDF_HIGHLIGHT_ALLOWED_DIRS": "D:\\pdfs" }
    }
  }
}
```

| 環境変数 | 意味 |
|---|---|
| `PDF_HIGHLIGHT_ALLOWED_DIRS` | 読み書きを許すフォルダ(複数なら `;` 区切り)。**省略すると、サーバ起動時の作業フォルダだけ** |
| `PDF_HIGHLIGHT_ALLOW_OVERWRITE` | `1` で、既存ファイルの上書きを許す(既定: 許さない) |
| `PDF_HIGHLIGHT_MAX_MB` / `_MAX_PAGES` / `_TIMEOUT` | 入力サイズ(既定 100)/ ページ数(2000)/ 1 呼び出しの秒数(60)の上限 |
| `PDF_HIGHLIGHT_PASSWORD` | 暗号化 PDF のパスワード(会話に出さずに済む) |

**安全装置**: 許可フォルダの外は読み書きできない / 入力と同じパスには書かない / 既存ファイルは上書きしない /
一致が 50 件を超える語、1 回で 200 件を超える書き込みは拒否 / 最良候補と 2 位の差が小さいあいまい指定は書き込まず候補を返す /
同じハイライトを再実行しても重複しない / エラーは `{"ok":false,"error":{code,message,hint}}` で返る。

**本文の取得について**: 語の間に空白の文字が無く、位置の隙間だけで語を分けている PDF(LaTeX 製など)では、
隙間の大きさから空白を補って返す(日本語など幅の広い文字の間には補わない)。段組みなど、PDF 内の文字の並びが
読み順と異なる PDF では、本文の順序も崩れる。

**現時点の未対応**(仕様書の P2 以降): 始点と終点で一節を指定、`pdf_clear_highlights`、
語境界(`whole_word`)、ページまたぎの検索。

## 照合ルール

- 空白・改行は無視して照合する → 行をまたぐ文字列も一致する。ハイライトは行ごとの矩形に分かれた 1 つの注釈になる。
- NFKC で正規化して照合する(全角英数と半角英数は同一視、「が」と「か」は区別)。
- 既定では大文字小文字を区別する(`-i` で無視)。
- 重複しない範囲で全出現を検索する(`aaa` の中の `aa` は 1 件)。

## 制限事項

- **縦書きは非対応**。
- **スキャン画像だけの PDF は不可**(テキスト層が無いページは警告し、`pages_without_text` に入る。OCR が別途必要)。
- 段組みなど、PDF 内の文字の並びが読み順と異なる場合、段をまたぐ検索は正しく一致しない。
- ルビ(ふりがな)が本文の文字列に混ざっている PDF では、ルビを含む部分が一致しないことがある。
- ハイライトは注釈として追加される。外観ストリームは持たないが、一般的なビューア(Acrobat / Edge / Chrome 等)で表示される。
  注釈を表示しないビューアでは見えない。
- 出力は pypdf で書き出すため、元 PDF の構造が一部変わる場合がある(署名付き PDF の署名は無効になる)。

## テスト

```powershell
python -m pytest tests -q
```

通常のページ、改行またぎ、ページまたぎ、全角/半角、濁点、MediaBox 原点のずれ、ページ回転(90/180/270 度)、
暗号化 PDF、入出力形式、CLI を検証している(注釈位置は描画した画像で確認)。
LLM ツール層は、`ref` の往復(検索した候補が必ず同じ箇所に戻る)、別プロセスでの検索→書き込み、
パス制限、上限、エラーコード、MCP の実接続(サーバを子プロセスで起動し公式クライアントで呼ぶ)を検証している。

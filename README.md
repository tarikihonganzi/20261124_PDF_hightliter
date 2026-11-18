# pdf_highlight

PDF 内の指定文字列をハイライト(蛍光ペン注釈)します。日本語・改行またぎに対応(横書きのみ)。

## ライセンス

依存ライブラリはすべて商用利用可能で、有料のものはありません。

| ライブラリ | ライセンス | 用途 |
|---|---|---|
| pdfplumber | MIT | 文字ごとの座標取得 |
| pypdf | BSD-3-Clause | ハイライト注釈の書き込み |

(テスト用: pytest = MIT / reportlab = BSD-3 / pypdfium2 = Apache-2.0 または BSD-3)

## セットアップ

```powershell
pip install -r requirements.txt        # 実行のみ
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

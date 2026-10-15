# receipt-normalizer

[![CI](https://github.com/swatanabe-0/receipt-normalizer/actions/workflows/ci.yml/badge.svg)](https://github.com/swatanabe-0/receipt-normalizer/actions/workflows/ci.yml)

**形式の異なるレシート画像を、共通のスキーマに正規化して、SQLite に取り込む**最小構成のサンプルです。

OCR（Gemini API）で読み取り、日付や金額の書き方の揺れを統一し、疑わしいものには「要確認」の印を付け、同じファイルを二重に登録しない、という一連の流れを、小さなコードで示しています。

> **このリポジトリについて**
> ポートフォリオ用に、ゼロから書いたサンプルです。実在の店・データ・業務のコードは、一切含みません。
> サンプル画像の店名・品目は、すべて架空のものです。

## 解決したい課題

発行元ごとに、書式がバラバラな帳票を、1つのデータベースにまとめたい。

| 揺れの例      | レシートでの現れ方                                         |
| ------------- | ---------------------------------------------------------- |
| 日付の書式    | `2025/03/07` / `令和7年3月8日(土)` / `R7.3.9` / `2025-03-11` |
| 数字の書式    | `¥1,280` / `1,060円` / `２１０`（全角）/ `▲¥ 100`（値引き） |
| 税の扱い      | 内税（明細に含む）/ 外税（小計 + 消費税 = 合計）            |
| レイアウト    | 日本語 / 英字 / 表形式 / 項目の有無                         |
| 読み取りの誤り | 値引き行の読み落とし、日付が切れている                      |

## 設計方針

形式ごとのテンプレートを作る方式は、形式が増えるたびに破綻します。そこで、次の方針にしています。

| 方針                                     | 理由                                                                                                         |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| **モデルには「読み取り」だけを任せる**   | 日付や金額の変換まで任せると、結果がぶれて、テストもできない                                                 |
| **変換は、コードで決定的に行う**         | 書き方の揺れへの対応を、テストで確かめながら増やせる（`normalize.py`）                                       |
| **共通のスキーマを先に決める**           | 形式ごとの分岐を作らずに、スキーマに合わせて出力させる（`schema.py`）                                        |
| **疑わしいものは、黙って取り込まない**   | 「明細の合計と合計金額が合わない」などを検出して、要確認の印を付ける（`validate.py`）                        |
| **読み取り結果を、そのまま残す**         | あとから、変換や検証が正しかったかを確かめられる（`raw_json`）                                               |
| **同じ入力で、何度実行しても同じ結果**   | ファイルの内容のハッシュで重複を判定し、再実行しても二重登録しない（`store.py`）                             |
| **1件の失敗で、全体を止めない**          | 失敗は記録して次へ進み、次回の実行でやり直す（`pipeline.py`）                                                |
| **読み取り方法を差し替えられる**         | `Extractor` という小さな約束事にして、API キー無しでも全体を動かせる・テストできる（`extract.py`）           |

## 構成

```mermaid
flowchart LR
    A[画像 / PDF] --> B[extract<br/>読み取り]
    B -->|印字されたままの文字列| C[normalize<br/>正規化]
    C --> D[validate<br/>検証]
    D --> E[(SQLite)]
    D -.疑わしい.-> F[要確認フラグ]
    F --> E
    B -.失敗.-> G[error として記録<br/>次回やり直す]
    G --> E
```

```
src/receipt_normalizer/
  schema.py     データの型（読み取り結果 / 正規化後）
  extract.py    読み取り（GeminiExtractor / FixtureExtractor）
  normalize.py  日付・金額・数量の正規化
  validate.py   検証ルール（要確認フラグ）
  store.py      SQLite への保存、集計
  pipeline.py   取り込みの流れ（1ファイル単位）
  cli.py        コマンドライン
scripts/
  generate_samples.py   ダミーのレシート画像の生成
samples/receipts/       生成済みのサンプル（画像と、読み取り結果の JSON）
tests/                  テスト（API キー不要）
```

## 使い方

### 1. セットアップ

Python 3.11 以上が必要です。

```bash
python -m venv .venv
.venv\Scripts\activate        # macOS / Linux は: source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. API キー無しで、動作を確かめる

`--extractor fixture` は、画像の隣にある `*.raw.json`（読み取り結果）を使います。取り込みの流れ全体を、API 無しで試せます。

```bash
receipt-normalizer ingest samples/receipts --db demo.db --extractor fixture
receipt-normalizer report --db demo.db
```

実行結果の例です。

```
[ok     ] receipt_01_bakery.png
[ok     ] receipt_02_stationery.png
...
[review ] receipt_06_bento.png  TOTAL_MISMATCH
[review ] receipt_07_florist.png  NO_DATE
[skip   ] receipt_08_duplicate.png
[error  ] receipt_09_unreadable.png  読み取り結果の JSON がありません: ...

完了: 9 件  (error=1  needs_review=2  ok=5  skipped=1)
```

もう一度実行すると、取り込み済みのものは `skip` になり、二重には登録されません（失敗した1件だけが、やり直されます）。

### 3. Gemini API で、実際に読み取る

```bash
# Windows (PowerShell)
$env:GEMINI_API_KEY = "（取得した API キー）"
# macOS / Linux
export GEMINI_API_KEY="（取得した API キー）"

receipt-normalizer ingest path/to/images --db receipts.db
```

- API キーは、環境変数で渡します。コードやリポジトリには、含めません（`.env` は `.gitignore` 済み）。
- 使うモデルは、環境変数 `GEMINI_MODEL` で変えられます（既定：`gemini-2.5-flash`）。

## サンプルの内容

`samples/receipts/` には、形式と状況の異なるダミーのレシートがあります。`scripts/generate_samples.py` で作り直せます。

| ファイル                  | 内容                                                  | 結果       |
| ------------------------- | ----------------------------------------------------- | ---------- |
| `receipt_01_bakery`       | 内税、`2025/03/07 12:34`、`¥` 表記                    | ok         |
| `receipt_02_stationery`   | 和暦 `令和7年3月8日`、`円` 表記                       | ok         |
| `receipt_03_grocery`      | `R7.3.9`、全角数字                                    | ok         |
| `receipt_04_cafe`         | 外税（小計 + 消費税 = 合計）、`×2` の数量             | ok         |
| `receipt_05_drugstore`    | 英字表記、通貨記号なし                                | ok         |
| `receipt_06_bento`        | 値引き行を読み落とした（明細の合計 ≠ 合計金額）       | 要確認     |
| `receipt_07_florist`      | 日付が切れていて読めない                              | 要確認     |
| `receipt_08_duplicate`    | `receipt_01` と同じファイル                           | スキップ   |
| `receipt_09_unreadable`   | 読み取れない画像（読み取り結果なし）                  | エラー     |

## テスト

```bash
pytest          # API キーは不要。読み取りは、モックで置き換えている
ruff check .    # 静的チェック
ruff format --check .
```

主な確認内容は次の通りです。

- 日付・金額の書式の揺れの変換（和暦、全角、符号の付き方など）
- 検証ルール（内税 / 外税、値引き、未来の日付など）
- 読み取り API の再試行（一時的な失敗、不正な JSON、上限回数）
- 取り込みの冪等性、失敗したファイルのやり直し

GitHub Actions で、push のたびに自動実行しています。

## 制限と、今後の課題

正直に書いておきます。

- **Gemini API を実際に呼んだときの、読み取り精度は評価していません。** テストでは、API を呼ぶ部分をモックにしています。精度は、画像の品質とモデルによって変わります。
- 重複の判定は、**ファイルの内容が完全に同じ**場合だけです。同じレシートを、別に撮影した場合は、別物として扱います。
- 金額は、**円（整数）** だけを扱います。外貨や、小数点以下は扱いません。
- 保存先は SQLite です。実運用では、RDB への置き換えや、要確認データの確認画面が必要です。
- 税率ごとの内訳（8% と 10%）は、保存していません。

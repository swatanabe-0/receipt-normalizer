"""ダミーのレシート画像と、読み取り結果の JSON（`*.raw.json`）を生成する。

実在の店・データは、一切使っていない。すべて架空の店名と品目。
形式（レイアウト・日付の書き方・数字の書き方・税の表記）を、意図的にバラバラにしている。

    python scripts/generate_samples.py

画像の生成には Pillow と、日本語フォントが必要。見つからない場合は --font で指定する。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "samples" / "receipts"

FONT_CANDIDATES = [
    # Windows
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/NotoSansJP-VF.ttf",
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf",
]

WIDTH = 520
MARGIN = 28
LINE_HEIGHT = 36


@dataclass
class Row:
    """レシートの1行。左・中央・右の3つの欄に文字を置く。"""

    left: str = ""
    middle: str = ""
    right: str = ""
    size: int = 22
    rule: bool = False  # 区切り線


def rule() -> Row:
    return Row(rule=True)


def find_font(explicit: str | None) -> str:
    candidates = [explicit] if explicit else FONT_CANDIDATES
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit(
        "日本語フォントが見つかりません。--font でフォントファイル（.ttf / .ttc）を指定してください。"
    )


def render(rows: list[Row], font_path: str, path: Path) -> None:
    height = MARGIN * 2 + LINE_HEIGHT * len(rows)
    image = Image.new("RGB", (WIDTH, height), (252, 252, 250))
    draw = ImageDraw.Draw(image)
    y = MARGIN
    for row in rows:
        if row.rule:
            mid = y + LINE_HEIGHT // 2
            draw.line([(MARGIN, mid), (WIDTH - MARGIN, mid)], fill=(120, 120, 120), width=1)
        else:
            font = ImageFont.truetype(font_path, row.size)
            if row.left:
                draw.text((MARGIN, y), row.left, font=font, fill=(25, 25, 25))
            if row.middle:
                draw.text((WIDTH - 190, y), row.middle, font=font, fill=(25, 25, 25))
            if row.right:
                width = draw.textlength(row.right, font=font)
                draw.text((WIDTH - MARGIN - width, y), row.right, font=font, fill=(25, 25, 25))
        y += LINE_HEIGHT
    image.save(path)


def item(name: str, qty: str | None, amount: str) -> dict:
    return {"name": name, "quantity_text": qty, "amount_text": amount}


def build_specs() -> list[dict]:
    """画像に印字する内容(rows)と、「読み取りモデルが返す」結果(raw)の組。"""
    return [
        # 1. 内税・スラッシュ区切りの日付・円記号
        {
            "file": "receipt_01_bakery",
            "rows": [
                Row("ベーカリー あおぞら", size=26),
                Row("サンプル市 みどり町 1-2-3", size=18),
                Row("2025/03/07 12:34"),
                rule(),
                Row("クロワッサン", "2", "¥ 320"),
                Row("食パン 1斤", "1", "¥ 280"),
                Row("コーヒー", "1", "¥ 450"),
                rule(),
                Row("合計", right="¥1,050"),
                Row("(内消費税等 ¥77)", size=18),
            ],
            "raw": {
                "store_name": "ベーカリー あおぞら",
                "date_text": "2025/03/07 12:34",
                "items": [
                    item("クロワッサン", "2", "¥ 320"),
                    item("食パン 1斤", "1", "¥ 280"),
                    item("コーヒー", "1", "¥ 450"),
                ],
                "tax_text": "¥77",
                "total_text": "¥1,050",
            },
        },
        # 2. 和暦（令和）・「円」表記
        {
            "file": "receipt_02_stationery",
            "rows": [
                Row("文具店 みどり", size=26),
                Row("令和7年3月8日(土)"),
                rule(),
                Row("ノート A5", "3", "660円"),
                Row("ボールペン 黒", "2", "400円"),
                rule(),
                Row("合計", right="1,060円"),
                Row("(内税 96円)", size=18),
            ],
            "raw": {
                "store_name": "文具店 みどり",
                "date_text": "令和7年3月8日(土)",
                "items": [item("ノート A5", "3", "660円"), item("ボールペン 黒", "2", "400円")],
                "tax_text": "96円",
                "total_text": "1,060円",
            },
        },
        # 3. 和暦（R表記）・全角数字
        {
            "file": "receipt_03_grocery",
            "rows": [
                Row("まるみ商店", size=26),
                Row("R7.3.9  18:02"),
                rule(),
                Row("牛乳", "１", "２１０"),
                Row("卵 10個", "１", "２８０"),
                Row("食パン", "２", "４００"),
                rule(),
                Row("合　計", right="８９０"),
            ],
            "raw": {
                "store_name": "まるみ商店",
                "date_text": "R7.3.9  18:02",
                "items": [
                    item("牛乳", "１", "２１０"),
                    item("卵 10個", "１", "２８０"),
                    item("食パン", "２", "４００"),
                ],
                "tax_text": None,
                "total_text": "８９０",
            },
        },
        # 4. 外税（小計 + 消費税 = 合計）・「年月日」・「×」付きの数量
        {
            "file": "receipt_04_cafe",
            "rows": [
                Row("カフェ ひだまり", size=26),
                Row("2025年3月10日"),
                rule(),
                Row("ブレンド", "×2", "¥ 900"),
                Row("ケーキセット", "×1", "¥ 850"),
                rule(),
                Row("小計", right="¥1,750"),
                Row("消費税(10%)", right="¥175"),
                Row("合計", right="¥1,925"),
            ],
            "raw": {
                "store_name": "カフェ ひだまり",
                "date_text": "2025年3月10日",
                "items": [item("ブレンド", "×2", "¥ 900"), item("ケーキセット", "×1", "¥ 850")],
                "tax_text": "¥175",
                "total_text": "¥1,925",
            },
        },
        # 5. 英字表記・ハイフン区切りの日付・通貨記号なし
        {
            "file": "receipt_05_drugstore",
            "rows": [
                Row("HIDAMARI DRUG STORE", size=24),
                Row("DATE: 2025-03-11"),
                rule(),
                Row("ITEM", "QTY", "AMOUNT", size=18),
                Row("Vitamin C", "1", "1,280"),
                Row("Mask 30pcs", "2", "1,160"),
                Row("Tissue", "1", "350"),
                rule(),
                Row("TOTAL", right="2,790"),
                Row("TAX (incl.)", right="253", size=18),
            ],
            "raw": {
                "store_name": "HIDAMARI DRUG STORE",
                "date_text": "DATE: 2025-03-11",
                "items": [
                    item("Vitamin C", "1", "1,280"),
                    item("Mask 30pcs", "2", "1,160"),
                    item("Tissue", "1", "350"),
                ],
                "tax_text": "253",
                "total_text": "2,790",
            },
        },
        # 6. 【要確認】値引き行の読み落とし → 明細の合計と、合計金額が合わない
        {
            "file": "receipt_06_bento",
            "rows": [
                Row("お弁当の はなまる", size=26),
                Row("2025/03/12 11:48"),
                rule(),
                Row("日替わり弁当", "1", "¥ 580"),
                Row("お茶", "1", "¥ 150"),
                Row("サラダ", "1", "¥ 350"),
                Row("値引き", "", "▲¥ 100"),
                rule(),
                Row("合計", right="¥980"),
            ],
            "raw": {
                "store_name": "お弁当の はなまる",
                "date_text": "2025/03/12 11:48",
                # 値引き行を読み落とした、という想定
                "items": [
                    item("日替わり弁当", "1", "¥ 580"),
                    item("お茶", "1", "¥ 150"),
                    item("サラダ", "1", "¥ 350"),
                ],
                "tax_text": None,
                "total_text": "¥980",
            },
        },
        # 7. 【要確認】日付が切れていて読めない
        {
            "file": "receipt_07_florist",
            "rows": [
                Row("花の店 つぼみ", size=26),
                Row(""),
                rule(),
                Row("ブーケ（小）", "1", "¥ 1,800"),
                Row("メッセージカード", "1", "¥ 100"),
                rule(),
                Row("合計", right="¥1,900"),
            ],
            "raw": {
                "store_name": "花の店 つぼみ",
                "date_text": None,
                "items": [
                    item("ブーケ（小）", "1", "¥ 1,800"),
                    item("メッセージカード", "1", "¥ 100"),
                ],
                "tax_text": None,
                "total_text": "¥1,900",
            },
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", help="日本語フォントのファイル（.ttf / .ttc）")
    args = parser.parse_args()

    font_path = find_font(args.font)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for spec in build_specs():
        image_path = OUT_DIR / f"{spec['file']}.png"
        render(spec["rows"], font_path, image_path)
        raw_path = OUT_DIR / f"{spec['file']}.raw.json"
        raw_path.write_text(
            json.dumps(spec["raw"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"generated: {image_path.name}")

    # 8. 同じファイルを、もう一度置いた場合（重複の取り込み防止の確認用）
    shutil.copyfile(OUT_DIR / "receipt_01_bakery.png", OUT_DIR / "receipt_08_duplicate.png")
    shutil.copyfile(
        OUT_DIR / "receipt_01_bakery.raw.json", OUT_DIR / "receipt_08_duplicate.raw.json"
    )
    print("generated: receipt_08_duplicate.png (receipt_01 の複製)")

    # 9. 読み取れない画像（読み取り結果の JSON が無い → 失敗の扱いの確認用）
    Image.new("RGB", (WIDTH, 220), (250, 250, 250)).save(OUT_DIR / "receipt_09_unreadable.png")
    print("generated: receipt_09_unreadable.png")


if __name__ == "__main__":
    sys.exit(main())

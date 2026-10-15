"""データの形（スキーマ）の定義。

読み取り（OCR）の結果と、正規化したあとの結果を、別の型として分けている。

- Raw*  : 画像に印字されている通りの「文字列」。モデルには、これだけを出力させる。
- 正規化後 : 日付は date、金額は int など、比較・集計できる型。変換はコードで行う（テストできる）。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# 読み取り結果（印字されたままの文字列）
#
# 注意：Gemini の response_schema に渡すため、デフォルト値は持たせない
# （すべて必須。読めない項目は null を返させる）。
# --------------------------------------------------------------------------


class RawLineItem(BaseModel):
    """明細1行。印字されている通りに書き写したもの。"""

    name: str | None = Field(description="品名。印字されたまま。読めなければ null")
    quantity_text: str | None = Field(
        description="数量。印字されたまま（例: '2', '×2'）。印字がなければ null"
    )
    amount_text: str | None = Field(
        description="その行の金額。印字されたまま（例: '¥1,280', '１，２８０円'）。値引きは符号も含める"
    )


class RawReceipt(BaseModel):
    """レシート1枚分の読み取り結果。値は、すべて印字されたままの文字列。"""

    store_name: str | None = Field(description="店名。読めなければ null")
    date_text: str | None = Field(
        description="日付（時刻があれば含めてよい）。印字されたまま。なければ null"
    )
    items: list[RawLineItem] = Field(description="明細。上から順に。なければ空の配列")
    tax_text: str | None = Field(description="消費税額。印字されたまま。なければ null")
    total_text: str | None = Field(description="合計金額。印字されたまま。なければ null")


# --------------------------------------------------------------------------
# 正規化後（比較・集計できる型）
# --------------------------------------------------------------------------


class LineItem(BaseModel):
    name: str
    quantity: int
    amount: int  # その行の金額（円）。値引きはマイナス


class Receipt(BaseModel):
    store_name: str | None
    purchased_on: date | None
    items: list[LineItem]
    tax: int | None
    total: int | None  # 円

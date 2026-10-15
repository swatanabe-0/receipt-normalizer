"""検証：正規化した結果が、筋の通ったものかを確かめる。

OCR は、読み間違い・読み落としが起きる。間違いを黙って取り込まないために、
疑わしいものには「フラグ」を付けて、人が確認できるようにする。
"""

from __future__ import annotations

from datetime import date

from .schema import Receipt

# フラグ名（検証結果として保存される）
NO_STORE_NAME = "NO_STORE_NAME"
NO_DATE = "NO_DATE"
FUTURE_DATE = "FUTURE_DATE"
NO_ITEMS = "NO_ITEMS"
NO_TOTAL = "NO_TOTAL"
NEGATIVE_TOTAL = "NEGATIVE_TOTAL"
TOTAL_MISMATCH = "TOTAL_MISMATCH"

# 人が読む説明（レポートで表示する）
DESCRIPTIONS = {
    NO_STORE_NAME: "店名が読み取れませんでした",
    NO_DATE: "日付が読み取れませんでした",
    FUTURE_DATE: "日付が未来になっています",
    NO_ITEMS: "明細が1行もありません",
    NO_TOTAL: "合計金額が読み取れませんでした",
    NEGATIVE_TOTAL: "合計金額がマイナスです",
    TOTAL_MISMATCH: "明細の合計と、合計金額が一致しません（読み落とし・読み間違いの可能性）",
    "UNPARSEABLE_DATE": "日付の書式を解釈できませんでした",
    "UNPARSEABLE_AMOUNT": "金額の書式を解釈できない項目がありました",
}


def validate(receipt: Receipt, *, today: date | None = None) -> list[str]:
    """疑わしい点のフラグを返す。問題がなければ空のリスト。"""
    today = today or date.today()
    flags: list[str] = []

    if not receipt.store_name:
        flags.append(NO_STORE_NAME)

    if receipt.purchased_on is None:
        flags.append(NO_DATE)
    elif receipt.purchased_on > today:
        flags.append(FUTURE_DATE)

    if not receipt.items:
        flags.append(NO_ITEMS)

    if receipt.total is None:
        flags.append(NO_TOTAL)
    elif receipt.total < 0:
        flags.append(NEGATIVE_TOTAL)

    if receipt.items and receipt.total is not None:
        items_sum = sum(item.amount for item in receipt.items)
        matches_inclusive = items_sum == receipt.total  # 内税（明細に税が含まれる）
        matches_exclusive = (
            receipt.tax is not None and items_sum + receipt.tax == receipt.total
        )  # 外税
        if not (matches_inclusive or matches_exclusive):
            flags.append(TOTAL_MISMATCH)

    return flags

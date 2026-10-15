"""正規化：印字されたままの文字列を、比較・集計できる型に変換する。

LLM には「読み取り」だけを任せ、変換はここで決定的に行う。
こうすると、書き方の揺れ（和暦、全角数字、桁区切り、通貨記号など）への対応を、
テストで確かめながら増やしていける。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from .schema import LineItem, RawReceipt, Receipt

# 和暦の「元年」の前年（令和1年 = 2019 → 2018 + 1）
_ERA_BASE = {"令和": 2018, "R": 2018, "平成": 1988, "H": 1988, "昭和": 1925, "S": 1925}

_NEGATIVE_MARKS = "-−▲△"

# フラグ名（検証結果として保存される）
UNPARSEABLE_DATE = "UNPARSEABLE_DATE"
UNPARSEABLE_AMOUNT = "UNPARSEABLE_AMOUNT"


def to_halfwidth(text: str) -> str:
    """全角の数字・記号・英字を半角にそろえる（NFKC）。"""
    return unicodedata.normalize("NFKC", text)


def parse_amount(text: str | None) -> int | None:
    """金額の文字列を、円の整数にする。

    例: '¥1,280' -> 1280 / '１，２８０円' -> 1280 / '▲100' -> -100 / '1,280-' -> -1280
    読み取れないときは ValueError。None は None のまま返す。
    """
    if text is None:
        return None
    s = to_halfwidth(text).replace("¥", " ").replace("円", " ")

    # 画像の字間が広いと、モデルは数字を1文字ずつ空白で区切って書き写すことがある（'8 9 0'）。
    # 1桁ずつ区切られた数字は、つなげて1つの数として扱う。
    s = re.sub(r"(?<!\d)\d(?:\s+\d)+(?!\d)", lambda m: re.sub(r"\s+", "", m.group()), s)

    match = re.search(r"([-−▲△]?)\s*(\d[\d,]*)(?:\.(\d+))?\s*([-−])?", s)
    if not match:
        raise ValueError(f"金額として読み取れません: {text!r}")

    # 読み取った数のほかに、数字が残っているなら、一部だけを読んでしまった可能性がある。
    # 黙って「一部の数」を採用すると、間違いに気づけない（'8 9 0' を 8 と読む、など）ので、失敗として扱う。
    leftover = s[: match.start()] + s[match.end() :]
    if re.search(r"\d", leftover):
        raise ValueError(f"金額に、読み取りきれない数字が含まれています: {text!r}")

    prefix, integer_part, fraction, suffix = match.groups()
    if fraction and set(fraction) != {"0"}:
        raise ValueError(f"円未満の金額は扱えません: {text!r}")

    value = int(integer_part.replace(",", ""))
    has_negative_prefix = prefix != "" and prefix in _NEGATIVE_MARKS
    has_negative_suffix = suffix is not None  # '1,280-' のように、後ろに付く書き方
    return -value if (has_negative_prefix or has_negative_suffix) else value


def parse_quantity(text: str | None) -> int:
    """数量。読み取れない・印字がないときは 1 とする。"""
    if text is None:
        return 1
    match = re.search(r"\d+", to_halfwidth(text))
    return int(match.group()) if match else 1


def parse_date(text: str | None) -> date | None:
    """日付の文字列を date にする。

    対応: 2025/03/07, 2025-03-07, 2025年3月7日, 令和7年3月7日, R7.3.7, 25/03/07
    （時刻や曜日が付いていてもよい）。読み取れないときは ValueError。
    """
    if text is None:
        return None
    s = to_halfwidth(text)

    # 和暦（令和・平成・昭和 / R・H・S）
    era = re.search(
        r"(令和|平成|昭和|(?<![A-Za-z])[RHS])\s*(元|\d{1,2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})",
        s,
        flags=re.IGNORECASE,
    )
    if era:
        name, year_text, month, day = era.groups()
        base = _ERA_BASE[name.upper() if name.isascii() else name]
        year = base + (1 if year_text == "元" else int(year_text))
        return date(year, int(month), int(day))

    # 西暦4桁
    full = re.search(r"(\d{4})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})", s)
    if full:
        year, month, day = full.groups()
        return date(int(year), int(month), int(day))

    # 西暦2桁（2000年代として扱う）
    short = re.search(r"(?<!\d)(\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})(?!\d)", s)
    if short:
        year, month, day = short.groups()
        return date(2000 + int(year), int(month), int(day))

    raise ValueError(f"日付として読み取れません: {text!r}")


def normalize(raw: RawReceipt) -> tuple[Receipt, list[str]]:
    """読み取り結果を正規化する。

    変換できない値は、例外にせず「フラグ」として返す（取り込み自体は続け、人が確認できるようにする）。
    """
    flags: list[str] = []

    purchased_on = None
    try:
        purchased_on = parse_date(raw.date_text)
    except ValueError:
        flags.append(UNPARSEABLE_DATE)

    def amount_or_flag(text: str | None) -> int | None:
        try:
            return parse_amount(text)
        except ValueError:
            flags.append(UNPARSEABLE_AMOUNT)
            return None

    items: list[LineItem] = []
    for item in raw.items:
        amount = amount_or_flag(item.amount_text)
        if amount is None:
            continue  # 金額が読めない行は取り込まない（フラグで人に知らせる）
        name = (item.name or "").strip() or "（品名なし）"
        items.append(
            LineItem(name=name, quantity=parse_quantity(item.quantity_text), amount=amount)
        )

    receipt = Receipt(
        store_name=(raw.store_name or "").strip() or None,
        purchased_on=purchased_on,
        items=items,
        tax=amount_or_flag(raw.tax_text),
        total=amount_or_flag(raw.total_text),
    )
    return receipt, flags

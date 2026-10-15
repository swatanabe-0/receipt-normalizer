from datetime import date

import pytest

from receipt_normalizer.normalize import (
    UNPARSEABLE_AMOUNT,
    UNPARSEABLE_DATE,
    normalize,
    parse_amount,
    parse_date,
    parse_quantity,
)
from receipt_normalizer.schema import RawLineItem, RawReceipt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¥1,280", 1280),
        ("¥ 320", 320),
        ("1,060円", 1060),
        ("１，２８０円", 1280),  # 全角
        ("８９０", 890),
        ("2,790", 2790),
        ("▲¥ 100", -100),  # 値引き
        ("-300", -300),
        ("−300", -300),  # U+2212
        ("1,280-", -1280),  # 後ろに付くマイナス
        ("¥1,000.00", 1000),
        (None, None),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["", "なし", "¥ー", "12.5円"])
def test_parse_amount_rejects_unreadable_values(text):
    with pytest.raises(ValueError):
        parse_amount(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2", 2), ("×2", 2), ("x3", 3), ("２点", 2), ("", 1), (None, 1), ("-", 1)],
)
def test_parse_quantity(text, expected):
    assert parse_quantity(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025/03/07 12:34", date(2025, 3, 7)),
        ("2025-03-07", date(2025, 3, 7)),
        ("2025年3月7日(金)", date(2025, 3, 7)),
        ("令和7年3月8日(土)", date(2025, 3, 8)),
        ("令和元年5月1日", date(2019, 5, 1)),
        ("平成30年12月31日", date(2018, 12, 31)),
        ("R7.3.9  18:02", date(2025, 3, 9)),
        ("r7.3.9", date(2025, 3, 9)),
        ("H30/1/2", date(2018, 1, 2)),
        ("DATE: 2025-03-11", date(2025, 3, 11)),
        ("２０２５／０３／０７", date(2025, 3, 7)),  # 全角
        ("25/03/07", date(2025, 3, 7)),
        (None, None),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["", "昨日", "2025/13/40", "令和7年2月30日"])
def test_parse_date_rejects_invalid_dates(text):
    with pytest.raises(ValueError):
        parse_date(text)


def test_normalize_collects_flags_instead_of_failing():
    raw = RawReceipt(
        store_name="  まるみ商店 ",
        date_text="いつか",
        items=[
            RawLineItem(name="牛乳", quantity_text="１", amount_text="２１０"),
            RawLineItem(name="謎の品", quantity_text=None, amount_text="読めない"),
        ],
        tax_text=None,
        total_text="¥210",
    )

    receipt, flags = normalize(raw)

    assert receipt.store_name == "まるみ商店"  # 前後の空白は取り除く
    assert receipt.purchased_on is None
    assert [i.name for i in receipt.items] == ["牛乳"]  # 金額が読めない行は、取り込まない
    assert receipt.total == 210
    assert flags == [UNPARSEABLE_DATE, UNPARSEABLE_AMOUNT]

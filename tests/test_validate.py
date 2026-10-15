from datetime import date

from receipt_normalizer.schema import LineItem, Receipt
from receipt_normalizer.validate import (
    FUTURE_DATE,
    NEGATIVE_TOTAL,
    NO_DATE,
    NO_ITEMS,
    NO_STORE_NAME,
    NO_TOTAL,
    TOTAL_MISMATCH,
    validate,
)

TODAY = date(2026, 1, 1)


def make_receipt(**overrides) -> Receipt:
    base = {
        "store_name": "テスト商店",
        "purchased_on": date(2025, 3, 7),
        "items": [
            LineItem(name="A", quantity=1, amount=300),
            LineItem(name="B", quantity=1, amount=700),
        ],
        "tax": None,
        "total": 1000,
    }
    base.update(overrides)
    return Receipt(**base)


def test_valid_receipt_has_no_flags():
    assert validate(make_receipt(), today=TODAY) == []


def test_tax_exclusive_receipt_is_valid():
    receipt = make_receipt(tax=100, total=1100)  # 明細の合計 + 消費税 = 合計
    assert validate(receipt, today=TODAY) == []


def test_total_mismatch_is_flagged():
    assert validate(make_receipt(total=1100), today=TODAY) == [TOTAL_MISMATCH]


def test_discount_line_is_summed():
    items = [
        LineItem(name="A", quantity=1, amount=1000),
        LineItem(name="値引き", quantity=1, amount=-100),
    ]
    assert validate(make_receipt(items=items, total=900), today=TODAY) == []


def test_missing_fields_are_flagged():
    receipt = make_receipt(store_name=None, purchased_on=None, items=[], total=None)
    flags = validate(receipt, today=TODAY)
    assert flags == [NO_STORE_NAME, NO_DATE, NO_ITEMS, NO_TOTAL]


def test_future_date_is_flagged():
    receipt = make_receipt(purchased_on=date(2026, 6, 1))
    assert validate(receipt, today=TODAY) == [FUTURE_DATE]


def test_negative_total_is_flagged():
    items = [LineItem(name="返品", quantity=1, amount=-500)]
    flags = validate(make_receipt(items=items, total=-500), today=TODAY)
    assert flags == [NEGATIVE_TOTAL]

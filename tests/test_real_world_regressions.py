"""実際の API で試して見つかった問題の、再発防止テスト。"""

import pytest

from receipt_normalizer.normalize import normalize, parse_amount
from receipt_normalizer.schema import RawLineItem, RawReceipt
from receipt_normalizer.validate import validate


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("8 9 0", 890),  # 字間が広い画像で、モデルが1文字ずつ空白で区切って返した
        ("２ １ ０", 210),
        ("¥ 1 2 8 0", 1280),
        ("(内税 96円)", 96),  # 説明の文字が付いていても、数字が1つなら読める
        ("¥ 320", 320),
    ],
)
def test_digit_by_digit_transcription_is_joined(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1,050 77",  # 2つの数が並んでいる（どちらが金額か分からない）
        "¥1,050 (内税 77)",
    ],
)
def test_ambiguous_amounts_are_rejected_instead_of_guessed(text):
    """一部の数だけを、黙って採用しない（'8 9 0' を 8 と読んでいた不具合の再発防止）。"""
    with pytest.raises(ValueError, match="数字"):
        parse_amount(text)


def test_actual_model_output_for_spaced_digits_is_normalized_correctly():
    # 実際の API が返した結果（字間の広い全角数字のレシート）。以前は、合計が 8 円になっていた。
    raw = RawReceipt(
        store_name="まるみ商店",
        date_text="R7.3.9 18:02",
        items=[
            RawLineItem(name="牛乳", quantity_text="1", amount_text="2 1 0"),
            RawLineItem(name="卵 10個", quantity_text="1", amount_text="2 8 0"),
            RawLineItem(name="食パン", quantity_text="2", amount_text="4 0 0"),
        ],
        tax_text=None,
        total_text="8 9 0",
    )

    receipt, normalize_flags = normalize(raw)

    assert receipt.total == 890
    assert [item.amount for item in receipt.items] == [210, 280, 400]
    assert normalize_flags == []
    assert validate(receipt) == []


def test_unreadable_amount_surfaces_as_review_not_silent_success():
    raw = RawReceipt(
        store_name="テスト商店",
        date_text="2025/03/07",
        items=[RawLineItem(name="A", quantity_text="1", amount_text="1,050 77")],
        tax_text=None,
        total_text="1,050",
    )

    receipt, normalize_flags = normalize(raw)
    flags = normalize_flags + validate(receipt)

    # 曖昧な明細は取り込まず、フラグが付く（人が確認できる）
    assert "UNPARSEABLE_AMOUNT" in flags
    assert receipt.items == []

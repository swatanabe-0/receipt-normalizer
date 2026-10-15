import json
from datetime import date
from pathlib import Path

import pytest

from receipt_normalizer.cli import main
from receipt_normalizer.evaluate import compare, evaluate_directory, load_truth, render_text
from receipt_normalizer.extract import ExtractionError, FixtureExtractor, RecordedExtractor
from receipt_normalizer.schema import LineItem, Receipt

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples" / "receipts"
TRUTH = ROOT / "samples" / "ground_truth"
RECORDED = ROOT / "samples" / "recorded" / "gemini-3.8-flash"
TODAY = date(2025, 12, 31)


def receipt(**overrides) -> Receipt:
    base = {
        "store_name": "テスト商店",
        "purchased_on": date(2025, 3, 7),
        "items": [
            LineItem(name="牛乳", quantity=1, amount=210),
            LineItem(name="食パン", quantity=2, amount=400),
        ],
        "tax": None,
        "total": 610,
    }
    base.update(overrides)
    return Receipt(**base)


# --- 比較（compare）---------------------------------------------------------


def test_identical_receipts_are_correct():
    assert compare(receipt(), receipt()).correct


def test_store_name_ignores_width_and_spaces():
    got = receipt(store_name="テスト　商店")  # 全角スペース
    assert compare(receipt(store_name="テスト商店"), got).store


def test_each_wrong_field_is_reported():
    got = receipt(store_name="別の店", purchased_on=date(2025, 3, 8), total=999, tax=10)
    score = compare(receipt(), got)

    assert not score.correct
    assert score.wrong_fields() == ["店名", "日付", "合計", "消費税"]


def test_missing_item_lowers_recall_but_not_precision():
    got = receipt(items=[LineItem(name="牛乳", quantity=1, amount=210)])
    score = compare(receipt(), got)

    assert (score.items_matched, score.items_expected, score.items_predicted) == (1, 2, 1)
    assert not score.items_exact
    assert score.wrong_fields() == ["明細"]


def test_extra_item_lowers_precision():
    extra = LineItem(name="謎の品", quantity=1, amount=100)
    got = receipt(items=[*receipt().items, extra])
    score = compare(receipt(), got)

    assert (score.items_matched, score.items_expected, score.items_predicted) == (2, 2, 3)


def test_item_amount_mismatch_is_not_matched():
    got = receipt(items=[LineItem(name="牛乳", quantity=1, amount=21), receipt().items[1]])
    assert compare(receipt(), got).items_matched == 1


# --- ディレクトリ全体の評価 --------------------------------------------------


def test_recorded_real_model_outputs_are_all_correct():
    summary = evaluate_directory(SAMPLES, TRUTH, RecordedExtractor(RECORDED), today=TODAY)

    assert len(summary._read()) == 6
    assert all(c.correct for c in summary._read())
    assert summary.silent_errors == []
    # 取得できていない2件は、「読み取れず」として、正直に区別される
    assert sorted(c.name for c in summary.unread) == [
        "receipt_07_florist.png",
        "receipt_09_unreadable.png",
    ]
    # 正解データがない（重複した）ファイルは、対象外
    assert summary.skipped_without_truth == ["receipt_08_duplicate.png"]


def test_fixture_with_a_simulated_misread_is_caught_not_silent():
    summary = evaluate_directory(SAMPLES, TRUTH, FixtureExtractor(), today=TODAY)

    wrong = {c.name: c for c in summary.wrong}
    assert list(wrong) == ["receipt_06_bento.png"]  # 値引き行の読み落とし
    assert wrong["receipt_06_bento.png"].flags == ["TOTAL_MISMATCH"]
    assert summary.silent_errors == []  # 間違いは、すべて「要確認」になっている


def test_correctly_reading_a_missing_date_is_not_an_error():
    summary = evaluate_directory(SAMPLES, TRUTH, FixtureExtractor(), today=TODAY)
    florist = next(c for c in summary.cases if c.name == "receipt_07_florist.png")

    assert florist.correct  # 日付が無いことを、正しく読み取れている
    assert florist.flags == ["NO_DATE"]  # そのうえで、人に確認を促している


def test_report_text_mentions_silent_error_count():
    summary = evaluate_directory(SAMPLES, TRUTH, RecordedExtractor(RECORDED), today=TODAY)
    text = render_text(summary)

    assert "黙って「正常」だった" in text
    assert "完全一致" in text


# --- 黙って通った誤りの検出（もっとも重要）----------------------------------


@pytest.fixture
def silently_wrong_case(tmp_path):
    """店名だけが間違っていて、金額は整合している（整合性の検証では見つからない誤り）。"""
    images = tmp_path / "images"
    truth = tmp_path / "ground_truth"
    recorded = tmp_path / "recorded"
    for directory in (images, truth, recorded):
        directory.mkdir()

    (images / "a.png").write_bytes(b"image")
    (truth / "a.json").write_text(
        json.dumps(
            {
                "store_name": "正しい店名",
                "purchased_on": "2025-03-07",
                "items": [{"name": "A", "quantity": 1, "amount": 100}],
                "tax": None,
                "total": 100,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (recorded / "a.json").write_text(
        json.dumps(
            {
                "store_name": "読み間違えた店名",  # ここだけが間違い
                "date_text": "2025/03/07",
                "items": [{"name": "A", "quantity_text": "1", "amount_text": "100"}],
                "tax_text": None,
                "total_text": "100",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return images, truth, recorded


def test_silent_error_is_detected(silently_wrong_case):
    images, truth, recorded = silently_wrong_case

    summary = evaluate_directory(images, truth, RecordedExtractor(recorded), today=TODAY)

    assert [c.name for c in summary.silent_errors] == ["a.png"]
    assert summary.wrong[0].score.wrong_fields() == ["店名"]
    assert "★黙って通った" in render_text(summary)


def test_cli_fails_on_silent_error_only_when_requested(silently_wrong_case, capsys):
    images, truth, recorded = silently_wrong_case
    base = [
        "evaluate",
        str(images),
        "--truth",
        str(truth),
        "--extractor",
        "recorded",
        "--recorded-dir",
        str(recorded),
    ]

    assert main(base) == 0  # 指定が無ければ、結果を表示するだけ
    assert main([*base, "--fail-on-silent-error"]) == 1  # CI では、ここで失敗させる
    assert "「正常」になったもの" in capsys.readouterr().err


def test_cli_passes_when_no_silent_error(capsys):
    code = main(
        [
            "evaluate",
            str(SAMPLES),
            "--extractor",
            "recorded",
            "--recorded-dir",
            str(RECORDED),
            "--fail-on-silent-error",
        ]
    )

    assert code == 0
    assert "完全一致" in capsys.readouterr().out


def test_cli_writes_json(tmp_path, capsys):
    out = tmp_path / "result.json"
    main(
        [
            "evaluate",
            str(SAMPLES),
            "--extractor",
            "recorded",
            "--recorded-dir",
            str(RECORDED),
            "--json",
            str(out),
        ]
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["read"] == 6
    assert data["silent_errors"] == []
    assert data["field_accuracy"]["合計"] == {"correct": 6, "total": 6}


# --- 記録した出力・設定の扱い ------------------------------------------------


def test_recorded_extractor_reports_missing_record(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b"x")

    with pytest.raises(ExtractionError, match="記録された出力がありません"):
        RecordedExtractor(tmp_path / "none").extract(image)


def test_recorded_extractor_requires_directory(capsys):
    code = main(["evaluate", str(SAMPLES), "--extractor", "recorded"])

    assert code == 2
    assert "--recorded-dir" in capsys.readouterr().err


def test_ground_truth_files_are_valid():
    files = sorted(TRUTH.glob("*.json"))
    assert len(files) == 8
    for path in files:
        load_truth(path)  # スキーマに合っていること

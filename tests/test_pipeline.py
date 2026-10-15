import json
from datetime import date
from pathlib import Path

import pytest

from receipt_normalizer import store
from receipt_normalizer.cli import main
from receipt_normalizer.extract import ExtractionError, FixtureExtractor
from receipt_normalizer.pipeline import (
    OUTCOME_ERROR,
    OUTCOME_NEEDS_REVIEW,
    OUTCOME_OK,
    OUTCOME_SKIPPED,
    process_directory,
    process_file,
)
from receipt_normalizer.schema import RawLineItem, RawReceipt

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "receipts"
TODAY = date(2025, 12, 31)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    yield connection
    connection.close()


def kinds(outcomes):
    return {o.path.name: o.kind for o in outcomes}


def test_sample_directory_is_ingested_as_expected(conn):
    outcomes = process_directory(SAMPLES, FixtureExtractor(), conn, today=TODAY)
    result = kinds(outcomes)

    # 形式の違う5枚は、そのまま取り込める
    for name in ["01_bakery", "02_stationery", "03_grocery", "04_cafe", "05_drugstore"]:
        assert result[f"receipt_{name}.png"] == OUTCOME_OK, name

    # 疑わしいものは「要確認」になる
    assert result["receipt_06_bento.png"] == OUTCOME_NEEDS_REVIEW
    assert result["receipt_07_florist.png"] == OUTCOME_NEEDS_REVIEW
    # 同じ内容のファイルは、二重に登録されない
    assert result["receipt_08_duplicate.png"] == OUTCOME_SKIPPED
    # 読み取れなかったものは、エラーとして記録され、他の処理は続く
    assert result["receipt_09_unreadable.png"] == OUTCOME_ERROR


def test_flags_explain_why_review_is_needed(conn):
    outcomes = {
        o.path.name: o for o in process_directory(SAMPLES, FixtureExtractor(), conn, today=TODAY)
    }

    assert outcomes["receipt_06_bento.png"].flags == ["TOTAL_MISMATCH"]
    assert outcomes["receipt_07_florist.png"].flags == ["NO_DATE"]


def test_normalized_values_are_stored(conn):
    process_directory(SAMPLES, FixtureExtractor(), conn, today=TODAY)

    row = conn.execute(
        "SELECT * FROM receipts WHERE source_name = ?", ("receipt_03_grocery.png",)
    ).fetchone()
    assert row["store_name"] == "まるみ商店"
    assert row["purchased_on"] == "2025-03-09"  # 'R7.3.9' → 西暦
    assert row["total"] == 890  # '８９０'（全角）→ 整数

    items = conn.execute(
        "SELECT name, quantity, amount FROM receipt_items WHERE receipt_id = ? ORDER BY id",
        (row["id"],),
    ).fetchall()
    assert [(i["name"], i["quantity"], i["amount"]) for i in items] == [
        ("牛乳", 1, 210),
        ("卵 10個", 1, 280),
        ("食パン", 2, 400),
    ]

    # 読み取り結果（印字されたまま）も、あとから検証できるように残している
    assert json.loads(row["raw_json"])["date_text"] == "R7.3.9  18:02"


def test_running_twice_does_not_create_duplicates(conn):
    process_directory(SAMPLES, FixtureExtractor(), conn, today=TODAY)
    before = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    second = process_directory(SAMPLES, FixtureExtractor(), conn, today=TODAY)

    after = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    assert after == before
    # 2回目は、エラーだった1件だけが、やり直される（他はすべてスキップ）
    assert sorted(set(kinds(second).values())) == [OUTCOME_ERROR, OUTCOME_SKIPPED]


def test_failed_file_is_retried_next_time(tmp_path, conn):
    image = tmp_path / "a.png"
    image.write_bytes(b"image-bytes")

    class FailingExtractor:
        def extract(self, path):
            raise ExtractionError("API が落ちています")

    class WorkingExtractor:
        def extract(self, path):
            return RawReceipt(
                store_name="復旧商店",
                date_text="2025/03/07",
                items=[RawLineItem(name="A", quantity_text="1", amount_text="100")],
                tax_text=None,
                total_text="100",
            )

    first = process_file(image, FailingExtractor(), conn, today=TODAY)
    assert first.kind == OUTCOME_ERROR
    assert "API が落ちています" in (first.message or "")

    second = process_file(image, WorkingExtractor(), conn, today=TODAY)
    assert second.kind == OUTCOME_OK
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_cli_ingest_and_report(tmp_path, capsys):
    db = tmp_path / "cli.db"

    exit_code = main(["ingest", str(SAMPLES), "--db", str(db), "--extractor", "fixture"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "ok=5" in out and "needs_review=2" in out and "error=1" in out and "skipped=1" in out

    assert main(["report", "--db", str(db)]) == 0
    report = capsys.readouterr().out
    assert "TOTAL_MISMATCH" in report
    assert "NO_DATE" in report
    assert "カフェ ひだまり" in report

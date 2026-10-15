"""設定・権限の問題（モデルが無い、キーが無効など）は、リトライせず、全体を止めて原因を伝える。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from receipt_normalizer import store
from receipt_normalizer.cli import main
from receipt_normalizer.extract import FatalExtractionError, GeminiExtractor
from receipt_normalizer.pipeline import process_directory, process_file

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "receipts"


class ApiError(Exception):
    """SDK の例外のように、HTTP ステータスを code として持つ。"""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code} {message}")
        self.code = code


class AlwaysFailingModels:
    def __init__(self, error: Exception):
        self._error = error
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        raise self._error


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "receipt.png"
    path.write_bytes(b"image-bytes")
    return path


@pytest.mark.parametrize("code", [401, 403, 404])
def test_configuration_errors_are_not_retried(image, code):
    models = AlwaysFailingModels(ApiError(code, "model is not available"))
    sleeps = []
    extractor = GeminiExtractor(
        model="old-model", client=SimpleNamespace(models=models), sleep=sleeps.append
    )

    with pytest.raises(FatalExtractionError) as info:
        extractor.extract(image)

    assert models.calls == 1  # 何度試しても直らないので、1回で中断する
    assert sleeps == []
    message = str(info.value)
    assert str(code) in message
    assert "old-model" in message  # どのモデルを指定したかを伝える
    assert "GEMINI_MODEL" in message  # 直し方を伝える


def test_transient_errors_are_still_retried(image):
    models = AlwaysFailingModels(ApiError(503, "unavailable"))
    extractor = GeminiExtractor(client=SimpleNamespace(models=models), sleep=lambda _: None)

    with pytest.raises(Exception, match="3回"):
        extractor.extract(image)

    assert models.calls == 3


def test_fatal_error_is_not_recorded_as_a_per_file_failure(tmp_path):
    class BrokenSetup:
        def extract(self, path):
            raise FatalExtractionError("モデルが利用できません")

    conn = store.connect(tmp_path / "t.db")

    with pytest.raises(FatalExtractionError):
        process_directory(SAMPLES, BrokenSetup(), conn)

    # 設定を直せば、そのまま全件を取り込める（失敗として残っていない）
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_process_file_propagates_fatal_error(image, tmp_path):
    class BrokenSetup:
        def extract(self, path):
            raise FatalExtractionError("キーが無効です")

    conn = store.connect(tmp_path / "t.db")
    with pytest.raises(FatalExtractionError):
        process_file(image, BrokenSetup(), conn)


def test_cli_stops_with_explanation(tmp_path, monkeypatch, capsys):
    class BrokenSetup:
        def extract(self, path):
            raise FatalExtractionError("モデルが利用できません（HTTP 404）")

    monkeypatch.setattr("receipt_normalizer.cli._build_extractor", lambda kind: BrokenSetup())

    code = main(["ingest", str(SAMPLES), "--db", str(tmp_path / "c.db")])

    assert code == 2
    captured = capsys.readouterr()
    assert "中断しました" in captured.err
    assert "HTTP 404" in captured.err
    assert "[error" not in captured.out  # 9件分のエラー表示が並ばない

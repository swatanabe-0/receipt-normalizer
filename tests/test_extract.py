import json
from types import SimpleNamespace

import pytest

from receipt_normalizer.extract import (
    ExtractionError,
    FixtureExtractor,
    GeminiExtractor,
    MissingApiKeyError,
)

VALID_JSON = json.dumps(
    {
        "store_name": "テスト商店",
        "date_text": "2025/03/07",
        "items": [{"name": "A", "quantity_text": "1", "amount_text": "¥100"}],
        "tax_text": None,
        "total_text": "¥100",
    },
    ensure_ascii=False,
)


class FakeModels:
    """generate_content の呼び出しを記録し、あらかじめ決めた結果を順に返す。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)


def make_extractor(responses, **kwargs):
    models = FakeModels(responses)
    sleeps = []
    extractor = GeminiExtractor(
        client=SimpleNamespace(models=models),
        sleep=sleeps.append,  # 実際には待たない
        **kwargs,
    )
    return extractor, models, sleeps


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "receipt.png"
    path.write_bytes(b"\x89PNG fake image bytes")
    return path


def test_gemini_extractor_returns_parsed_result(image):
    extractor, models, sleeps = make_extractor([VALID_JSON], model="test-model")

    raw = extractor.extract(image)

    assert raw.store_name == "テスト商店"
    assert raw.items[0].amount_text == "¥100"
    assert sleeps == []
    call = models.calls[0]
    assert call["model"] == "test-model"
    assert call["config"].response_mime_type == "application/json"  # JSON に固定している
    assert call["config"].temperature == 0


def test_gemini_extractor_retries_on_transient_error(image):
    extractor, models, sleeps = make_extractor([RuntimeError("503"), VALID_JSON])

    raw = extractor.extract(image)

    assert raw.total_text == "¥100"
    assert len(models.calls) == 2
    assert sleeps == [1]  # 1回目の失敗のあとに、1秒待つ


def test_gemini_extractor_retries_on_invalid_json(image):
    extractor, models, _ = make_extractor(["これは JSON ではありません", VALID_JSON])

    assert extractor.extract(image).store_name == "テスト商店"
    assert len(models.calls) == 2


def test_gemini_extractor_gives_up_after_max_attempts(image):
    extractor, models, sleeps = make_extractor([RuntimeError("down")] * 3, max_attempts=3)

    with pytest.raises(ExtractionError, match="3回"):
        extractor.extract(image)

    assert len(models.calls) == 3
    assert sleeps == [1, 2]  # 間隔を延ばして待つ（最後の失敗のあとは待たない）


def test_gemini_extractor_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError):
        GeminiExtractor()


def test_fixture_extractor_reads_sidecar_json(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"x")
    image.with_suffix(".raw.json").write_text(VALID_JSON, encoding="utf-8")

    assert FixtureExtractor().extract(image).store_name == "テスト商店"


def test_fixture_extractor_fails_without_sidecar(tmp_path):
    image = tmp_path / "b.png"
    image.write_bytes(b"x")

    with pytest.raises(ExtractionError):
        FixtureExtractor().extract(image)

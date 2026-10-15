"""回数制限（HTTP 429）への対応：サーバーが示す待ち時間に従う / 呼び出しの間隔をあける。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from receipt_normalizer.extract import GeminiExtractor, ThrottledExtractor
from receipt_normalizer.schema import RawReceipt

VALID_JSON = json.dumps(
    {
        "store_name": "テスト商店",
        "date_text": "2025/03/07",
        "items": [],
        "tax_text": None,
        "total_text": "¥100",
    },
    ensure_ascii=False,
)

# 実際の API が返した、回数制限のメッセージ（抜粋）
RATE_LIMIT_MESSAGE = (
    "429 RESOURCE_EXHAUSTED. You exceeded your current quota, limit: 5. "
    "Please retry in 59.674354853s."
)


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class ScriptedModels:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "receipt.png"
    path.write_bytes(b"image-bytes")
    return path


def make(responses, **kwargs):
    models = ScriptedModels(responses)
    sleeps: list[float] = []
    extractor = GeminiExtractor(
        client=SimpleNamespace(models=models), sleep=sleeps.append, **kwargs
    )
    return extractor, models, sleeps


def test_waits_for_the_time_suggested_by_the_server(image):
    extractor, models, sleeps = make([ApiError(429, RATE_LIMIT_MESSAGE), VALID_JSON])

    raw = extractor.extract(image)

    assert isinstance(raw, RawReceipt)
    assert models.calls == 2
    assert sleeps == [61]  # 59.67 秒 → 切り上げて 60、余裕を1秒足して 61


def test_wait_is_capped(image):
    message = "Please retry in 600s."
    extractor, _, sleeps = make([ApiError(429, message), VALID_JSON], max_rate_limit_wait=90)

    extractor.extract(image)

    assert sleeps == [90]  # 長すぎる待ち時間は、上限で打ち切る


def test_falls_back_to_a_long_wait_when_no_hint_is_given(image):
    extractor, _, sleeps = make([ApiError(429, "quota exceeded"), VALID_JSON])

    extractor.extract(image)

    assert sleeps == [15]  # 通常の再試行（1秒）より、長めに待つ


def test_rate_limit_message_is_logged(image, caplog):
    extractor, _, _ = make([ApiError(429, RATE_LIMIT_MESSAGE), VALID_JSON])

    with caplog.at_level("WARNING"):
        extractor.extract(image)

    assert "回数制限" in caplog.text
    assert image.name in caplog.text


class CountingExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, path: Path) -> RawReceipt:
        self.calls += 1
        return RawReceipt.model_validate_json(VALID_JSON)


def test_throttle_keeps_the_minimum_interval(image):
    now = [100.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    inner = CountingExtractor()
    throttled = ThrottledExtractor(inner, 12, sleep=fake_sleep, clock=lambda: now[0])

    throttled.extract(image)  # 1回目は、待たない
    now[0] += 5  # 5秒が経過したことにする
    throttled.extract(image)  # 12 - 5 = 7 秒、待つ
    now[0] += 20  # 十分に時間が空いた
    throttled.extract(image)  # 待たない

    assert inner.calls == 3
    assert sleeps == [pytest.approx(7)]

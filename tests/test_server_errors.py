"""実際の API で起きた、サーバー側の不調（503）と、1日の上限（429）への対応。"""

import json
from types import SimpleNamespace

import pytest

from receipt_normalizer.extract import (
    ExtractionError,
    FatalExtractionError,
    GeminiExtractor,
    _format_duration,
    _parse_duration,
    _suggested_delay,
)

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

# 実際の API が返した、1日の上限に達したときのメッセージ（抜粋）
DAILY_QUOTA_MESSAGE = (
    "429 RESOURCE_EXHAUSTED. You exceeded your current quota. limit: 20, model: gemini-3.8-flash\n"
    "Please retry in 17h44m2.693344759s. 'retryDelay': '63842s'"
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


def test_server_busy_waits_longer_between_retries(image):
    busy = "This model is currently experiencing high demand."
    extractor, models, sleeps = make(
        [ApiError(503, busy), ApiError(503, busy), VALID_JSON], max_attempts=3
    )

    extractor.extract(image)

    assert models.calls == 3
    assert sleeps == [5, 15]  # 1秒・2秒では、負荷の集中が収まらないことがある


def test_server_busy_gives_up_after_max_attempts(image):
    extractor, models, _ = make([ApiError(503, "busy")] * 3, max_attempts=3)

    with pytest.raises(ExtractionError) as info:
        extractor.extract(image)

    assert not isinstance(info.value, FatalExtractionError)  # 一時的な失敗。次回やり直せる
    assert models.calls == 3


def test_daily_quota_stops_immediately_with_explanation(image):
    extractor, models, sleeps = make([ApiError(429, DAILY_QUOTA_MESSAGE), VALID_JSON])

    with pytest.raises(FatalExtractionError) as info:
        extractor.extract(image)

    assert models.calls == 1  # 待っても回復しないので、すぐに中断する
    assert sleeps == []
    message = str(info.value)
    assert "17時間44分" in message  # いつ使えるようになるか
    assert "GEMINI_MODEL" in message  # 別のモデルを使う方法
    assert "重複しません" in message  # 再実行しても安全であること


def test_short_rate_limit_is_still_just_waited_out(image):
    extractor, models, sleeps = make([ApiError(429, "Please retry in 59.6s."), VALID_JSON])

    extractor.extract(image)

    assert models.calls == 2
    assert sleeps == [61]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("59.67s", 59.67),
        ("63842s", 63842),
        ("17h44m2.69s", 17 * 3600 + 44 * 60 + 2.69),
        ("2m30s", 150),
        ("1h", 3600),
    ],
)
def test_parse_duration(text, expected):
    assert _parse_duration(text) == pytest.approx(expected)


def test_parse_duration_rejects_garbage():
    assert _parse_duration("") is None
    assert _parse_duration("soon") is None


def test_suggested_delay_reads_both_message_formats():
    assert _suggested_delay(Exception("Please retry in 59.67s.")) == pytest.approx(59.67)
    assert _suggested_delay(Exception("{'retryDelay': '55s'}")) == 55
    assert _suggested_delay(Exception("no hint")) is None


def test_format_duration():
    assert _format_duration(63842) == "17時間44分"
    assert _format_duration(150) == "2分"
    assert _format_duration(20) == "1分"

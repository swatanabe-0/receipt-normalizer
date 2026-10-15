"""読み取り（OCR）：画像から、印字されたままの内容を取り出す。

読み取りの方法は差し替えられるように、Extractor という小さな約束事（Protocol）にしている。

- GeminiExtractor  : Gemini API で読み取る（実際に使うもの）
- FixtureExtractor : 画像の隣にある JSON を返す（API キーなしでの動作確認・テスト用）
"""

from __future__ import annotations

import logging
import math
import mimetypes
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .schema import RawReceipt

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"

PROMPT = """\
添付されたレシート（または領収書）の画像を読み取り、指定されたスキーマの JSON だけを返してください。

ルール:
- 画像に印字されている文字を、そのまま書き写してください。
  日付・金額・数量の書式を、整えたり変換したりしないでください（'令和7年3月7日'、'１，２８０円' などもそのまま）。
- 読み取れない項目や、画像に無い項目は、推測せずに null にしてください。
- 明細は、上から順に、すべての行を含めてください。値引き行は、符号も含めて書き写してください。
- 合計金額、消費税額は、印字されているものをそのまま返してください（自分で計算しないでください）。
"""


class ExtractionError(Exception):
    """読み取りに失敗した（リトライしても取得できなかった）。"""


class FatalExtractionError(ExtractionError):
    """設定・権限の問題（モデルが無い、キーが無効など）。

    何度やり直しても直らず、どのファイルでも同じ理由で失敗するため、
    ファイルごとの失敗として記録せず、処理全体を止めて、原因を伝える。
    """


class MissingApiKeyError(FatalExtractionError):
    """API キーが設定されていない。"""


# リトライしても直らない HTTP ステータス（認証・権限・モデルが存在しない）
_FATAL_HTTP_CODES = {401, 403, 404}

# 一時的なサーバー側の不調（負荷の集中など）。数秒では戻らないことがあるため、長めに待つ
_SERVER_BUSY_HTTP_CODES = {500, 502, 503, 504}

# サーバーが「この時間待って」と示す、待ち時間がこれを超えるなら、1日の上限などに達したと判断する
_QUOTA_EXHAUSTED_THRESHOLD_SECONDS = 600

_RETRY_AFTER_PATTERNS = (
    # 'Please retry in 59.67s.' / 'Please retry in 17h44m2.69s.'
    re.compile(r"retry in\s+((?:\d+h)?(?:\d+m)?[\d.]+s)", re.IGNORECASE),
    # "'retryDelay': '59s'"
    re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?([\dhms.]+)"),
)
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?")


def _parse_duration(text: str) -> float | None:
    """'59.67s' / '17h44m2.69s' / '63842s' を、秒数にする。"""
    match = _DURATION.fullmatch(text)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def _suggested_delay(error: Exception | None) -> float | None:
    """エラーに含まれる、サーバーが示す待ち時間（秒）。示されていなければ None。"""
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(str(error))
        if match:
            seconds = _parse_duration(match.group(1))
            if seconds is not None:
                return seconds
    return None


def _format_duration(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}時間{minutes}分"
    return f"{max(minutes, 1)}分"


def _retry_delay(error: Exception | None, attempt: int, *, max_wait: float) -> float:
    """再試行までの待ち時間（秒）。

    - 回数制限（429）: サーバーが示す待ち時間に従う（上限あり）。
    - サーバーの不調（5xx）: 5秒 → 15秒 → 45秒 と、長めに延ばす。
    - それ以外: 1秒 → 2秒 → 4秒 と延ばす。
    """
    code = getattr(error, "code", None)

    if code == 429:
        suggested = _suggested_delay(error)
        if suggested is None:
            return min(15 * attempt, max_wait)  # 示されないときは、長めに待つ
        return min(math.ceil(suggested) + 1, max_wait)

    if code in _SERVER_BUSY_HTTP_CODES:
        return min(5 * 3 ** (attempt - 1), max_wait)

    return 2 ** (attempt - 1)


class Extractor(Protocol):
    def extract(self, path: Path) -> RawReceipt: ...


class FixtureExtractor:
    """画像と同じ場所にある `<画像名>.raw.json` を、読み取り結果として返す。

    API キーなしで、取り込みの流れ全体を動かすためのもの。
    JSON が無いファイルは、読み取り失敗として扱う。
    """

    def extract(self, path: Path) -> RawReceipt:
        sidecar = path.with_suffix(".raw.json")
        if not sidecar.exists():
            raise ExtractionError(f"読み取り結果の JSON がありません: {sidecar.name}")
        return RawReceipt.model_validate_json(sidecar.read_text(encoding="utf-8"))


class GeminiExtractor:
    """Gemini API で、画像から RawReceipt を取り出す。

    - 出力は JSON（スキーマ指定）に固定し、temperature は 0（ばらつきを抑える）。
    - 通信エラーや、不正な JSON が返ってきた場合は、間隔を空けて再試行する。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        client: object | None = None,
        max_attempts: int = 3,
        max_rate_limit_wait: float = 90,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self._max_attempts = max_attempts
        self._max_rate_limit_wait = max_rate_limit_wait
        self._sleep = sleep

        if client is None:
            key = api_key or os.environ.get("GEMINI_API_KEY")
            if not key:
                raise MissingApiKeyError(
                    "環境変数 GEMINI_API_KEY が設定されていません"
                    "（API キーなしで試す場合は --extractor fixture を使います）"
                )
            from google import genai  # 必要なときだけ読み込む

            client = genai.Client(api_key=key)
        self._client = client

    def extract(self, path: Path) -> RawReceipt:
        from google.genai import types

        data = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._client.models.generate_content(  # type: ignore[attr-defined]
                    model=self._model,
                    contents=[types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RawReceipt,
                        temperature=0,
                    ),
                )
                return RawReceipt.model_validate_json(response.text)
            except Exception as error:
                if getattr(error, "code", None) in _FATAL_HTTP_CODES:
                    # 設定・権限の問題は、リトライしても直らないため、すぐに中断する
                    raise FatalExtractionError(
                        f"Gemini API を利用できません（HTTP {error.code}）。"
                        f"モデル名（現在: {self._model}）や、API キーの権限を確認してください。"
                        "モデルは、環境変数 GEMINI_MODEL で指定できます。\n"
                        f"詳細: {error}"
                    ) from error
                if getattr(error, "code", None) == 429:
                    suggested = _suggested_delay(error)
                    if suggested is not None and suggested > _QUOTA_EXHAUSTED_THRESHOLD_SECONDS:
                        # 1日の上限などに達している。待っても、この実行中には回復しない
                        raise FatalExtractionError(
                            "利用回数の上限（無料枠の1日あたりの制限など）に達しました。"
                            f"約{_format_duration(suggested)}後に、再び使えるようになります。\n"
                            "別のモデルを GEMINI_MODEL で指定するか、時間をおいて、もう一度実行してください"
                            "（取り込み済みのファイルは、再実行しても重複しません）。\n"
                            f"詳細: {error}"
                        ) from error
                # 不正な JSON（ValidationError）も、通信エラーなど SDK 側の例外も、同じく再試行の対象にする
                last_error = error

            if attempt < self._max_attempts:
                delay = _retry_delay(last_error, attempt, max_wait=self._max_rate_limit_wait)
                if getattr(last_error, "code", None) == 429:
                    logger.warning(
                        "回数制限のため、%d 秒待ってから再試行します（%s: %d/%d回目）",
                        delay,
                        path.name,
                        attempt,
                        self._max_attempts,
                    )
                self._sleep(delay)

        raise ExtractionError(
            f"{self._max_attempts}回試しましたが、読み取れませんでした: {last_error}"
        )


class ThrottledExtractor:
    """読み取りの間隔を、一定以上あけるための包み（回数制限のある API を、続けて呼ぶとき用）。

    例: 無料枠の「1分に5回」なら、間隔を 12 秒以上にすれば、制限に当たらない。
    """

    def __init__(
        self,
        inner: Extractor,
        min_interval: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    def extract(self, path: Path) -> RawReceipt:
        if self._last_call is not None:
            remaining = self._min_interval - (self._clock() - self._last_call)
            if remaining > 0:
                self._sleep(remaining)
        try:
            return self._inner.extract(path)
        finally:
            self._last_call = self._clock()

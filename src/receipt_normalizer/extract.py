"""読み取り（OCR）：画像から、印字されたままの内容を取り出す。

読み取りの方法は差し替えられるように、Extractor という小さな約束事（Protocol）にしている。

- GeminiExtractor  : Gemini API で読み取る（実際に使うもの）
- FixtureExtractor : 画像の隣にある JSON を返す（API キーなしでの動作確認・テスト用）
"""

from __future__ import annotations

import mimetypes
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .schema import RawReceipt

DEFAULT_MODEL = "gemini-2.5-flash"

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


class MissingApiKeyError(ExtractionError):
    """API キーが設定されていない。"""


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self._max_attempts = max_attempts
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
                # 不正な JSON（ValidationError）も、通信エラーなど SDK 側の例外も、同じく再試行の対象にする
                last_error = error

            if attempt < self._max_attempts:
                self._sleep(2 ** (attempt - 1))  # 1秒 → 2秒 と間隔を延ばす

        raise ExtractionError(
            f"{self._max_attempts}回試しましたが、読み取れませんでした: {last_error}"
        )

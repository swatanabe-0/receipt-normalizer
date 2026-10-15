"""取り込みの流れ：読み取り → 正規化 → 検証 → 保存。

1ファイルが失敗しても、他のファイルの取り込みは続ける。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import store
from .extract import Extractor
from .normalize import normalize
from .validate import validate

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}

# 取り込み結果の種類
OUTCOME_OK = store.STATUS_OK
OUTCOME_NEEDS_REVIEW = store.STATUS_NEEDS_REVIEW
OUTCOME_ERROR = store.STATUS_ERROR
OUTCOME_SKIPPED = "skipped"  # 取り込み済みのため、何もしなかった


@dataclass
class Outcome:
    path: Path
    kind: str
    flags: list[str] = field(default_factory=list)
    message: str | None = None


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_receipt_files(directory: Path) -> Iterator[Path]:
    """対象の画像（と PDF）を、名前順に返す。"""
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            yield path


def process_file(
    path: Path,
    extractor: Extractor,
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
) -> Outcome:
    """1ファイルを取り込む。"""
    digest = file_hash(path)

    existing = store.get_status(conn, digest)
    if existing is not None and existing != store.STATUS_ERROR:
        return Outcome(path, OUTCOME_SKIPPED)  # すでに取り込み済み（二重登録を防ぐ）
    if existing == store.STATUS_ERROR:
        store.delete(conn, digest)  # 前回の失敗は、やり直す

    try:
        raw = extractor.extract(path)
    except Exception as error:
        message = str(error)
        store.save_error(conn, file_hash=digest, source_name=path.name, message=message)
        return Outcome(path, OUTCOME_ERROR, message=message)

    receipt, normalize_flags = normalize(raw)
    flags = normalize_flags + validate(receipt, today=today)

    status = store.save_receipt(
        conn,
        file_hash=digest,
        source_name=path.name,
        receipt=receipt,
        flags=flags,
        raw_json=raw.model_dump_json(),
    )
    return Outcome(path, status, flags=flags)


def process_directory(
    directory: Path,
    extractor: Extractor,
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
) -> list[Outcome]:
    return [process_file(p, extractor, conn, today=today) for p in iter_receipt_files(directory)]

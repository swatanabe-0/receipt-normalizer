"""保存：正規化した結果を SQLite に保存する。

同じファイルを2回取り込んでも、二重に登録されないようにしている（ファイルの内容のハッシュで判定）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .schema import Receipt

STATUS_OK = "ok"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_ERROR = "error"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash     TEXT NOT NULL UNIQUE,   -- ファイル内容の SHA-256（重複排除のキー）
    source_name   TEXT NOT NULL,
    status        TEXT NOT NULL,          -- ok / needs_review / error
    flags         TEXT NOT NULL DEFAULT '[]',
    store_name    TEXT,
    purchased_on  TEXT,                   -- ISO 形式 (YYYY-MM-DD)
    tax           INTEGER,
    total         INTEGER,
    raw_json      TEXT,                   -- 読み取り結果（印字されたまま）。あとから検証できるよう残す
    error_message TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    quantity   INTEGER NOT NULL,
    amount     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """データベースに接続する（無ければ作る）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get_status(conn: sqlite3.Connection, file_hash: str) -> str | None:
    """そのファイルを、すでに取り込んでいるか。取り込み済みならその状態、なければ None。"""
    row = conn.execute("SELECT status FROM receipts WHERE file_hash = ?", (file_hash,)).fetchone()
    return row["status"] if row else None


def delete(conn: sqlite3.Connection, file_hash: str) -> None:
    """取り込み済みの記録を消す（失敗した記録を、やり直すときに使う）。"""
    conn.execute("DELETE FROM receipts WHERE file_hash = ?", (file_hash,))
    conn.commit()


def save_receipt(
    conn: sqlite3.Connection,
    *,
    file_hash: str,
    source_name: str,
    receipt: Receipt,
    flags: list[str],
    raw_json: str,
) -> str:
    """レシートと明細を、1つのトランザクションで保存する。保存した状態（ok / needs_review）を返す。"""
    status = STATUS_NEEDS_REVIEW if flags else STATUS_OK
    with conn:  # 途中で失敗したら、全体を取り消す
        cursor = conn.execute(
            """
            INSERT INTO receipts
              (file_hash, source_name, status, flags, store_name, purchased_on,
               tax, total, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_hash,
                source_name,
                status,
                json.dumps(flags, ensure_ascii=False),
                receipt.store_name,
                receipt.purchased_on.isoformat() if receipt.purchased_on else None,
                receipt.tax,
                receipt.total,
                raw_json,
                _now(),
            ),
        )
        receipt_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO receipt_items (receipt_id, name, quantity, amount) VALUES (?, ?, ?, ?)",
            [(receipt_id, i.name, i.quantity, i.amount) for i in receipt.items],
        )
    return status


def save_error(conn: sqlite3.Connection, *, file_hash: str, source_name: str, message: str) -> None:
    """読み取りに失敗したファイルを記録する（次回の実行で、やり直せるようにする）。"""
    with conn:
        conn.execute(
            """
            INSERT INTO receipts (file_hash, source_name, status, error_message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_hash, source_name, STATUS_ERROR, message, _now()),
        )


# --- レポート用の集計 --------------------------------------------------------


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM receipts GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}


def list_needing_attention(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """人の確認が必要なもの（要確認、または読み取り失敗）。"""
    return conn.execute(
        """
        SELECT source_name, status, flags, error_message
        FROM receipts WHERE status != ? ORDER BY source_name
        """,
        (STATUS_OK,),
    ).fetchall()


def totals_by_store(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """店ごとの件数と合計金額（正常に取り込めたものだけ）。"""
    return conn.execute(
        """
        SELECT store_name, COUNT(*) AS receipts, SUM(total) AS total
        FROM receipts WHERE status = ? AND store_name IS NOT NULL
        GROUP BY store_name ORDER BY total DESC
        """,
        (STATUS_OK,),
    ).fetchall()

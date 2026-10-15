"""コマンドライン：画像の取り込み（ingest）と、結果の確認（report）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import store
from .extract import (
    ExtractionError,
    Extractor,
    FatalExtractionError,
    FixtureExtractor,
    GeminiExtractor,
    ThrottledExtractor,
)
from .pipeline import (
    OUTCOME_ERROR,
    OUTCOME_NEEDS_REVIEW,
    OUTCOME_OK,
    OUTCOME_SKIPPED,
    process_directory,
)
from .validate import DESCRIPTIONS

_LABELS = {
    OUTCOME_OK: "ok     ",
    OUTCOME_NEEDS_REVIEW: "review ",
    OUTCOME_ERROR: "error  ",
    OUTCOME_SKIPPED: "skip   ",
}


def _build_extractor(kind: str) -> Extractor:
    if kind == "fixture":
        return FixtureExtractor()
    return GeminiExtractor()


def _cmd_ingest(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"ディレクトリが見つかりません: {directory}", file=sys.stderr)
        return 2

    try:
        extractor = _build_extractor(args.extractor)
        if args.min_interval > 0:
            # 回数制限のある API を、続けて呼ぶときに、制限に当たらないよう間隔をあける
            extractor = ThrottledExtractor(extractor, args.min_interval)
    except ExtractionError as error:
        print(f"設定エラー: {error}", file=sys.stderr)
        return 2

    conn = store.connect(args.db)
    try:
        outcomes = process_directory(directory, extractor, conn)
    except FatalExtractionError as error:
        # モデルが無い・キーが無効など。どのファイルでも同じなので、全体を止めて原因を伝える
        print(f"\n中断しました: {error}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.kind] = counts.get(outcome.kind, 0) + 1
        detail = ""
        if outcome.flags:
            detail = "  " + ", ".join(outcome.flags)
        elif outcome.message:
            detail = f"  {outcome.message}"
        print(f"[{_LABELS[outcome.kind]}] {outcome.path.name}{detail}")

    summary = "  ".join(f"{kind}={n}" for kind, n in sorted(counts.items()))
    print(f"\n完了: {len(outcomes)} 件  ({summary})")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"データベースが見つかりません: {args.db}", file=sys.stderr)
        return 2
    conn = store.connect(args.db)

    print("== 状態別の件数 ==")
    for status, n in sorted(store.count_by_status(conn).items()):
        print(f"  {status:<13} {n}")

    attention = store.list_needing_attention(conn)
    print("\n== 人の確認が必要なもの ==")
    if not attention:
        print("  なし")
    for row in attention:
        if row["status"] == store.STATUS_ERROR:
            print(f"  {row['source_name']}: 読み取り失敗 ({row['error_message']})")
            continue

        for flag in json.loads(row["flags"]):
            print(f"  {row['source_name']}: {flag} - {DESCRIPTIONS.get(flag, '')}")

    print("\n== 店ごとの合計（正常に取り込めたもの）==")
    stores = store.totals_by_store(conn)
    if not stores:
        print("  なし")
    for row in stores:
        print(f"  {row['store_name']:<16} {row['receipts']:>3} 件  {row['total']:>8,} 円")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows のコンソールでも、日本語が文字化けしないようにする
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    # 回数制限による待機など、途中の状況を表示する
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    parser = argparse.ArgumentParser(
        prog="receipt-normalizer",
        description="形式の異なるレシート画像を、共通のスキーマに正規化して SQLite に取り込みます。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="ディレクトリ内の画像を取り込む")
    ingest.add_argument("directory", help="画像のあるディレクトリ")
    ingest.add_argument("--db", default="receipts.db", help="保存先の SQLite ファイル")
    ingest.add_argument(
        "--extractor",
        choices=["gemini", "fixture"],
        default="gemini",
        help="読み取り方法。fixture は、API キー無しで試すための固定結果",
    )
    ingest.add_argument(
        "--min-interval",
        type=float,
        default=0,
        metavar="SECONDS",
        help="読み取りの間隔（秒）。回数制限のある API 用。無料枠の「1分に5回」なら 13 など",
    )
    ingest.set_defaults(func=_cmd_ingest)

    report = sub.add_parser("report", help="取り込み結果を確認する")
    report.add_argument("--db", default="receipts.db", help="SQLite ファイル")
    report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

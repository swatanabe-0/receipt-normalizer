"""コマンドライン：取り込み（ingest）、結果の確認（report）、精度の評価（evaluate）。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import store
from .evaluate import evaluate_directory, render_text, to_json
from .extract import (
    ExtractionError,
    Extractor,
    FatalExtractionError,
    FixtureExtractor,
    GeminiExtractor,
    RecordedExtractor,
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


def _build_extractor(kind: str, recorded_dir: str | None = None) -> Extractor:
    if kind == "fixture":
        return FixtureExtractor()
    if kind == "recorded":
        if not recorded_dir:
            raise ExtractionError(
                "--extractor recorded には、--recorded-dir（記録の置き場所）が必要です"
            )
        return RecordedExtractor(Path(recorded_dir))
    return GeminiExtractor()


def _make_extractor(args: argparse.Namespace) -> Extractor:
    extractor = _build_extractor(args.extractor, args.recorded_dir)
    if args.min_interval > 0:
        # 回数制限のある API を、続けて呼ぶときに、制限に当たらないよう間隔をあける
        extractor = ThrottledExtractor(extractor, args.min_interval)
    return extractor


def _cmd_ingest(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"ディレクトリが見つかりません: {directory}", file=sys.stderr)
        return 2

    try:
        extractor = _make_extractor(args)
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


def _cmd_evaluate(args: argparse.Namespace) -> int:
    images = Path(args.directory)
    if not images.is_dir():
        print(f"ディレクトリが見つかりません: {images}", file=sys.stderr)
        return 2

    truth_dir = Path(args.truth) if args.truth else images.parent / "ground_truth"
    if not truth_dir.is_dir():
        print(
            f"正解データのディレクトリが見つかりません: {truth_dir}（--truth で指定します）",
            file=sys.stderr,
        )
        return 2

    try:
        extractor = _make_extractor(args)
    except ExtractionError as error:
        print(f"設定エラー: {error}", file=sys.stderr)
        return 2

    try:
        summary = evaluate_directory(images, truth_dir, extractor)
    except FatalExtractionError as error:
        print(f"\n中断しました: {error}", file=sys.stderr)
        return 2

    print(render_text(summary))
    if args.json:
        Path(args.json).write_text(to_json(summary) + "\n", encoding="utf-8")
        print(f"\n（JSON を書き出しました: {args.json}）")

    if args.fail_on_silent_error and summary.silent_errors:
        print(
            f"\n失敗: 間違っているのに「正常」になったものが {len(summary.silent_errors)} 件あります",
            file=sys.stderr,
        )
        return 1
    return 0


def _add_extractor_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--extractor",
        choices=["gemini", "fixture", "recorded"],
        default="gemini",
        help=(
            "読み取り方法。gemini: 実際の API / fixture: 画像の隣の JSON（API キー無しで試す用）/ "
            "recorded: 過去に実際の API から得た出力の記録（--recorded-dir が必要）"
        ),
    )
    parser.add_argument("--recorded-dir", help="--extractor recorded のときの、記録の置き場所")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0,
        metavar="SECONDS",
        help="読み取りの間隔（秒）。回数制限のある API 用。無料枠の「1分に5回」なら 13 など",
    )


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
    _add_extractor_options(ingest)
    ingest.set_defaults(func=_cmd_ingest)

    report = sub.add_parser("report", help="取り込み結果を確認する")
    report.add_argument("--db", default="receipts.db", help="SQLite ファイル")
    report.set_defaults(func=_cmd_report)

    evaluate = sub.add_parser("evaluate", help="読み取り結果を正解データと比べて、精度を測る")
    evaluate.add_argument("directory", help="画像のあるディレクトリ")
    evaluate.add_argument(
        "--truth",
        help="正解データのディレクトリ（既定: 画像のディレクトリと同じ階層の ground_truth）",
    )
    evaluate.add_argument("--json", metavar="PATH", help="結果を JSON としても書き出す")
    evaluate.add_argument(
        "--fail-on-silent-error",
        action="store_true",
        help="間違っているのに「正常」になったものがあれば、終了コード 1 で終わる（CI 用）",
    )
    _add_extractor_options(evaluate)
    evaluate.set_defaults(func=_cmd_evaluate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

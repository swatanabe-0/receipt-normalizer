"""評価：読み取り結果を、正解データと比べて、どれだけ正しいかを測る。

「動いた」ではなく、「どれだけ正しいか」「間違えたとき、人の確認に回ったか」を、再現できる形で示す。

特に重視するのは、**黙って通った誤り**（結果が間違っているのに、「要確認」にならず、正常として扱われたもの）。
これが 0 であることが、取り込みの仕組みとしての、一番大事な条件になる。
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .extract import Extractor
from .normalize import normalize
from .pipeline import iter_receipt_files
from .schema import LineItem, Receipt
from .validate import validate


def _key(text: str | None) -> str:
    """店名・品名の比較用。全角・半角と、空白の違いは、同じものとして扱う。"""
    return unicodedata.normalize("NFKC", text or "").replace(" ", "").replace("　", "")


def load_truth(path: Path) -> Receipt:
    return Receipt.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass
class FieldScore:
    """1枚分の、項目ごとの正誤。"""

    store: bool
    date: bool
    total: bool
    tax: bool
    items_matched: int  # 正解の明細のうち、読み取れたもの
    items_expected: int  # 正解の明細の数
    items_predicted: int  # 読み取った明細の数

    @property
    def items_exact(self) -> bool:
        return self.items_matched == self.items_expected == self.items_predicted

    @property
    def correct(self) -> bool:
        return self.store and self.date and self.total and self.tax and self.items_exact

    def wrong_fields(self) -> list[str]:
        names = {"store": "店名", "date": "日付", "total": "合計", "tax": "消費税"}
        wrong = [label for attr, label in names.items() if not getattr(self, attr)]
        if not self.items_exact:
            wrong.append("明細")
        return wrong


def _item_key(item: LineItem) -> tuple[str, int, int]:
    return (_key(item.name), item.quantity, item.amount)


def compare(truth: Receipt, got: Receipt) -> FieldScore:
    expected = Counter(_item_key(i) for i in truth.items)
    predicted = Counter(_item_key(i) for i in got.items)
    matched = sum((expected & predicted).values())
    return FieldScore(
        store=_key(truth.store_name) == _key(got.store_name),
        date=truth.purchased_on == got.purchased_on,
        total=truth.total == got.total,
        tax=truth.tax == got.tax,
        items_matched=matched,
        items_expected=sum(expected.values()),
        items_predicted=sum(predicted.values()),
    )


@dataclass
class CaseResult:
    name: str
    error: str | None = None  # 読み取れなかった場合の理由
    score: FieldScore | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def read(self) -> bool:
        return self.score is not None

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def correct(self) -> bool:
        return bool(self.score and self.score.correct)

    @property
    def silent_error(self) -> bool:
        """結果が間違っているのに、「要確認」にならなかった（もっとも危険なケース）。"""
        return self.read and not self.correct and not self.flagged


@dataclass
class Summary:
    cases: list[CaseResult]
    skipped_without_truth: list[str]

    def _read(self) -> list[CaseResult]:
        return [c for c in self.cases if c.read]

    def field_accuracy(self) -> dict[str, tuple[int, int]]:
        read = self._read()
        scores = [c.score for c in read if c.score]
        return {
            "店名": (sum(s.store for s in scores), len(scores)),
            "日付": (sum(s.date for s in scores), len(scores)),
            "合計": (sum(s.total for s in scores), len(scores)),
            "消費税": (sum(s.tax for s in scores), len(scores)),
            "明細（再現率）": (
                sum(s.items_matched for s in scores),
                sum(s.items_expected for s in scores),
            ),
            "明細（適合率）": (
                sum(s.items_matched for s in scores),
                sum(s.items_predicted for s in scores),
            ),
            "完全一致": (sum(s.correct for s in scores), len(scores)),
        }

    @property
    def wrong(self) -> list[CaseResult]:
        return [c for c in self._read() if not c.correct]

    @property
    def caught(self) -> list[CaseResult]:
        return [c for c in self.wrong if c.flagged]

    @property
    def silent_errors(self) -> list[CaseResult]:
        return [c for c in self.cases if c.silent_error]

    @property
    def unread(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.read]

    def to_dict(self) -> dict:
        return {
            "field_accuracy": {
                k: {"correct": a, "total": b} for k, (a, b) in self.field_accuracy().items()
            },
            "read": len(self._read()),
            "unread": [{"name": c.name, "reason": c.error} for c in self.unread],
            "wrong": len(self.wrong),
            "wrong_but_flagged": len(self.caught),
            "silent_errors": [c.name for c in self.silent_errors],
            "cases": [
                {
                    "name": c.name,
                    "read": c.read,
                    "correct": c.correct,
                    "flags": c.flags,
                    "score": asdict(c.score) if c.score else None,
                    "error": c.error,
                }
                for c in self.cases
            ],
        }


def evaluate_directory(
    images: Path,
    truth_dir: Path,
    extractor: Extractor,
    *,
    today: date | None = None,
) -> Summary:
    """画像を読み取り、正規化・検証したうえで、正解データと比べる（データベースは使わない）。"""
    cases: list[CaseResult] = []
    skipped: list[str] = []

    for path in iter_receipt_files(images):
        truth_path = truth_dir / f"{path.stem}.json"
        if not truth_path.exists():
            skipped.append(path.name)  # 正解データがないもの（重複ファイルなど）
            continue

        truth = load_truth(truth_path)
        try:
            raw = extractor.extract(path)
        except Exception as error:
            cases.append(CaseResult(path.name, error=str(error).splitlines()[0]))
            continue

        receipt, normalize_flags = normalize(raw)
        flags = normalize_flags + validate(receipt, today=today)
        cases.append(CaseResult(path.name, score=compare(truth, receipt), flags=flags))

    return Summary(cases=cases, skipped_without_truth=skipped)


def render_text(summary: Summary) -> str:
    """人が読むための、評価レポート。"""
    lines: list[str] = []
    read = summary._read()
    lines.append(f"== 項目別の正解率（読み取れた {len(read)} 件）==")
    for label, (ok, total) in summary.field_accuracy().items():
        rate = f"{ok / total:>4.0%}" if total else "  - "
        lines.append(f"  {label:<14} {ok:>3} / {total:<3} {rate}")

    lines.append("")
    lines.append("== 誤りの検知（結果が間違っていたもの）==")
    lines.append(f"  誤りがあった               : {len(summary.wrong)} 件")
    lines.append(f"    うち「要確認」になった   : {len(summary.caught)} 件")
    lines.append(f"    うち黙って「正常」だった : {len(summary.silent_errors)} 件  ← 0 が条件")

    lines.append("")
    lines.append("== 個別の結果 ==")
    for case in summary.cases:
        if not case.read:
            lines.append(f"  {case.name:<28} 読み取れず  ({case.error})")
            continue
        flags = f"  要確認: {', '.join(case.flags)}" if case.flagged else ""
        if case.correct:
            verdict = "一致"
        else:
            verdict = (
                f"不一致（{'・'.join(case.score.wrong_fields())}）" if case.score else "不一致"
            )
        if case.silent_error:
            verdict += "  ★黙って通った"
        lines.append(f"  {case.name:<28} {verdict}{flags}")

    if summary.skipped_without_truth:
        lines.append("")
        lines.append(
            "  （正解データがないため、対象外: " + ", ".join(summary.skipped_without_truth) + "）"
        )
    return "\n".join(lines)


def to_json(summary: Summary) -> str:
    return json.dumps(summary.to_dict(), ensure_ascii=False, indent=2)

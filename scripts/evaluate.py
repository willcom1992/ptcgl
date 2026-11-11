"""スクリーンショットで認識・照合の精度を計測する。

    python scripts/evaluate.py assets/samples [--out docs/validation-results.md]

assets/samples/labels.csv の各行について 検出 → OCR → 英語カード特定 → 日本語照合 を実行し、
成功率と所要時間を Markdown の表として出力する。失敗例は OCR の生テキストも残す。

labels.csv の列:
    file, en_set, en_number, jp_set, jp_number, resolution, scale, monitors, mode, note
    （jp_set/jp_number は空でもよい。その場合は英語カードの特定だけを採点する）
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ptcgl.cli import build_services, configure_console_encoding  # noqa: E402
from ptcgl.config import load_config  # noqa: E402
from ptcgl.data.limitless import CardRef  # noqa: E402
from ptcgl.data.mapping import MappingStore  # noqa: E402
from ptcgl.imageio import read_image  # noqa: E402
from ptcgl.recognition.detector import CardBox  # noqa: E402


@dataclass
class Row:
    file: str
    en: CardRef | None
    jp: CardRef | None
    env: str
    note: str


def read_labels(path: Path) -> list[Row]:
    rows: list[Row] = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            en = (
                CardRef(
                    "en", r["en_set"].strip().upper(), r["en_number"].strip().lstrip("0") or "0"
                )
                if r.get("en_set") and r.get("en_number")
                else None
            )
            jp = (
                CardRef("jp", r["jp_set"].strip(), r["jp_number"].strip().lstrip("0") or "0")
                if r.get("jp_set") and r.get("jp_number")
                else None
            )
            env = " / ".join(
                v
                for v in (r.get("resolution"), r.get("scale"), r.get("monitors"), r.get("mode"))
                if v
            )
            rows.append(Row(r["file"], en, jp, env, r.get("note", "")))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--no-detect", action="store_true", help="画像全体をカードとして扱う")
    args = parser.parse_args(argv)

    configure_console_encoding()
    labels = read_labels(args.samples / "labels.csv")
    cfg = load_config()
    pipeline, http = build_services(cfg)
    # 6. 評価はアルゴリズムの計測なので、ユーザーの保存済み対応表は使わず汚さない
    tmp_db = Path(tempfile.mkdtemp(prefix="ptcgl_eval_")) / "mapping.sqlite3"
    pipeline.repo._mapping = MappingStore(tmp_db)  # noqa: SLF001
    full_frame = CardBox(0.0, 0.0, 1.0, 1.0) if args.no_detect else None

    lines = [
        "| file | env | detect | OCR name/number | EN 特定 | JP 照合 | 時間(ms) | 備考 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    en_ok = jp_ok = detect_ok = 0
    times: list[float] = []
    failures: list[str] = []
    try:
        for row in labels:
            frame = read_image(args.samples / row.file)
            if frame is None:
                lines.append(f"| {row.file} | {row.env} | - | 画像なし | - | - | - | {row.note} |")
                continue
            result = pipeline.identify_frame(frame, box=full_frame)
            total_ms = sum(result.timings_ms.values())
            times.append(total_ms)
            detected = result.card_box is not None
            detect_ok += detected
            rec = result.recognized
            ocr_text = f"{rec.name_en} {rec.number}/{rec.total}" if rec else "-"
            # 番号が隠れる表示では検索で同一カードの別印刷が返るため、Int. Prints に期待の印刷が
            # 含まれていれば同一カードとして正解扱いにする
            same_card = (
                row.en is not None
                and result.match is not None
                and row.en in {p.ref for p in result.match.en.int_prints}
            )
            en_hit = row.en is not None and (result.en_ref == row.en or same_card)
            jp_hit = (
                row.jp is not None
                and result.match is not None
                and result.match.best is not None
                and result.match.best.ref == row.jp
            )
            en_ok += en_hit
            jp_ok += jp_hit
            same_note = " 同一カード" if same_card and result.en_ref != row.en else ""
            en_cell = f"{'OK' if en_hit else 'NG'} ({result.en_ref}{same_note})"
            best_ref = result.match.best.ref if result.match and result.match.best else None
            jp_cell = f"{'OK' if jp_hit else 'NG'} ({best_ref})" if row.jp else "-"
            lines.append(
                f"| {row.file} | {row.env} | {'OK' if detected else 'NG'} | {ocr_text} "
                f"| {en_cell} | {jp_cell} | {total_ms:.0f} | {row.note} {result.error or ''} |"
            )
            if not en_hit:
                failures.append(
                    f"### {row.file}\n\n期待: {row.en}\n\nOCR:\n```\n"
                    + "\n".join(f"{ln.confidence:.2f} {ln.text}" for ln in result.ocr_lines)
                    + "\n```\n"
                )
    finally:
        http.close()

    n = len(labels) or 1
    summary = [
        f"- サンプル数: {len(labels)}",
        f"- 検出率: {detect_ok}/{len(labels)} ({detect_ok / n:.0%})",
        f"- 英語カード特定率: {en_ok}/{len(labels)} ({en_ok / n:.0%})",
        f"- 日本語照合率（ラベルあり分）: {jp_ok}/{sum(1 for r in labels if r.jp)}",
        f"- 所要時間 中央値: {statistics.median(times):.0f} ms" if times else "- 所要時間: -",
    ]
    report = "\n".join(["## 評価結果", "", *summary, "", *lines, "", "## 失敗例", "", *failures])
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("a", encoding="utf-8") as f:
            f.write("\n" + report + "\n")
        print(f"-> {args.out}")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

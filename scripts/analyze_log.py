"""常駐モードのログ（ptcgl.log）から認識の所要時間を経路別に集計する。

    python scripts/analyze_log.py                       # user_data_dir/ptcgl.log の全体
    python scripts/analyze_log.py --since "2026-11-12 10:00"
    python scripts/analyze_log.py path/to/ptcgl.log

出力: 成功・失敗・中断の件数と所要時間、経路（番号行 / HP バッジ / 拡大読み直し / 3 段目 / 通常）
別の中央値と 90 パーセンタイル、失敗理由の上位、失敗の連なり（演出中に続く失敗）の長さ。
「表示が遅い」の原因を切り分けるときに使う（docs/architecture.md「常駐モードの認識フロー」参照）。
"""

from __future__ import annotations

import argparse
import ast
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ptcgl.cli import configure_console_encoding  # noqa: E402
from ptcgl.config import data_dir  # noqa: E402

_RESULT_RE = re.compile(
    r"^(?P<ts>\S+ \S+) INFO ptcgl\.ui\.worker: 認識 (?P<ms>\d+)ms ok=(?P<ok>\w+) en=(?P<en>.*?) "
    r"region=(?P<region>\S+) (?:stable=(?P<stable>\w+) )?(?:bg=(?P<bg>\d+) )?"
    r"timings=(?P<timings>\{.*?\}) error=(?P<error>.*)$"
)
_ABORT_RE = re.compile(
    r"^(?P<ts>\S+ \S+) INFO ptcgl\.ui\.worker: 認識を中断 (?P<ms>\d+)ms.*?"
    r"(?:bg=(?P<bg>\d+) )?timings=(?P<timings>\{.*?\})"
    r"(?: .*diff=(?P<diff>[\d.]+) text_diff=(?P<text_diff>\S+))?$"
)
_ROI_RE = re.compile(
    r"HP バッジ領域の拡大読み直しで番号なし: (?P<n>\d+) 件（省略 (?P<skipped>\d+), "
    r"記憶との差分 (?P<diffs>\[.*?\]), (?P<ms>\d+)ms）"
)
_ROI_OK_RE = re.compile(
    r"HP バッジ領域の拡大読み直しで番号を発見: .*（記憶との差分 (?P<diff>None|\d+)）"
)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S,%f"


def _diff_bucket(d: float | None) -> str:
    if d is None:
        return "記憶なし"
    if d < 10:
        return "<10"
    if d < 20:
        return "10-20"
    if d < 40:
        return "20-40"
    return ">=40"


def classify(timings: dict[str, float]) -> str:
    """成功した認識の timings のキーから、どの段階で決着したかを推定する。"""
    if "ocr_pass1" in timings:
        return "normal"  # 通常パイプライン（手動領域の設定時、または旧版のログ）
    if "frame_detect" in timings:
        return "fast:hp_roi"  # HP バッジ領域（またはアンカーのカード領域）の拡大読み直し
    if "hp_badge" in timings and "hp_roi" not in timings:
        return "fast:hp_badge"  # HP バッジアンカー + 検索
    if "ocr_rest" in timings:
        return "fast:tier3"
    return "fast:number"


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * ratio), len(ordered) - 1)]


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("log", nargs="?", type=Path, default=data_dir() / "ptcgl.log")
    parser.add_argument("--since", default="", help='この時刻以降だけ集計（"2026-11-12 10:00"）')
    args = parser.parse_args(argv)
    if not args.log.exists():
        print(f"ログがありません: {args.log}")
        return 1

    results: list[dict] = []
    aborts: list[float] = []
    abort_diffs: list[tuple[float, float | None]] = []  # 中断時の (画面全体, 文字の箱の中) の差分
    background_changes = 0  # 画面は変わったが文字の箱の中は不変で続行した回数
    roi_rois = roi_skipped = 0  # HP バッジ領域の拡大読み直し（失敗）の件数と、記憶で省いた件数
    roi_ms: list[float] = []
    roi_diffs: Counter[str] = Counter()  # 記憶との差分の分布
    roi_ok_diffs: Counter[str] = Counter()  # 読めた領域の、記憶との差分の分布
    for line in args.log.read_text(encoding="utf-8", errors="replace").splitlines():
        if args.since and line[: len(args.since)] < args.since:
            continue
        m = _RESULT_RE.match(line)
        if m:
            results.append(
                {
                    "ts": datetime.strptime(m["ts"], _TS_FORMAT),
                    "ms": int(m["ms"]),
                    "ok": m["ok"] == "True",
                    "stable": m["stable"],
                    "timings": ast.literal_eval(m["timings"]),
                    "error": m["error"],
                }
            )
            background_changes += int(m["bg"] or 0)
            continue
        m = _ABORT_RE.match(line)
        if m:
            aborts.append(int(m["ms"]))
            background_changes += int(m["bg"] or 0)
            if m["diff"]:
                text_diff = None if m["text_diff"] == "-" else float(m["text_diff"])
                abort_diffs.append((float(m["diff"]), text_diff))
            continue
        m = _ROI_RE.search(line)
        if m:
            roi_rois += int(m["n"])
            roi_skipped += int(m["skipped"])
            roi_ms.append(int(m["ms"]))
            for d in ast.literal_eval(m["diffs"]):
                roi_diffs[_diff_bucket(d)] += 1
            continue
        m = _ROI_OK_RE.search(line)
        if m:
            roi_ok_diffs[_diff_bucket(None if m["diff"] == "None" else int(m["diff"]))] += 1

    print(f"ログ: {args.log}")
    print(f"認識: {len(results)} 回, 中断: {len(aborts)} 回")
    if not results:
        return 0
    ok = [r["ms"] for r in results if r["ok"]]
    ng = [r["ms"] for r in results if not r["ok"]]
    for label, values in (("成功", ok), ("失敗", ng), ("中断", aborts)):
        if values:
            median, p90 = statistics.median(values), percentile(values, 0.9)
            print(
                f"  {label}: {len(values):4d} 回  合計 {sum(values) / 1000:6.0f} 秒"
                f"  中央値 {median:5.0f} ms  p90 {p90:5.0f} ms"
            )

    if background_changes or abort_diffs:
        print(f"\n文字の箱の外だけが変わって続行した回数: {background_changes}")
    if abort_diffs:
        whole = [d for d, _ in abort_diffs]
        text = [t for _, t in abort_diffs if t is not None]
        line = f"中断時の差分: 画面全体 中央値 {statistics.median(whole):.1f} 最小 {min(whole):.1f}"
        if text:
            line += f" / 文字の箱の中 中央値 {statistics.median(text):.1f} 最小 {min(text):.1f}"
        print(line)

    if roi_rois:
        print(
            f"HP バッジ領域の読み直し（失敗）: {roi_rois} 件 / {len(roi_ms)} 回,"
            f" 記憶で省略 {roi_skipped}, 1 回の中央値 {statistics.median(roi_ms):.0f} ms,"
            f" 合計 {sum(roi_ms) / 1000:.0f} 秒"
        )
        print("  記憶との差分:", dict(roi_diffs.most_common()))
    if roi_ok_diffs:
        # 成功側の差分が省略のしきい値（FAILED_ROI_MAX_DIFF）より大きければ、しきい値は安全
        print(f"HP バッジ領域の読み直し（成功）: {sum(roi_ok_diffs.values())} 回")
        print("  記憶との差分:", dict(roi_ok_diffs.most_common()))

    print("\n経路別（成功した認識）:")
    by_path: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["ok"]:
            by_path[classify(r["timings"])].append(r["ms"])
    for path_name in sorted(by_path):
        values = by_path[path_name]
        print(
            f"  {path_name:14s} {len(values):4d} 回  中央値 {statistics.median(values):5.0f} ms"
            f"  p90 {percentile(values, 0.9):5.0f} ms"
        )

    print("\n成功した認識の段階別の中央値 (ms):")
    keys = Counter(k for r in results if r["ok"] for k in r["timings"])
    for key, count in keys.most_common():
        values = [r["timings"][key] for r in results if r["ok"] and key in r["timings"]]
        print(f"  {key:18s} {statistics.median(values):6.0f}  (n={count})")

    stable = Counter((r["stable"], r["ok"]) for r in results if r["stable"] is not None)
    if stable:
        print("\n静止/非静止 × 成否:")
        for (flag, is_ok), count in sorted(stable.items()):
            print(f"  stable={flag} ok={is_ok}: {count}")

    print("\n失敗の理由（上位）:")
    for error, count in Counter(r["error"] for r in results if not r["ok"]).most_common(8):
        print(f"  {count:4d} {error}")

    # 失敗の連なり（6 秒以内に続く失敗）。演出中にワーカーが塞がっていた時間の目安
    bursts: list[list[dict]] = []
    current: list[dict] = []
    previous: datetime | None = None
    for r in results:
        if not r["ok"] and previous is not None and (r["ts"] - previous).total_seconds() < 6:
            current.append(r)
        else:
            if len(current) >= 2:
                bursts.append(current)
            current = [r] if not r["ok"] else []
        previous = r["ts"]
    if len(current) >= 2:
        bursts.append(current)
    if bursts:
        busy = sum(r["ms"] for b in bursts for r in b) / 1000
        median_len = statistics.median(len(b) for b in bursts)
        print(
            f"\n失敗の連なり: {len(bursts)} 本, 長さ中央値 {median_len:.0f},"
            f" 最長 {max(len(b) for b in bursts)}, 連なりの合計 {busy:.0f} 秒"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

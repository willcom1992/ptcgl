"""カード領域の検出方式を比較する（Phase 1 で採用方式を決めるための検証）。

    python scripts/detect_spike.py assets/samples --out .cache/detect

各スクリーンショットに対して検出器ごとの矩形を描画した画像を保存し、検出率と時間を表にする。
labels.csv に card_region 列（rx,ry,rw,rh の比率）があれば IoU も計算する。
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ptcgl.imageio import read_image, write_image  # noqa: E402
from ptcgl.recognition.detector import CardBox, ContourCardDetector  # noqa: E402


def iou(a: CardBox, b: CardBox) -> float:
    ax1, ay1, ax2, ay2 = a.rx, a.ry, a.rx + a.rw, a.ry + a.rh
    bx1, by1, bx2, by2 = b.rx, b.ry, b.rx + b.rw, b.ry + b.rh
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = a.rw * a.rh + b.rw * b.rh - inter
    return inter / union if union else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--out", type=Path, default=Path(".cache/detect"))
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    detectors = {
        "contour": ContourCardDetector(),
        "contour-loose": ContourCardDetector(min_height_ratio=0.15, aspect_tolerance=0.2),
    }

    expected: dict[str, CardBox] = {}
    labels = args.samples / "labels.csv"
    if labels.exists():
        with labels.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("card_region"):
                    expected[r["file"]] = CardBox(*[float(v) for v in r["card_region"].split(",")])

    print("| file | detector | box | conf | IoU | ms |")
    print("|---|---|---|---|---|---|")
    for img_path in sorted(args.samples.glob("*.png")):
        frame = read_image(img_path)
        if frame is None:
            continue
        h, w = frame.shape[:2]
        for name, det in detectors.items():
            t0 = time.perf_counter()
            box = det.detect(frame)
            ms = (time.perf_counter() - t0) * 1000
            vis = frame.copy()
            if box is not None:
                x, y, bw, bh = box.to_pixels(w, h)
                cv2.rectangle(vis, (x, y), (x + bw, y + bh), (0, 255, 0), max(2, h // 400))
            exp = expected.get(img_path.name)
            if exp is not None:
                x, y, bw, bh = exp.to_pixels(w, h)
                cv2.rectangle(vis, (x, y), (x + bw, y + bh), (0, 0, 255), max(1, h // 600))
            write_image(args.out / f"{img_path.stem}_{name}.png", vis)
            box_txt = f"({box.rx:.2f},{box.ry:.2f},{box.rw:.2f},{box.rh:.2f})" if box else "-"
            iou_txt = f"{iou(box, exp):.2f}" if (box and exp) else "-"
            conf = box.confidence if box else 0.0
            print(f"| {img_path.name} | {name} | {box_txt} | {conf:.2f} | {iou_txt} | {ms:.0f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

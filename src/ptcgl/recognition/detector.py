"""画面フレームから拡大表示されたカードの矩形を検出する。

OCR とは独立したモジュール。検出結果はフレームに対する比率座標で返すため、
解像度・DPI・ウィンドウ位置に依存しない。方式は Protocol で差し替え可能。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

# 実カードの縦横比（63mm x 88mm）
CARD_ASPECT = 63 / 88


@dataclass(frozen=True)
class CardBox:
    """フレームに対する比率座標 (0..1) の矩形。"""

    rx: float
    ry: float
    rw: float
    rh: float
    confidence: float = 1.0

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        x = int(round(self.rx * width))
        y = int(round(self.ry * height))
        w = int(round(self.rw * width))
        h = int(round(self.rh * height))
        return x, y, max(w, 1), max(h, 1)

    @classmethod
    def from_pixels(
        cls, x: int, y: int, w: int, h: int, width: int, height: int, confidence: float = 1.0
    ) -> CardBox:
        return cls(x / width, y / height, w / width, h / height, confidence)


def to_frame_box(box: CardBox | None, region: CardBox) -> CardBox | None:
    """領域（フレーム比率）内の比率座標を、フレーム全体の比率座標に戻す。"""
    if box is None:
        return None
    return CardBox(
        region.rx + box.rx * region.rw,
        region.ry + box.ry * region.rh,
        box.rw * region.rw,
        box.rh * region.rh,
        box.confidence,
    )


class CardDetector(Protocol):
    def detect(self, frame_bgr: np.ndarray) -> CardBox | None: ...


def crop(frame_bgr: np.ndarray, box: CardBox) -> np.ndarray:
    h, w = frame_bgr.shape[:2]
    x, y, bw, bh = box.to_pixels(w, h)
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + bw, w), min(y + bh, h)
    return frame_bgr[y0:y1, x0:x1]


class ManualRegionDetector:
    """設定で指定された固定の比率領域を返す。"""

    def __init__(self, box: CardBox) -> None:
        self.box = box

    def detect(self, frame_bgr: np.ndarray) -> CardBox | None:
        return self.box


class ContourCardDetector:
    """輪郭検出でカード形状（縦長・縦横比 63:88）の最大矩形を探す。

    - Canny → 膨張 → 輪郭 → 4 頂点近似 → 縦横比と最小サイズで絞り込み
    - フレーム高さに対する比率でサイズ判定するため解像度に依存しない
    """

    def __init__(
        self,
        min_height_ratio: float = 0.25,
        aspect_tolerance: float = 0.12,
        canny_thresholds: tuple[int, int] = (50, 150),
    ) -> None:
        self.min_height_ratio = min_height_ratio
        self.aspect_tolerance = aspect_tolerance
        self.canny_thresholds = canny_thresholds

    def detect(self, frame_bgr: np.ndarray) -> CardBox | None:
        h, w = frame_bgr.shape[:2]
        if h == 0 or w == 0:
            return None
        # 処理コスト削減のため長辺 1000px 程度に縮小してから検出する
        scale = min(1.0, 1000 / max(h, w))
        small = cv2.resize(frame_bgr, None, fx=scale, fy=scale) if scale < 1.0 else frame_bgr
        sh, sw = small.shape[:2]

        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, *self.canny_thresholds)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        best: tuple[float, tuple[int, int, int, int]] | None = None
        min_h = sh * self.min_height_ratio
        for cnt in contours:
            x, y, bw, bh = cv2.boundingRect(cnt)
            if bh < min_h or bw < 10:
                continue
            aspect = bw / bh
            if abs(aspect - CARD_ASPECT) > self.aspect_tolerance:
                continue
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.03 * peri, True)
            if len(approx) < 4 or len(approx) > 8:
                continue
            area = bw * bh
            fill = cv2.contourArea(cnt) / area if area else 0
            if fill < 0.7:
                continue
            score = area * fill
            if best is None or score > best[0]:
                best = (score, (x, y, bw, bh))

        if best is None:
            return None
        _, (x, y, bw, bh) = best
        aspect_err = abs(bw / bh - CARD_ASPECT) / self.aspect_tolerance
        confidence = max(0.0, 1.0 - aspect_err) * 0.5 + 0.5
        return CardBox.from_pixels(x, y, bw, bh, sw, sh, confidence)

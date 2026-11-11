"""RapidOCR によるテキスト抽出。

速度について: onnxruntime の既定（全論理コア）はコア数の多い CPU で逆に遅くなる
（24 論理コアの開発機で 2.5 秒 → 4〜8 スレッドで 0.5 秒）。ゲームが動いている負荷時は
4 スレッドが最も安定したため、自動設定の上限は 4 にする。
角度分類（180 度回転の判定）は画面の文字には不要なので使わない。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

MAX_AUTO_THREADS = 4


def default_ocr_threads() -> int:
    """onnxruntime に渡すスレッド数。論理コアの半分、2〜4 の範囲。"""
    cpus = os.cpu_count() or 4
    return max(2, min(MAX_AUTO_THREADS, cpus // 2))


Box = tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    box: Box


def box_rect(box: Box) -> tuple[float, float, float, float]:
    """4 点の箱を軸平行の (x0, y0, x1, y1) にする。"""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def crop_box(image: np.ndarray, box: Box, pad: int = 2) -> np.ndarray:
    """検出した 4 点の箱を切り出す。傾いた箱は射影変換で水平に起こす（RapidOCR と同じ方法）。"""
    pts = np.array(box, dtype=np.float32)
    if len(pts) != 4:
        h, w = image.shape[:2]
        x0, y0, x1, y1 = box_rect(box)
        xa, ya = max(int(x0) - pad, 0), max(int(y0) - pad, 0)
        xb, yb = min(int(x1) + pad + 1, w), min(int(y1) + pad + 1, h)
        return image[ya:yb, xa:xb]
    cw = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3]))) + 2 * pad
    ch = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2]))) + 2 * pad
    cw, ch = max(cw, 1), max(ch, 1)
    dst = np.array(
        [[pad, pad], [cw - pad, pad], [cw - pad, ch - pad], [pad, ch - pad]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(
        image, matrix, (cw, ch), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC
    )


class OcrEngine:
    """RapidOCR のラッパー。初回呼び出し時にモデルを遅延ロードする。"""

    def __init__(self, threads: int | None = None, use_cls: bool = False) -> None:
        self._engine: Any = None
        self.threads = threads if threads and threads > 0 else default_ocr_threads()
        self.use_cls = use_cls

    def _get_engine(self) -> Any:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR(
                intra_op_num_threads=self.threads,
                inter_op_num_threads=1,
                use_cls=self.use_cls,
            )
        return self._engine

    def read(self, image: np.ndarray) -> list[OcrLine]:
        result, _ = self._get_engine()(image, use_cls=self.use_cls)
        if not result:
            return []
        return [
            OcrLine(text=text, confidence=float(conf), box=tuple(tuple(p) for p in box))
            for box, text, conf in result
        ]

    def detect(self, image: np.ndarray, max_side: int | None = None) -> list[Box]:
        """文字領域の検出だけを行う（認識はしない）。高速パスで読む行を選ぶために使う。

        `max_side` を指定すると長辺がその長さになるよう縮小して検出し、座標は元画像に戻す。
        検出の計算量は画素数に比例するため、縮小で 2〜3 倍速くなる。
        """
        h, w = image.shape[:2]
        scale = 1.0
        if max_side and max(h, w) > max_side:
            scale = max_side / max(h, w)
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        result, _ = self._get_engine()(image, use_det=True, use_cls=False, use_rec=False)
        if not result:
            return []
        return [tuple((float(p[0]) / scale, float(p[1]) / scale) for p in box) for box in result]

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        """切り出した行画像をまとめて認識する。detect() で選んだ領域に対して使う。"""
        if not crops:
            return []
        rec_res, _ = self._get_engine().text_rec(crops)
        return [(str(text), float(conf)) for text, conf in rec_res]

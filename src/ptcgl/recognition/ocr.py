"""RapidOCR によるテキスト抽出。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    box: tuple[tuple[float, float], ...]


class OcrEngine:
    """RapidOCR のラッパー。初回呼び出し時にモデルを遅延ロードする。"""

    def __init__(self) -> None:
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def read(self, image: np.ndarray) -> list[OcrLine]:
        result, _ = self._get_engine()(image)
        if not result:
            return []
        return [
            OcrLine(text=text, confidence=float(conf), box=tuple(tuple(p) for p in box))
            for box, text, conf in result
        ]

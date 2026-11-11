"""画像ファイルの読み書き。

OpenCV の `cv2.imread` / `cv2.imwrite` は Windows で非 ASCII のパス（日本語ユーザー名や
「スクリーンショット 2026-…png」）を扱えないため、バイト列経由で読み書きする。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def read_image(path: str | Path) -> np.ndarray | None:
    """BGR 画像として読み込む。読めなければ None。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def write_image(path: str | Path, image: np.ndarray) -> bool:
    path = Path(path)
    try:
        ok, buf = cv2.imencode(path.suffix or ".png", image)
    except cv2.error:
        return False
    if not ok:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(path))
    return True

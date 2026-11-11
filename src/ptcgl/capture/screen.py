"""mss による画面キャプチャ。"""

from __future__ import annotations

import numpy as np


class ScreenCapture:
    """指定した矩形領域をキャプチャして BGR の numpy 配列を返す。"""

    def __init__(self, region: tuple[int, int, int, int] | None = None) -> None:
        self.region = region

    def grab(self) -> np.ndarray:
        import mss

        with mss.mss() as sct:
            if self.region is None:
                monitor = sct.monitors[1]  # プライマリモニタ
            else:
                left, top, width, height = self.region
                monitor = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(monitor)
            frame = np.asarray(shot)  # BGRA
            return frame[:, :, :3].copy()  # BGR

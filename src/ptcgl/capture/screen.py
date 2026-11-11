"""mss による画面キャプチャ（物理ピクセル）。"""

from __future__ import annotations

import hashlib
import json

import numpy as np

Rect = tuple[int, int, int, int]


class ScreenCapture:
    """指定した矩形領域をキャプチャして BGR の numpy 配列を返す。"""

    def __init__(self, region: Rect | None = None) -> None:
        self.region = region

    def grab(self, region: Rect | None = None) -> np.ndarray:
        import mss

        from ptcgl.capture.window import ensure_dpi_aware

        ensure_dpi_aware()  # mss が先に V1 の DPI 対応を宣言してしまうのを防ぐ
        region = region or self.region
        with mss.mss() as sct:
            if region is None:
                monitor = sct.monitors[1]  # プライマリモニタ
            else:
                left, top, width, height = region
                monitor = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(monitor)
            frame = np.asarray(shot)  # BGRA
            return frame[:, :, :3].copy()  # BGR


def list_monitors() -> list[dict[str, int]]:
    """mss のモニター一覧（index 0 は仮想スクリーン全体）を返す。"""
    import mss

    from ptcgl.capture.window import ensure_dpi_aware

    ensure_dpi_aware()

    with mss.mss() as sct:
        return [dict(m) for m in sct.monitors]


def monitor_fingerprint() -> str:
    """モニター構成（位置・解像度）のハッシュ。構成変更の検知に使う。"""
    monitors = list_monitors()[1:]
    payload = json.dumps(sorted(monitors, key=lambda m: (m["left"], m["top"])), sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

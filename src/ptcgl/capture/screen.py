"""mss による画面キャプチャ（物理ピクセル）。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

Rect = tuple[int, int, int, int]


class ScreenCapture:
    """指定した矩形領域をキャプチャして BGR の numpy 配列を返す。"""

    def __init__(self, region: Rect | None = None) -> None:
        self.region = region
        self._sct: Any = None  # 常駐モードでは毎回作り直さず使い回す（作成コストが大きい）

    def _session(self) -> Any:
        if self._sct is None:
            import mss

            from ptcgl.capture.window import ensure_dpi_aware

            ensure_dpi_aware()  # mss が先に V1 の DPI 対応を宣言してしまうのを防ぐ
            self._sct = mss.mss()
        return self._sct

    def close(self) -> None:
        if self._sct is not None:
            try:
                self._sct.close()
            finally:
                self._sct = None

    def grab(self, region: Rect | None = None) -> np.ndarray:
        region = region or self.region
        sct = self._session()
        try:
            if region is None:
                monitor = sct.monitors[1]  # プライマリモニタ
            else:
                left, top, width, height = region
                monitor = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(monitor)
        except Exception:
            # 画面ロックやモニター構成変更で DC が無効になることがあるため、次回は作り直す
            self.close()
            raise
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

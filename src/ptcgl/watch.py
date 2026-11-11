"""常駐モードの監視ロジック（Qt に依存しない）。

一定間隔で PTCGL ウィンドウをキャプチャし、画面が変化したら即座に認識を走らせる。
毎フレーム OCR すると CPU を占有するため、縮小グレースケールの差分で変化を判定する。

遅延を抑えるための方針:
- 変化を検知したら静止を待たずに認識する（カードの拡大表示は 1 フレームで出終わることが多い）
- 直前の基準フレームとの差分から「変化した領域」の外接矩形を求め、まずそこだけを OCR する。
  全画面 OCR（0.5〜1.5 秒）を省き、小さな領域の OCR（0.2〜0.4 秒）だけで済ませる。
  領域で見つからなければ全画面で再試行する
- 静止していないフレームで失敗したときは基準フレームを更新しない（静止後に再度変化として拾う）。
  失敗が続いたら試行間隔を 1, 2, 4 … 回（上限 MAX_BACKOFF_TICKS）と倍々に延ばし、
  演出が続く盤面で OCR が走り続けないようにする
- 静止したフレームで認識したら成否にかかわらず基準にし、画面が変わるまで再試行しない
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from ptcgl.capture.window import WindowInfo
from ptcgl.pipeline import IdentifyResult
from ptcgl.recognition.detector import CardBox, crop

log = logging.getLogger(__name__)

_THUMB_WIDTH = 320
_REGION_PIXEL_THRESHOLD = 24  # 変化した画素とみなす輝度差（0-255）
_REGION_PAD_RATIO = 0.04  # 変化領域の外側に足す余白（フレーム比）
MAX_REGION_AREA = 0.6  # これより広く変化していれば全画面扱い
MAX_BACKOFF_TICKS = 40
WINDOW_SEARCH_EVERY_TICKS = 8  # PTCGL が見つからない間は 8 ティック（既定 2 秒）に 1 回だけ探す


def _thumbnail(frame_bgr: np.ndarray) -> np.ndarray:
    h, w = frame_bgr.shape[:2]
    scale = _THUMB_WIDTH / max(w, 1)
    small = cv2.resize(
        frame_bgr, (max(int(w * scale), 1), max(int(h * scale), 1)), interpolation=cv2.INTER_AREA
    )
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)


def frame_difference(a: np.ndarray, b: np.ndarray) -> float:
    """2 枚の縮小グレースケールの平均絶対差分（0-255）。サイズが違えば最大値を返す。"""
    if a.shape != b.shape:
        return 255.0
    return float(np.mean(np.abs(a - b)))


def changed_region(reference: np.ndarray, current: np.ndarray) -> CardBox | None:
    """基準と現在の縮小画像の差分から、変化した領域の外接矩形を比率で返す。

    変化が無ければ None。領域は少し膨らませ、余白を足す。
    """
    if reference.shape != current.shape:
        return None
    mask = (np.abs(reference - current) > _REGION_PIXEL_THRESHOLD).astype(np.uint8)
    if not mask.any():
        return None
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    h, w = mask.shape
    x0, x1 = int(cols[0]) / w, (int(cols[-1]) + 1) / w
    y0, y1 = int(rows[0]) / h, (int(rows[-1]) + 1) / h
    pad = _REGION_PAD_RATIO
    x0, y0 = max(x0 - pad, 0.0), max(y0 - pad, 0.0)
    x1, y1 = min(x1 + pad, 1.0), min(y1 + pad, 1.0)
    return CardBox(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True)
class Decision:
    recognize: bool
    stable: bool = True
    region: CardBox | None = None  # None なら全画面


class FrameWatcher:
    """フレーム列から「認識すべき瞬間」と「見るべき領域」を判定する。"""

    def __init__(self, change_threshold: float = 4.0, stable_threshold: float = 1.5) -> None:
        self.change_threshold = change_threshold
        self.stable_threshold = stable_threshold
        self._reference: np.ndarray | None = None  # 最後に認識した（または基準にした）フレーム
        self._previous: np.ndarray | None = None
        self._ticks_since_attempt = 0
        self._unstable_failures = 0  # 静止しないまま認識して失敗した連続回数
        self._last_decision_stable = True

    def reset(self) -> None:
        self._reference = None
        self._previous = None
        self._ticks_since_attempt = 0
        self._unstable_failures = 0
        self._last_decision_stable = True

    @property
    def unstable_limit(self) -> int:
        """静止しない画面で次の認識を試みるまでの待ち回数（失敗が続くほど長くなる）。"""
        return min(2**self._unstable_failures, MAX_BACKOFF_TICKS)

    def decide(self, frame_bgr: np.ndarray) -> Decision:
        thumb = _thumbnail(frame_bgr)
        previous, self._previous = self._previous, thumb
        self._ticks_since_attempt += 1
        if self._reference is None:
            # 最初のフレームは基準にするだけ（起動時に出ている画面は次の変化で拾う）
            self._reference = thumb
            return Decision(recognize=False)
        if frame_difference(self._reference, thumb) < self.change_threshold:
            return Decision(recognize=False)
        stable = previous is not None and (
            frame_difference(previous, thumb) < self.stable_threshold
        )
        if not stable and self._ticks_since_attempt < self.unstable_limit:
            return Decision(recognize=False, stable=False)
        region = changed_region(self._reference, thumb)
        if region is not None and region.rw * region.rh > MAX_REGION_AREA:
            region = None
        self._ticks_since_attempt = 0
        self._last_decision_stable = stable
        return Decision(recognize=True, stable=stable, region=region)

    def should_recognize(self, frame_bgr: np.ndarray) -> bool:
        return self.decide(frame_bgr).recognize

    def mark_recognized(self, frame_bgr: np.ndarray, found: bool = True) -> None:
        """認識を実行した結果を反映する。`found` はカードを特定できたか。"""
        if found or self._last_decision_stable:
            self._reference = _thumbnail(frame_bgr)
            self._unstable_failures = 0
        else:
            # 動いている最中の失敗。基準は据え置き、静止したら再度変化として拾う
            self._unstable_failures += 1

    def set_reference(self, frame_bgr: np.ndarray) -> None:
        """現在の画面を「認識済み」として基準にする（起動直後など）。"""
        self._reference = _thumbnail(frame_bgr)
        self._unstable_failures = 0


@dataclass(frozen=True)
class WatchEvent:
    """1 回の監視ステップの結果。"""

    kind: str  # "no_window" | "minimized" | "idle" | "result" | "error"
    message: str = ""
    result: IdentifyResult | None = None
    elapsed_ms: float = 0.0
    region: CardBox | None = None


class WatchSession:
    """ウィンドウ探索・キャプチャ・変化判定・認識を 1 ステップずつ進める。

    依存はすべて呼び出し可能オブジェクトで受け取り、Qt や Win32 無しでテストできるようにする。
    """

    def __init__(
        self,
        find_window: Callable[[], WindowInfo | None],
        refresh_window: Callable[[WindowInfo], WindowInfo | None],
        grab: Callable[[tuple[int, int, int, int]], np.ndarray],
        identify: Callable[[np.ndarray], IdentifyResult],
        watcher: FrameWatcher | None = None,
        on_status: Callable[[str], None] | None = None,
        identify_fast: Callable[..., IdentifyResult] | None = None,
    ) -> None:
        self._find_window = find_window
        self._refresh_window = refresh_window
        self._grab = grab
        self._identify = identify
        self._identify_fast = identify_fast
        self.watcher = watcher or FrameWatcher()
        self._on_status = on_status
        self.window: WindowInfo | None = None
        self._ticks_until_search = 0

    def _status(self, text: str) -> None:
        if self._on_status is not None:
            self._on_status(text)

    def _locate_window(self) -> WindowInfo | None:
        if self.window is not None:
            refreshed = self._refresh_window(self.window)
            if refreshed is not None:
                return refreshed
            self.window = None
            self.watcher.reset()
        # ウィンドウ列挙は全トップレベルウィンドウの矩形・プロセス名取得を伴い軽くないため、
        # 見つからない間は間隔を空ける
        if self._ticks_until_search > 0:
            self._ticks_until_search -= 1
            return None
        found = self._find_window()
        if found is not None:
            self.window = found
            self.watcher.reset()
        else:
            self._ticks_until_search = WINDOW_SEARCH_EVERY_TICKS - 1
        return found

    def _recognize(self, frame: np.ndarray, region: CardBox | None) -> IdentifyResult:
        """高速パス（変化領域 → 全画面）で試し、見つからなければ通常パイプラインで全画面を読む。"""
        if region is not None:
            sub = crop(frame, region)
            if sub.shape[0] >= 32 and sub.shape[1] >= 32:
                result = (self._identify_fast or self._identify)(sub)
                if result.ok:
                    result.card_box = _to_frame_box(result.card_box, region)
                    return result
                log.info(
                    "変化領域では見つからず全画面で再試行: %s timings=%s",
                    result.error,
                    {k: round(v) for k, v in result.timings_ms.items()},
                )
        if self._identify_fast is not None:
            result = self._identify_fast(frame, full_frame=True)
            if result.ok:
                return result
            log.info(
                "高速パスで見つからず通常パイプラインへ: %s timings=%s",
                result.error,
                {k: round(v) for k, v in result.timings_ms.items()},
            )
        return self._identify(frame)

    def step(self) -> WatchEvent:
        window = self._locate_window()
        if window is None:
            return WatchEvent("no_window", "PTCGL のウィンドウが見つかりません")
        _, _, w, h = window.client_rect
        if w <= 0 or h <= 0:
            return WatchEvent("minimized", "PTCGL が最小化されています")
        try:
            frame = self._grab(window.client_rect)
        except Exception as exc:  # 画面ロック・解像度変更中など
            log.warning("キャプチャに失敗: %s", exc)
            return WatchEvent("error", f"キャプチャに失敗: {exc}")
        decision = self.watcher.decide(frame)
        if not decision.recognize:
            return WatchEvent("idle")

        self._status("認識中...")
        t0 = time.perf_counter()
        try:
            result = self._recognize(frame, decision.region)
        except Exception as exc:
            log.exception("認識中にエラー")
            self.watcher.mark_recognized(frame, found=False)
            return WatchEvent("error", f"認識に失敗: {exc}")
        self.watcher.mark_recognized(frame, found=result.ok)
        elapsed = (time.perf_counter() - t0) * 1000
        return WatchEvent(
            "result", result.error or "", result=result, elapsed_ms=elapsed, region=decision.region
        )


def _to_frame_box(box: CardBox | None, region: CardBox) -> CardBox | None:
    """領域内の比率座標をフレーム全体の比率座標に戻す。"""
    if box is None:
        return None
    return CardBox(
        region.rx + box.rx * region.rw,
        region.ry + box.ry * region.rh,
        box.rw * region.rw,
        box.rh * region.rh,
        box.confidence,
    )

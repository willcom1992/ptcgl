"""常駐モードの監視ロジック（watch.py）のテスト。Qt・Win32 に依存しない。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from ptcgl.capture.window import WindowInfo
from ptcgl.pipeline import IdentifyResult
from ptcgl.recognition.detector import CardBox
from ptcgl.watch import (
    MAX_BACKOFF_TICKS,
    FrameWatcher,
    WatchSession,
    _thumbnail,
    changed_region,
    frame_difference,
)


def _frame(value: int, size: tuple[int, int] = (720, 1280)) -> np.ndarray:
    return np.full((size[0], size[1], 3), value, dtype=np.uint8)


def _frame_with_card(base: int, card: int) -> np.ndarray:
    f = _frame(base)
    f[200:600, 400:700] = card  # 画面の一部に拡大カードが出た想定
    return f


def test_frame_difference_handles_shape_mismatch() -> None:
    a = np.zeros((10, 10), dtype=np.float32)
    b = np.zeros((5, 10), dtype=np.float32)
    assert frame_difference(a, b) == 255.0
    assert frame_difference(a, a) == 0.0


def test_changed_region_covers_the_card_with_padding() -> None:
    ref = _thumbnail(_frame(40))
    cur = _thumbnail(_frame_with_card(40, 220))
    region = changed_region(ref, cur)
    assert region is not None
    # カードは x 400-700 / 1280, y 200-600 / 720 にある。余白込みで包含していること
    assert region.rx <= 400 / 1280 and region.rx + region.rw >= 700 / 1280
    assert region.ry <= 200 / 720 and region.ry + region.rh >= 600 / 720
    assert region.rw * region.rh < 0.3
    assert changed_region(ref, ref) is None


def test_watcher_recognizes_immediately_when_card_appears() -> None:
    w = FrameWatcher(change_threshold=4.0, stable_threshold=1.5)
    assert w.decide(_frame(40)).recognize is False  # 最初のフレームは基準にするだけ
    assert w.decide(_frame(40)).recognize is False  # 変化なし
    d = w.decide(_frame_with_card(40, 220))
    assert d.recognize is True and d.stable is False  # 静止を待たずに認識
    assert d.region is not None and d.region.rw * d.region.rh < 0.3
    w.mark_recognized(_frame_with_card(40, 220), found=True)
    assert w.decide(_frame_with_card(40, 220)).recognize is False  # 同じ画面は再認識しない
    assert w.decide(_frame_with_card(40, 221)).recognize is False  # わずかな揺れも無視


def test_watcher_uses_full_frame_when_most_of_screen_changes() -> None:
    w = FrameWatcher()
    w.decide(_frame(40))
    d = w.decide(_frame(200))
    assert d.recognize is True and d.region is None


def test_watcher_keeps_reference_after_unstable_failure() -> None:
    w = FrameWatcher(change_threshold=4.0, stable_threshold=1.5)
    w.decide(_frame(40))
    assert w.decide(_frame_with_card(40, 150)).recognize is True
    w.mark_recognized(_frame_with_card(40, 150), found=False)  # 動いている最中に失敗
    # 基準は据え置きなので、静止した次のフレームも「変化」として拾う
    d = w.decide(_frame_with_card(40, 220))
    assert d.recognize is False  # 失敗直後 1 回は待つ（倍々の 1 回目）
    d = w.decide(_frame_with_card(40, 220))
    assert d.recognize is True and d.stable is True
    w.mark_recognized(_frame_with_card(40, 220), found=True)
    assert w.unstable_limit == 1
    assert w.decide(_frame_with_card(40, 220)).recognize is False


def test_watcher_backs_off_on_constant_animation() -> None:
    w = FrameWatcher(change_threshold=4.0, stable_threshold=1.5)
    w.decide(_frame(40))
    animation = iter(range(1, 10_000))

    def ticks_until_recognize() -> int:
        for i in range(1, 100):
            if w.decide(_frame_with_card(40, 100 + (next(animation) * 37) % 100)).recognize:
                return i
        return 0

    assert ticks_until_recognize() == 1
    w.mark_recognized(_frame(0), found=False)
    assert ticks_until_recognize() == 2
    w.mark_recognized(_frame(0), found=False)
    assert ticks_until_recognize() == 4
    w.mark_recognized(_frame(0), found=False)
    assert ticks_until_recognize() == 8
    assert w.unstable_limit <= MAX_BACKOFF_TICKS
    w.mark_recognized(_frame(0), found=True)  # 見つかれば元に戻る
    assert w.unstable_limit == 1


def test_watcher_stable_failure_updates_reference() -> None:
    w = FrameWatcher()
    w.decide(_frame(40))
    w.decide(_frame(90))
    d = w.decide(_frame(90))
    assert d.recognize is True and d.stable is True
    w.mark_recognized(_frame(90), found=False)
    assert w.decide(_frame(90)).recognize is False  # 静止画面での失敗は画面が変わるまで再試行しない
    assert w.unstable_limit == 1


def test_watcher_reset_forgets_history() -> None:
    w = FrameWatcher()
    w.decide(_frame(40))
    w.decide(_frame(200))
    w.reset()
    assert w.decide(_frame(200)).recognize is False
    assert w.decide(_frame(40)).recognize is True


def _info(hwnd: int = 1, client=(0, 0, 1280, 720)) -> WindowInfo:  # noqa: ANN001
    return WindowInfo(hwnd, "Pokemon TCG Live", "Pokemon TCG Live.exe", client, client, 96)


class _Fake:
    def __init__(self) -> None:
        self.window: WindowInfo | None = _info()
        self.frames: list[np.ndarray] = []
        self.identify_calls: list[tuple[int, int]] = []  # 渡された画像の (h, w)
        self.statuses: list[str] = []
        self.found_on_region = False

    def find(self) -> WindowInfo | None:
        return self.window

    def refresh(self, info: WindowInfo) -> WindowInfo | None:
        return self.window

    def grab(self, rect) -> np.ndarray:  # noqa: ANN001
        return self.frames.pop(0)

    def identify(self, frame: np.ndarray) -> IdentifyResult:
        self.identify_calls.append(frame.shape[:2])
        if self.found_on_region and frame.shape[0] < 720:
            from ptcgl.data.limitless import CardRef

            res = IdentifyResult(en_ref=CardRef("en", "MEG", "76"))
            res.card_box = CardBox(0.0, 0.0, 1.0, 1.0)
            res.match = SimpleNamespace(best=object())  # type: ignore[assignment]
            return res
        return IdentifyResult(error="拡大表示されたカードが見つかりません")

    def session(self) -> WatchSession:
        return WatchSession(
            self.find, self.refresh, self.grab, self.identify, on_status=self.statuses.append
        )


def test_session_reports_missing_window_and_minimized() -> None:
    fake = _Fake()
    fake.window = None
    s = fake.session()
    assert s.step().kind == "no_window"
    fake.window = _info(client=(0, 0, 0, 0))
    # 見つからなかった直後は列挙を間引く（WINDOW_SEARCH_EVERY_TICKS に 1 回）
    assert s.step().kind == "no_window"
    s._ticks_until_search = 0
    assert s.step().kind == "minimized"


def test_session_tries_changed_region_then_full_frame() -> None:
    fake = _Fake()
    fake.frames = [_frame(40), _frame_with_card(40, 220)]
    s = fake.session()
    assert s.step().kind == "idle"
    ev = s.step()
    assert ev.kind == "result" and ev.region is not None
    # 変化領域（小さい画像）→ 見つからず → 全画面 の順
    assert len(fake.identify_calls) == 2
    assert fake.identify_calls[0][0] < 720 and fake.identify_calls[1] == (720, 1280)
    assert fake.statuses == ["認識中..."]


def test_session_region_hit_skips_full_frame() -> None:
    fake = _Fake()
    fake.found_on_region = True
    fake.frames = [_frame(40), _frame_with_card(40, 220), _frame_with_card(40, 220)]
    s = fake.session()
    s.step()
    ev = s.step()
    assert ev.kind == "result" and ev.result is not None and ev.result.ok
    assert len(fake.identify_calls) == 1
    # 領域内の座標がフレーム全体の比率に戻っていること
    box = ev.result.card_box
    assert box is not None and 0 < box.rx < 0.4 and box.rw < 0.5
    assert s.step().kind == "idle"


def test_session_resets_watcher_when_window_is_replaced() -> None:
    fake = _Fake()
    fake.frames = [_frame(40), _frame(200)]
    s = fake.session()
    s.step()
    assert s.step().kind == "result"
    fake.window = None
    assert s.step().kind == "no_window"
    fake.window = _info(hwnd=2)
    fake.frames = [_frame(200), _frame(40)]
    s._ticks_until_search = 0  # 列挙の間引きを解除
    assert s.step().kind == "idle"  # 新しいウィンドウでは履歴を捨てて最初から
    assert s.step().kind == "result"


def test_session_survives_capture_and_identify_errors() -> None:
    fake = _Fake()

    def bad_grab(rect):  # noqa: ANN001, ANN202
        raise OSError("screen locked")

    s = WatchSession(fake.find, fake.refresh, bad_grab, fake.identify)
    ev = s.step()
    assert ev.kind == "error" and "screen locked" in ev.message

    def bad_identify(frame):  # noqa: ANN001, ANN202
        raise RuntimeError("ocr broke")

    fake.frames = [_frame(40), _frame(200), _frame(200), _frame(200)]
    s = WatchSession(fake.find, fake.refresh, fake.grab, bad_identify)
    s.step()
    ev = s.step()
    assert ev.kind == "error" and "ocr broke" in ev.message
    assert s.step().kind == "error"  # 静止したので再試行
    assert s.step().kind == "idle"  # 静止画面での失敗は基準になり、画面が変わるまで再試行しない

"""常駐モードの監視ロジック（watch.py）のテスト。Qt・Win32 に依存しない。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from ptcgl import watch as watch_mod
from ptcgl.capture.window import WindowInfo
from ptcgl.pipeline import IdentifyResult
from ptcgl.recognition.detector import CardBox
from ptcgl.watch import (
    MAX_BACKOFF_TICKS,
    STABLE_FAILURE_WAIT_TICKS,
    ChangeCanceller,
    FrameWatcher,
    WatchSession,
    _thumbnail,
    changed_region,
    frame_difference,
    masked_difference,
)


@pytest.fixture(autouse=True)
def _no_cancel_recapture(monkeypatch) -> None:  # noqa: ANN001
    """既定では中断判定の撮り直しを行わない（テストの所要時間に結果が左右されないように）。

    中断を試すテストは自分で CANCEL_CHECK_INTERVAL_S を 0 に上書きする。
    """
    monkeypatch.setattr(watch_mod, "CANCEL_CHECK_INTERVAL_S", 1e9)


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


def test_watcher_waits_briefly_after_stable_failures() -> None:
    # 演出の合間に静止する盤面: 静止画面で失敗した後は、次の変化から一定ティック待つ（倍々にしない）
    w = FrameWatcher(change_threshold=4.0, stable_threshold=1.5)
    w.decide(_frame(40))
    value = 40

    def change_and_count() -> int:
        """画面を変え、認識が走るまでのティック数を返す。"""
        nonlocal value
        value = (value + 60) % 256
        for i in range(1, 100):
            if w.decide(_frame(value)).recognize:
                return i
        return 0

    assert change_and_count() == 1  # 最初は静止を待たずに認識
    w.mark_recognized(_frame(value), found=False)  # 動いている最中の失敗
    d = w.decide(_frame(value))
    assert d.recognize is True and d.stable is True  # 静止したので待たずに読む
    w.mark_recognized(_frame(value), found=False)  # 静止画面での失敗 1 回目
    assert w.stable_limit == STABLE_FAILURE_WAIT_TICKS == 2
    assert change_and_count() == 2
    for _ in range(3):  # 続いても延びない（手札に乗せたカードの表示が 2 秒待たされていた）
        w.mark_recognized(_frame(value), found=False)
        assert change_and_count() == 2
    w.mark_recognized(_frame(value), found=True)  # 見つかれば元に戻る
    assert w.stable_limit == 0 and w.unstable_limit == 1
    assert change_and_count() == 1


def test_watcher_stable_backoff_counts_from_the_change() -> None:
    # 静止画面での失敗のあと、しばらく静止してから画面が変わっても待ちは「変化してから」数える
    w = FrameWatcher(change_threshold=4.0, stable_threshold=1.5)
    w.decide(_frame(40))
    w.decide(_frame(100))
    w.decide(_frame(100))
    w.mark_recognized(_frame(100), found=False)  # 静止画面での失敗 → 次の変化から 2 ティック待つ
    for _ in range(10):
        assert w.decide(_frame(100)).recognize is False  # 変化なし
    assert w.decide(_frame(160)).recognize is False  # 変化 1 ティック目: 待つ
    assert w.decide(_frame(160)).recognize is True  # 2 ティック目


def test_watcher_unstable_failure_then_stable_frame_is_not_delayed() -> None:
    # 動いている最中の失敗の直後に静止したら、静止画面の待ちは掛からない
    w = FrameWatcher(change_threshold=4.0, stable_threshold=1.5)
    w.decide(_frame(40))
    assert w.decide(_frame(200)).recognize is True
    w.mark_recognized(_frame(200), found=False)
    assert w.stable_limit == 0
    assert w.decide(_frame(200)).recognize is True


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


def test_session_falls_back_to_full_ocr_when_identify_is_given() -> None:
    # 通常パイプラインが渡されていれば（手動領域の設定時）、高速パスの後に最終手段として呼ぶ
    fake = _Fake()
    fast_calls: list[bool] = []

    def identify_fast(frame: np.ndarray, full_frame: bool = False, cancel=None) -> IdentifyResult:  # noqa: ANN001
        fast_calls.append(full_frame)
        res = IdentifyResult(error="番号行が見つかりません（高速パス）")
        if full_frame:
            assert cancel is not None  # 全画面の高速パスには中断判定を渡す
            res.hp_badge_rois = 0
        return res

    fake.frames = [_frame(40), _frame_with_card(40, 220)]
    s = WatchSession(fake.find, fake.refresh, fake.grab, fake.identify, identify_fast=identify_fast)
    s.step()
    ev = s.step()
    assert ev.kind == "result" and ev.result is not None and not ev.result.ok
    assert fast_calls == [False, True]  # 変化領域 → 全画面
    assert fake.identify_calls == [(720, 1280)]


def test_session_without_identify_stops_after_fast_path() -> None:
    # 常駐モードの既定: 高速パスが全行認識と同じ範囲を読むので通常パイプラインは呼ばない
    fake = _Fake()
    fast_calls: list[bool] = []

    def identify_fast(frame: np.ndarray, full_frame: bool = False, cancel=None) -> IdentifyResult:  # noqa: ANN001
        fast_calls.append(full_frame)
        return IdentifyResult(error="番号行が見つかりません（高速パス）")

    fake.frames = [_frame(40), _frame_with_card(40, 220)]
    s = WatchSession(fake.find, fake.refresh, fake.grab, None, identify_fast=identify_fast)
    s.step()
    ev = s.step()
    assert ev.kind == "result" and ev.result is not None and not ev.result.ok
    assert fast_calls == [False, True]
    assert fake.identify_calls == []
    with pytest.raises(ValueError):
        WatchSession(fake.find, fake.refresh, fake.grab, None)


def test_session_aborts_when_screen_changes_during_recognition(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(watch_mod, "CANCEL_CHECK_INTERVAL_S", 0.0)
    fake = _Fake()
    full_calls = 0

    def identify_fast(frame: np.ndarray, full_frame: bool = False, cancel=None) -> IdentifyResult:  # noqa: ANN001
        nonlocal full_calls
        if full_frame:
            full_calls += 1
            if cancel():
                return IdentifyResult(error="画面が変わったため中断", cancelled=True)
        return IdentifyResult(error="番号行が見つかりません（高速パス）")

    # 基準 → カード出現（認識開始）→ 変化領域で失敗した直後の撮り直しで画面が大きく変わっている
    fake.frames = [_frame(40), _frame_with_card(40, 220), _frame(200)]
    s = WatchSession(fake.find, fake.refresh, fake.grab, None, identify_fast=identify_fast)
    s.step()
    ev = s.step()
    assert ev.kind == "aborted" and ev.result is not None and ev.result.cancelled
    # 変化領域の直後は箱が分からないので判定せず、全画面の高速パスの中で打ち切る
    assert full_calls == 1
    assert not fake.frames

    # 打ち切りは動いている最中の失敗と同じく試行間隔を延ばす（演出中に OCR が毎ティック走らない）。
    # ただし基準は据え置きで、静止した画面は間隔に関係なく再び「変化」として読む
    # （撮り直し 2 回分も同じ画面なので中断しない）
    assert s.watcher.unstable_limit == 2
    card = _frame_with_card(40, 220)
    fake.frames = [card, card, card]
    ev = s.step()
    assert ev.kind == "result" and full_calls == 2
    assert s.watcher.unstable_limit == 1  # 認識できたら元に戻る


def test_session_aborts_between_fast_path_and_full_pipeline(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(watch_mod, "CANCEL_CHECK_INTERVAL_S", 0.0)
    fake = _Fake()

    def identify_fast(frame: np.ndarray, full_frame: bool = False, cancel=None) -> IdentifyResult:  # noqa: ANN001
        return IdentifyResult(error="番号行が見つかりません（高速パス）")

    # 変化領域の後は撮り直さない。全画面の高速パスの後の撮り直しで画面が変わっている
    card = _frame_with_card(40, 220)
    fake.frames = [_frame(40), card, _frame(200)]
    s = WatchSession(fake.find, fake.refresh, fake.grab, fake.identify, identify_fast=identify_fast)
    s.step()
    ev = s.step()
    assert ev.kind == "aborted"
    assert fake.identify_calls == []  # 通常パイプラインは呼ばれない


def test_masked_difference_limits_to_boxes() -> None:
    a = np.zeros((90, 160), dtype=np.float32)
    b = a.copy()
    b[:, 80:] = 100  # 右半分だけ変化
    assert masked_difference(a, b, [CardBox(0.0, 0.0, 0.5, 1.0)]) == 0.0
    assert masked_difference(a, b, [CardBox(0.5, 0.0, 0.5, 1.0)]) == 100.0
    assert masked_difference(a, b, []) is None
    assert masked_difference(a, b[:45], [CardBox(0.0, 0.0, 1.0, 1.0)]) == 255.0


def test_canceller_continues_when_only_background_changes(monkeypatch) -> None:  # noqa: ANN001
    """詳細表示の背後で盤面の演出が続いても、文字の箱の中が変わらなければ打ち切らない。"""
    monkeypatch.setattr(watch_mod, "CANCEL_CHECK_INTERVAL_S", 0.0)
    card = _frame_with_card(40, 220)
    background = _frame_with_card(120, 220)  # カードの外だけ明るくなった（演出）
    gone = card.copy()
    gone[200:600, 400:700] = 90  # カードの中が変わった（表示が消えた）
    frames = [background, background, gone]
    rect = (0, 0, 1280, 720)
    text_boxes = [CardBox(400 / 1280, 200 / 720, 300 / 1280, 400 / 720)]

    shimmer = _frame_with_card(120, 228)  # 背景の演出に加え、ホロ加工の光で箱の中も少し変わった
    frames.insert(1, shimmer)

    cancel = ChangeCanceller(lambda r: frames.pop(0), card, rect, threshold=4.0)
    assert cancel(text_boxes) is False  # 画面全体は変わったが箱の中は不変
    assert cancel.background_changes == 1
    assert cancel.last_diff >= 4.0 and cancel.last_text_diff == 0.0
    assert cancel(text_boxes) is False  # 箱の中の差分 8 はしきい値（4 × 4 = 16）未満なので続ける
    assert cancel.last_text_diff == 8.0 and cancel.background_changes == 2
    assert cancel() is True  # 箱が無ければ画面全体で判定する
    assert cancel(text_boxes) is True  # 箱の中が大きく変われば打ち切る
    assert cancel.last_text_diff is not None and cancel.last_text_diff >= 16.0
    assert cancel.background_changes == 2


def test_canceller_rate_limits_and_tolerates_capture_errors(monkeypatch) -> None:  # noqa: ANN001
    calls = 0

    def grab(rect) -> np.ndarray:  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise RuntimeError("capture failed")

    cancel = ChangeCanceller(grab, _frame(40), (0, 0, 1280, 720), threshold=4.0)
    assert cancel() is False and calls == 0  # 間隔内（autouse で 1e9 秒）は撮り直さない
    monkeypatch.setattr(watch_mod, "CANCEL_CHECK_INTERVAL_S", 0.0)
    assert cancel() is False and calls == 1  # 撮れなければ変わっていない扱い


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
    # 例外の内容は画面に出さずログに残す
    assert ev.kind == "error" and ev.message == "キャプチャに失敗しました"

    def bad_identify(frame):  # noqa: ANN001, ANN202
        raise RuntimeError("ocr broke")

    fake.frames = [_frame(40), _frame(200), _frame(200), _frame(200)]
    s = WatchSession(fake.find, fake.refresh, fake.grab, bad_identify)
    s.step()
    ev = s.step()
    assert ev.kind == "error" and ev.message == "認識に失敗しました"
    assert s.step().kind == "error"  # 静止したので再試行
    assert s.step().kind == "idle"  # 静止画面での失敗は基準になり、画面が変わるまで再試行しない

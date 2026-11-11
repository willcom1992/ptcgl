"""常駐モードのワーカースレッド。

キャプチャ・OCR・HTTP はすべてこのスレッドで行い、UI スレッドをブロックしない。
Pipeline（OCR エンジン・HTTP クライアント・SQLite）はこのスレッドが所有し、
候補の確定などメインスレッドからの依頼はキュー接続のスロットで受ける。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ptcgl.capture.screen import ScreenCapture
from ptcgl.capture.window import (
    WindowInfo,
    find_window,
    get_window_info,
    is_window_alive,
    is_window_minimized,
)
from ptcgl.config import AppConfig
from ptcgl.pipeline import Pipeline
from ptcgl.watch import FrameWatcher, WatchSession

log = logging.getLogger(__name__)


class RecognitionWorker(QObject):
    """QThread 上で WatchSession を定期実行し、結果をシグナルで通知する。"""

    status_changed = Signal(str)
    result_ready = Signal(object)  # IdentifyResult
    window_changed = Signal(object)  # WindowInfo | None

    def __init__(self, pipeline: Pipeline, config: AppConfig) -> None:
        super().__init__()
        self._pipeline = pipeline
        self._config = config
        self._timer: QTimer | None = None
        self._paused = False
        self._busy = False
        self._stopping = False
        self._last_window_key: tuple | None = ()
        self._capture = ScreenCapture()
        self._session = WatchSession(
            find_window=self._find,
            refresh_window=self._refresh,
            grab=self._capture.grab,
            identify=self._pipeline.identify_frame,
            identify_fast=self._pipeline.identify_fast,
            watcher=FrameWatcher(
                change_threshold=config.capture.change_threshold,
                stable_threshold=config.capture.stable_threshold,
            ),
            on_status=self.status_changed.emit,
        )

    # --- ウィンドウ探索（ワーカースレッドで実行） ---------------------------

    def _find(self) -> WindowInfo | None:
        tw = self._config.target_window
        return find_window(title_pattern=tw.title_pattern, process_name=tw.process_name)

    def _refresh(self, info: WindowInfo) -> WindowInfo | None:
        if not is_window_alive(info.hwnd):
            return None
        if is_window_minimized(info.hwnd):
            return WindowInfo(
                hwnd=info.hwnd,
                title=info.title,
                process_name=info.process_name,
                rect=info.rect,
                client_rect=(0, 0, 0, 0),
                dpi=info.dpi,
            )
        try:
            return get_window_info(info.hwnd)
        except Exception:
            return None

    # --- スロット ------------------------------------------------------------

    @Slot()
    def start(self) -> None:
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.setInterval(max(int(self._config.capture.interval_ms), 100))
            self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.status_changed.emit("PTCGL のウィンドウを探しています...")

    @Slot()
    def stop(self) -> None:
        self._stopping = True
        if self._timer is not None:
            self._timer.stop()
        self._capture.close()

    def request_stop(self) -> None:
        """どのスレッドからでも呼べる停止要求。以降のティックを無視する。"""
        self._stopping = True

    @Slot(bool)
    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self.status_changed.emit("一時停止中（Ctrl+R で再開）")
        else:
            self._session.watcher.reset()
            self.status_changed.emit("監視を再開しました")

    @Slot(object, object, bool)
    def confirm(self, en_ref: object, card: object, same_art: bool) -> None:
        """ユーザーが候補一覧から選んだ日本語版を保存する。"""
        try:
            self._pipeline.repo.confirm(en_ref, card, same_art)  # type: ignore[arg-type]
            self.status_changed.emit(f"選択を保存しました: {card.label}")  # type: ignore[attr-defined]
        except Exception as exc:
            log.exception("選択の保存に失敗")
            self.status_changed.emit(f"選択の保存に失敗: {exc}")

    @Slot(object)
    def identify_ref(self, ref: object) -> None:
        """セット・番号指定の照合（CLI からの起動やテスト用）。"""
        result = self._pipeline.identify_ref(ref)  # type: ignore[arg-type]
        self.result_ready.emit(result)

    @Slot()
    def _tick(self) -> None:
        if self._paused or self._busy or self._stopping:
            return
        self._busy = True
        try:
            event = self._session.step()
        finally:
            self._busy = False

        window = self._session.window
        key = (window.hwnd, window.client_rect) if window is not None else None
        if key != self._last_window_key:
            self._last_window_key = key
            self.window_changed.emit(window)

        if event.kind == "result" and event.result is not None:
            r = event.result
            log.info(
                "認識 %.0fms ok=%s en=%s region=%s timings=%s error=%s",
                event.elapsed_ms,
                r.ok,
                r.en_ref,
                f"{event.region.rw * event.region.rh:.2f}" if event.region else "full",
                {k: round(v) for k, v in r.timings_ms.items()},
                r.error,
            )
            self.result_ready.emit(event.result)
            if event.result.ok:
                secs = event.elapsed_ms / 1000
                self.status_changed.emit(f"認識 {secs:.1f} 秒")
            else:
                self.status_changed.emit(event.message or "カードを特定できません")
        elif event.kind in ("no_window", "minimized", "error"):
            self.status_changed.emit(event.message)


class WorkerThread(QThread):
    """RecognitionWorker を動かすスレッド。終了時にタイマーを止めてから join する。"""

    def __init__(self, worker: RecognitionWorker) -> None:
        super().__init__()
        self.worker = worker
        worker.moveToThread(self)
        self.started.connect(worker.start)

    def shutdown(self, timeout_ms: int = 30000) -> bool:
        """タイマーを止めてスレッドの終了を待つ。終了できたら True。

        認識の途中（Limitless の取得中など）は中断できないため、HTTP のタイムアウトを含めて
        十分に待つ。呼び出し側は True のときだけ共有リソース（HTTP クライアント等）を閉じる。
        """
        if not self.isRunning():
            return True
        # ワーカーのスロットはワーカースレッドで実行されるため、直接呼ばずイベント経由で止める
        from PySide6.QtCore import QMetaObject, Qt

        self.worker.request_stop()
        QMetaObject.invokeMethod(self.worker, "stop", Qt.ConnectionType.QueuedConnection)
        self.quit()
        if self.wait(timeout_ms):
            return True
        log.warning("ワーカースレッドが %d ms 以内に終了しませんでした", timeout_ms)
        return False

"""ワーカーと表示ウィンドウの接続テスト（pytest-qt、ネットワーク・画面キャプチャ無し）。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ptcgl.capture.window import WindowInfo
from ptcgl.config import AppConfig
from ptcgl.data.cards import JapaneseCard, MatchResult
from ptcgl.data.limitless import CardPage, CardRef
from ptcgl.pipeline import IdentifyResult

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")


def _match(en_number: str = "76", jp_number: str = "40", image: Path | None = None) -> MatchResult:
    en = CardRef("en", "MEG", en_number)
    jp = JapaneseCard(CardRef("jp", "M1L", jp_number), "リオル", "M1L", "x", "http://x", image)
    page = CardPage(en, "Riolu", "Mega Evolution", "x", 70, None, None)
    return MatchResult(en=page, best=jp, same_art=True, confidence=0.95, candidates=[jp])


class _FakeRepo:
    def __init__(self) -> None:
        self.confirmed: list[tuple] = []

    def confirm(self, en_ref, jp, same_art) -> None:  # noqa: ANN001
        self.confirmed.append((en_ref, jp, same_art))


class _FakePipeline:
    def __init__(self) -> None:
        self.repo = _FakeRepo()
        self.calls = 0

    def identify_frame(
        self, frame: np.ndarray, box=None, native_retry: bool = True
    ) -> IdentifyResult:  # noqa: ANN001
        self.calls += 1
        assert native_retry is False  # 常駐モードでは元解像度の読み直しを省く
        return IdentifyResult(en_ref=CardRef("en", "MEG", "76"), match=_match())

    def identify_fast(self, frame: np.ndarray, full_frame: bool = False) -> IdentifyResult:
        return IdentifyResult(error="番号行が見つかりません（高速パス）")

    def identify_ref(self, ref: CardRef) -> IdentifyResult:
        return IdentifyResult(en_ref=ref, match=_match())

    def confirm(self, en_ref, jp, same_art) -> None:  # noqa: ANN001
        self.repo.confirm(en_ref, jp, same_art)


def _worker(monkeypatch, pipeline: _FakePipeline, frames: list[np.ndarray]):  # noqa: ANN001, ANN202
    from ptcgl.ui import worker as worker_mod

    info = WindowInfo(1, "Pokemon TCG Live", "Pokemon TCG Live.exe", (0, 0, 8, 8), (0, 0, 8, 8), 96)
    monkeypatch.setattr(worker_mod, "find_window", lambda **kw: info)
    monkeypatch.setattr(worker_mod, "is_window_alive", lambda hwnd: True)
    monkeypatch.setattr(worker_mod, "is_window_minimized", lambda hwnd: False)
    monkeypatch.setattr(worker_mod, "get_window_info", lambda hwnd: info)
    monkeypatch.setattr(worker_mod.ScreenCapture, "grab", lambda self, rect=None: frames.pop(0))
    return worker_mod.RecognitionWorker(pipeline, AppConfig())  # type: ignore[arg-type]


def test_worker_emits_window_then_result(qtbot, monkeypatch) -> None:  # noqa: ANN001
    pipeline = _FakePipeline()
    frame = np.full((8, 8, 3), 50, dtype=np.uint8)
    changed = np.full((8, 8, 3), 200, dtype=np.uint8)
    w = _worker(monkeypatch, pipeline, [frame, changed])
    windows: list = []
    results: list = []
    statuses: list[str] = []
    w.window_changed.connect(windows.append)
    w.result_ready.connect(results.append)
    w.status_changed.connect(statuses.append)

    w._tick()
    assert len(windows) == 1 and windows[0].title == "Pokemon TCG Live"
    assert results == []
    w._tick()
    assert len(results) == 1 and results[0].ok
    assert pipeline.calls == 1
    assert statuses[-1] == "監視中"


def test_worker_pause_skips_ticks(qtbot, monkeypatch) -> None:  # noqa: ANN001
    pipeline = _FakePipeline()
    w = _worker(monkeypatch, pipeline, [])
    w.set_paused(True)
    w._tick()  # grab は呼ばれない（frames が空でも落ちない）
    assert pipeline.calls == 0


def test_worker_confirm_delegates_to_repository(qtbot, monkeypatch) -> None:  # noqa: ANN001
    pipeline = _FakePipeline()
    w = _worker(monkeypatch, pipeline, [])
    m = _match()
    statuses: list[str] = []
    w.status_changed.connect(statuses.append)
    w.confirm(m.en.ref, m.best, False)
    assert pipeline.repo.confirmed == [(m.en.ref, m.best, False)]
    assert statuses == []  # 成功の表示は MainWindow 側だけが出す


def test_worker_emits_fixed_status_on_failed_recognition(qtbot, monkeypatch) -> None:  # noqa: ANN001
    pipeline = _FakePipeline()

    def fail(frame, box=None, native_retry=True):  # noqa: ANN001, ANN202
        return IdentifyResult(
            error="拡大表示されたカードが見つかりません（番号・HP のいずれも検出できず）"
        )

    pipeline.identify_frame = fail  # type: ignore[method-assign]
    frame = np.full((8, 8, 3), 50, dtype=np.uint8)
    changed = np.full((8, 8, 3), 200, dtype=np.uint8)
    w = _worker(monkeypatch, pipeline, [frame, changed])
    statuses: list[str] = []
    w.status_changed.connect(statuses.append)
    w._tick()
    w._tick()
    # 失敗理由は画面に出さず、固定の短い文言だけを出す
    assert statuses[-1] == "カードを特定できません"


def test_main_window_show_result_keeps_last_card_on_failure(qtbot, tmp_path: Path) -> None:  # noqa: ANN001
    from ptcgl.imageio import write_image
    from ptcgl.ui.main_window import MainWindow

    image = tmp_path / "jp.png"
    write_image(image, np.full((4, 4, 3), 200, dtype=np.uint8))
    cfg = AppConfig()
    cfg.overlay.exclude_from_capture = False
    win = MainWindow(cfg)
    qtbot.addWidget(win)
    win.show_result(IdentifyResult(match=_match(image=image)))
    assert "リオル" in win._name_label.text()
    win.show_watch_status("監視中")
    win.show_result(IdentifyResult(error="拡大表示されたカードが見つかりません"))
    assert "リオル" in win._name_label.text()
    # 失敗理由は画面に出さない（ログに残す）。状態行はワーカーが更新する
    assert win._watch_label.text() == "監視中"


def test_main_window_candidate_click_requests_confirm(qtbot) -> None:  # noqa: ANN001
    from ptcgl.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.overlay.exclude_from_capture = False
    win = MainWindow(cfg)
    qtbot.addWidget(win)
    m = _match()
    other = JapaneseCard(CardRef("jp", "SV1S", "5"), "リオル", "SV1S", "y", "http://y")
    m.candidates = [m.best, other]
    win.show_match(m)
    received: list = []
    win.confirm_requested.connect(lambda en, jp, same: received.append((en, jp, same)))
    win._on_candidate_clicked(win._candidates.item(1))
    assert received == [(m.en.ref, other, False)]


def test_main_window_pause_shortcut_emits_toggle(qtbot) -> None:  # noqa: ANN001
    from ptcgl.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.overlay.exclude_from_capture = False
    win = MainWindow(cfg)
    qtbot.addWidget(win)
    states: list[bool] = []
    win.pause_toggled.connect(states.append)
    win.toggle_paused()
    win.toggle_paused()
    assert states == [True, False]


def test_main_window_candidate_click_marks_same_art_by_illustrator(qtbot) -> None:  # noqa: ANN001
    from ptcgl.ui.main_window import MainWindow

    cfg = AppConfig()
    cfg.overlay.exclude_from_capture = False
    win = MainWindow(cfg)
    qtbot.addWidget(win)
    m = _match()  # 英語版のイラストレーターは "x"
    same = JapaneseCard(CardRef("jp", "SV1S", "5"), "リオル", "SV1S", "X", "http://y")
    m.candidates = [m.best, same]
    win.show_match(m)
    received: list = []
    win.confirm_requested.connect(lambda en, jp, same_art: received.append(same_art))
    win._on_candidate_clicked(win._candidates.item(1))
    assert received == [True]


def test_main_window_title_does_not_match_ptcgl_pattern(qtbot) -> None:  # noqa: ANN001
    import re

    from ptcgl.ui.main_window import WINDOW_TITLE, MainWindow

    cfg = AppConfig()
    cfg.overlay.exclude_from_capture = False
    win = MainWindow(cfg)
    qtbot.addWidget(win)
    assert win.windowTitle() == WINDOW_TITLE
    # 自分のウィンドウを PTCGL と誤認しない
    assert re.search(cfg.target_window.title_pattern, WINDOW_TITLE) is None

"""メインウィンドウ（日本語カード画像の表示）。

- 画像はウィンドウサイズに追従して拡縮する
- 候補が複数あるときは一覧から選択でき、選択結果は呼び出し側へシグナルで通知する
- Ctrl+T で最前面表示を切り替える。位置・サイズはモニターに対する比率で設定に保存する
- Ctrl+R で常駐モードの監視を一時停止・再開する。Ctrl+Q で終了する
- 自分自身を画面キャプチャから除外し、PTCGL に重ねて置いても認識の邪魔にならないようにする
- 状態行には操作に関わる状態だけを出す。認識秒数や失敗理由はログファイルに残す
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QKeySequence,
    QMoveEvent,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from ptcgl.config import AppConfig

if TYPE_CHECKING:
    from ptcgl.capture.window import WindowInfo
    from ptcgl.pipeline import IdentifyResult


# ウィンドウのタイトル。PTCGL 側のウィンドウ検出（target_window.title_pattern）に
# 引っかからないよう、"Pokemon TCG Live" という並びは含めない
WINDOW_TITLE = "PTCGL 日本語カードビューア"
_MAX_CANDIDATE_ROWS = 3  # 候補一覧をこの行数までの高さに抑える


class MainWindow(QMainWindow):
    """日本語カード画像を表示する常時最前面ウィンドウ。"""

    candidate_selected = Signal(object)  # JapaneseCard
    confirm_requested = Signal(object, object, bool)  # (en CardRef, JapaneseCard, same_art)
    pause_toggled = Signal(bool)

    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__()
        self.config = config or AppConfig()
        self._pixmap: QPixmap | None = None
        self._match = None
        self._paused = False

        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.config.overlay.always_on_top)

        self._image_label = QLabel("PTCGL のカードを待機中...")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(120, 160)
        self._name_label = QLabel("")
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setWordWrap(True)
        self._name_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("color: #b36b00;")
        self._watch_label = QLabel("")
        self._watch_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._watch_label.setStyleSheet("color: #666; font-size: 12px;")
        self._candidates = QListWidget()
        self._candidates.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._candidates.setTextElideMode(Qt.TextElideMode.ElideRight)  # 長い名前は省略して 1 行に
        self._candidates.setUniformItemSizes(True)
        self._candidates.hide()
        self._candidates.itemClicked.connect(self._on_candidate_clicked)

        layout = QVBoxLayout()
        layout.setSpacing(2)  # カード名・候補一覧・状態行を詰めて並べる
        layout.addWidget(self._image_label, stretch=1)
        layout.addWidget(self._name_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._candidates)
        layout.addWidget(self._watch_label)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        self._status_label.hide()  # 空のラベルが余白にならないよう、文字があるときだけ出す

        QShortcut(QKeySequence("Ctrl+T"), self).activated.connect(self.toggle_always_on_top)
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self.toggle_paused)
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.close)
        self._restore_geometry()

    # --- 表示 -------------------------------------------------------------

    def show_card(self, name_ja: str, image_path: Path) -> None:
        """日本語カードの名前と画像を表示する。"""
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self._pixmap = None
            self._image_label.setText(f"画像を読み込めません: {image_path}")
        else:
            self._pixmap = pixmap
            self._rescale()
        self._name_label.setText(name_ja)

    def show_match(self, match) -> None:  # noqa: ANN001 - MatchResult
        """照合結果を表示する。候補が複数あれば一覧も出す。"""
        self._match = match
        if match.best is not None and match.best.image_path:
            self.show_card(match.best.label, match.best.image_path)
        else:
            self._pixmap = None
            self._image_label.setText("対応する日本語版が見つかりません")
            self._name_label.setText(match.en.name)
        # 照合の理由文はログに残し、画面にはユーザーの確認が要る場合だけ短く出す
        same_art = match.best is None or match.same_art
        self.show_message("" if same_art else "別イラスト（要確認）")

        self._candidates.clear()
        if len(match.candidates) > 1:
            for card in match.candidates:
                item = QListWidgetItem(card.label or str(card.ref))
                item.setData(Qt.ItemDataRole.UserRole, card)
                self._candidates.addItem(item)
            self._fit_candidates()
            self._candidates.show()
        else:
            self._candidates.hide()
        # 状態行や候補一覧の表示・非表示で画像の領域が変わるので、レイアウト確定後に拡縮し直す
        QTimer.singleShot(0, self._rescale)

    def _fit_candidates(self) -> None:
        """候補一覧の高さを行数に合わせる（最大 _MAX_CANDIDATE_ROWS 行、それ以上はスクロール）。"""
        rows = min(self._candidates.count(), _MAX_CANDIDATE_ROWS)
        row_h = self._candidates.sizeHintForRow(0)
        self._candidates.setFixedHeight(rows * row_h + self._candidates.frameWidth() * 2)

    def show_message(self, text: str) -> None:
        self._status_label.setText(text)
        self._status_label.setVisible(bool(text))

    def show_watch_status(self, text: str) -> None:
        """常駐モードの監視状態（ウィンドウ検出・認識中・一時停止など）を表示する。"""
        self._watch_label.setText(text)

    def show_window_info(self, info: WindowInfo | None) -> None:
        """監視対象ウィンドウの状態を表示する（ワーカーの window_changed から呼ばれる）。"""
        if info is None:
            self.show_watch_status("PTCGL のウィンドウが見つかりません")
            return
        _, _, w, h = info.client_rect
        if w <= 0 or h <= 0:
            self.show_watch_status("PTCGL が最小化されています")
        else:
            self.show_watch_status("監視中")

    def show_result(self, result: IdentifyResult) -> None:
        """常駐モードの認識結果を反映する。

        同じカードが続けて認識された場合は表示を変えない。カードが見つからなかった場合も
        直前の表示を残す（誤ったカードを出さないため）。失敗の理由はログに残し、状態行は
        ワーカーの `status_changed` が「カードを特定できません」に更新する。
        """
        if result.match is None or result.match.best is None:
            return
        current = self._match
        if (
            current is not None
            and current.en.ref == result.match.en.ref
            and current.best is not None
            and current.best.ref == result.match.best.ref
        ):
            return
        self.show_match(result.match)

    def _on_candidate_clicked(self, item: QListWidgetItem) -> None:
        card = item.data(Qt.ItemDataRole.UserRole)
        if card.image_path:
            self.show_card(card.label, card.image_path)
        self.candidate_selected.emit(card)
        if self._match is not None:
            # 選んだ候補を記憶し、次回から同じ英語カードにはこの日本語版を出す。
            # 同一絵柄かどうかはイラストレーターの一致で判定する（別イラスト扱いのまま固定しない）
            from ptcgl.data.cards import same_illustrator

            same_art = same_illustrator(self._match.en.illustrator, card.illustrator)
            self.confirm_requested.emit(self._match.en.ref, card, same_art)
            self.show_message("選択した候補を記憶しました: " + card.label)

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        scaled = self._pixmap.scaled(
            self._image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    # --- ウィンドウ挙動 ---------------------------------------------------

    def toggle_always_on_top(self) -> None:
        on = not bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.config.overlay.always_on_top = on
        self.show()

    def toggle_paused(self) -> None:
        self._paused = not self._paused
        self.pause_toggled.emit(self._paused)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        # setWindowFlag でネイティブウィンドウが作り直されるため、表示のたびに適用する
        if self.config.overlay.exclude_from_capture:
            from ptcgl.capture.window import set_exclude_from_capture

            set_exclude_from_capture(int(self.winId()), True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()
        self._store_geometry()

    def moveEvent(self, event: QMoveEvent) -> None:  # noqa: N802
        super().moveEvent(event)
        self._store_geometry()

    def _store_geometry(self) -> None:
        screen = self.screen()
        if screen is None:
            return
        geo = screen.geometry()
        if geo.width() == 0 or geo.height() == 0:
            return
        frame = self.frameGeometry()
        self.config.overlay.monitor_name = screen.name()
        self.config.overlay.ratio_pos = (
            (frame.x() - geo.x()) / geo.width(),
            (frame.y() - geo.y()) / geo.height(),
        )
        self.config.overlay.ratio_size = (
            self.width() / geo.width(),
            self.height() / geo.height(),
        )

    def _restore_geometry(self) -> None:
        from PySide6.QtGui import QGuiApplication

        ov = self.config.overlay
        screens = QGuiApplication.screens()
        screen = next((s for s in screens if s.name() == ov.monitor_name), None)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(420, 620)
            return
        geo = screen.geometry()
        w = max(int(geo.width() * ov.ratio_size[0]), 200)
        h = max(int(geo.height() * ov.ratio_size[1]), 260)
        self.resize(w, h)
        if ov.ratio_pos is not None:
            x = geo.x() + int(geo.width() * ov.ratio_pos[0])
            y = geo.y() + int(geo.height() * ov.ratio_pos[1])
            # 画面外に出ないように補正
            x = min(max(x, geo.x()), geo.x() + geo.width() - w)
            y = min(max(y, geo.y()), geo.y() + geo.height() - h)
            self.move(x, y)

"""メインウィンドウ（日本語カード画像の表示）。

- 画像はウィンドウサイズに追従して拡縮する
- 候補が複数あるときは一覧から選択でき、選択結果は呼び出し側へシグナルで通知する
- Ctrl+T で最前面表示を切り替える。位置・サイズはモニターに対する比率で設定に保存する
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QMoveEvent, QPixmap, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)

from ptcgl.config import AppConfig


class MainWindow(QMainWindow):
    """日本語カード画像を表示する常時最前面ウィンドウ。"""

    candidate_selected = Signal(object)  # JapaneseCard

    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__()
        self.config = config or AppConfig()
        self._pixmap: QPixmap | None = None
        self._match = None

        self.setWindowTitle("ptcgl")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.config.overlay.always_on_top)

        self._image_label = QLabel("PTCGL のカードを待機中...")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(120, 160)
        self._name_label = QLabel("")
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label.setWordWrap(True)
        self._status_label = QLabel("")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet("color: #888;")
        self._candidates = QListWidget()
        self._candidates.setMaximumHeight(120)
        self._candidates.hide()
        self._candidates.itemClicked.connect(self._on_candidate_clicked)

        layout = QVBoxLayout()
        layout.addWidget(self._image_label, stretch=1)
        layout.addWidget(self._name_label)
        layout.addWidget(self._status_label)
        layout.addWidget(self._candidates)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        shortcut = QShortcut(QKeySequence("Ctrl+T"), self)
        shortcut.activated.connect(self.toggle_always_on_top)
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
        status = match.reason
        if match.best is not None and not match.same_art:
            status = "別イラスト（要確認）: " + status
        self._status_label.setText(status)

        self._candidates.clear()
        if len(match.candidates) > 1:
            for card in match.candidates:
                item = QListWidgetItem(card.label or str(card.ref))
                item.setData(Qt.ItemDataRole.UserRole, card)
                self._candidates.addItem(item)
            self._candidates.show()
        else:
            self._candidates.hide()

    def show_message(self, text: str) -> None:
        self._status_label.setText(text)

    def _on_candidate_clicked(self, item: QListWidgetItem) -> None:
        card = item.data(Qt.ItemDataRole.UserRole)
        if card.image_path:
            self.show_card(card.label, card.image_path)
        self.candidate_selected.emit(card)

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

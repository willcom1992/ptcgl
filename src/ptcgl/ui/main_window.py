"""メインウィンドウ。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget


class MainWindow(QMainWindow):
    """日本語カード画像を表示する常時最前面ウィンドウ。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ptcgl")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(420, 620)

        self._image_label = QLabel("PTCGL のカードを待機中...")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._name_label = QLabel("")
        self._name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout()
        layout.addWidget(self._image_label, stretch=1)
        layout.addWidget(self._name_label)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def show_card(self, name_ja: str, image_path: Path) -> None:
        """日本語カードの名前と画像を表示する。"""
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self._image_label.setText(f"画像を読み込めません: {image_path}")
        else:
            scaled = pixmap.scaled(
                self._image_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._image_label.setPixmap(scaled)
        self._name_label.setText(name_ja)

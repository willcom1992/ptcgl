"""QApplication の起動。"""

from __future__ import annotations

import sys

from ptcgl.config import AppConfig, load_config, save_config


def _create_app(argv: list[str]):  # noqa: ANN202
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from ptcgl.capture.window import ensure_dpi_aware

    ensure_dpi_aware()
    # 物理ピクセルと論理ピクセルの変換を Qt の devicePixelRatio に任せ、丸めない
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("ptcgl")
    return app


def show_match_window(match, cfg: AppConfig | None = None) -> int:  # noqa: ANN001
    """照合結果をウィンドウに表示してイベントループを回す（CLI 用）。"""
    from ptcgl.ui.main_window import MainWindow

    cfg = cfg or load_config()
    app = _create_app(sys.argv)
    window = MainWindow(cfg)
    window.show_match(match)
    window.show()
    code = app.exec()
    save_config(window.config)
    return code


def main(argv: list[str] | None = None) -> int:
    """アプリケーションを起動し、終了コードを返す。"""
    from ptcgl.ui.main_window import MainWindow

    cfg = load_config()
    app = _create_app(argv if argv is not None else sys.argv)
    window = MainWindow(cfg)
    window.show()
    code = app.exec()
    save_config(window.config)
    return code

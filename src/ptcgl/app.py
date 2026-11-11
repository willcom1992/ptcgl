"""QApplication の起動。"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """アプリケーションを起動し、終了コードを返す。"""
    from PySide6.QtWidgets import QApplication

    from ptcgl.ui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("ptcgl")
    window = MainWindow()
    window.show()
    return app.exec()

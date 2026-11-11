"""PTCGL ウィンドウの検出（pywin32）。"""

from __future__ import annotations

PTCGL_WINDOW_TITLE = "Pokémon TCG Live"


def find_ptcgl_window() -> tuple[int, int, int, int] | None:
    """PTCGL ウィンドウの矩形 (left, top, width, height) を返す。見つからなければ None。"""
    try:
        import win32gui
    except ImportError:
        return None

    hwnd = win32gui.FindWindow(None, PTCGL_WINDOW_TITLE)
    if not hwnd:
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left, top, right - left, bottom - top

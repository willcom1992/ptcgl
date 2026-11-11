"""PTCGL ウィンドウの検出（Win32 API）。

矩形は DWM の拡張フレーム境界を物理ピクセルで取得する。DPI はウィンドウごとに取得し、
モニター間で倍率が異なる環境でも座標がずれないようにする。
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass

PTCGL_WINDOW_TITLE = "Pokémon TCG Live"
PTCGL_PROCESS_NAME = "Pokemon TCG Live.exe"

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_dpi_awareness_set = False


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process_name: str
    rect: tuple[int, int, int, int]  # (left, top, width, height) 物理ピクセル
    client_rect: tuple[int, int, int, int]  # クライアント領域（スクリーン座標、物理ピクセル）
    dpi: int

    @property
    def scale(self) -> float:
        return self.dpi / 96.0


def ensure_dpi_aware() -> None:
    """プロセスを Per-Monitor DPI 対応にする（座標を物理ピクセルで扱うため）。

    Qt を使う場合は QApplication 生成前に呼ぶ。失敗しても致命的ではない。
    """
    global _dpi_awareness_set
    if _dpi_awareness_set or sys.platform != "win32":
        return
    _dpi_awareness_set = True
    user32 = ctypes.windll.user32
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()


def _window_dpi(hwnd: int) -> int:
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(wintypes.HWND(hwnd))
        return int(dpi) or 96
    except (AttributeError, OSError):
        return 96


def _frame_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(_DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr == 0:
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except (AttributeError, OSError):
        pass
    import win32gui

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return left, top, right - left, bottom - top


def _client_rect(hwnd: int) -> tuple[int, int, int, int]:
    import win32gui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    sx, sy = win32gui.ClientToScreen(hwnd, (left, top))
    return sx, sy, right - left, bottom - top


def _process_name(hwnd: int) -> str:
    try:
        import win32api
        import win32con
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
        return path.replace("\\", "/").rsplit("/", 1)[-1]
    except Exception:
        return ""


def get_window_info(hwnd: int) -> WindowInfo:
    import win32gui

    return WindowInfo(
        hwnd=hwnd,
        title=win32gui.GetWindowText(hwnd),
        process_name=_process_name(hwnd),
        rect=_frame_rect(hwnd),
        client_rect=_client_rect(hwnd),
        dpi=_window_dpi(hwnd),
    )


def list_windows() -> list[WindowInfo]:
    """タイトルを持つ可視トップレベルウィンドウを列挙する。"""
    if sys.platform != "win32":
        return []
    import win32gui

    ensure_dpi_aware()
    result: list[WindowInfo] = []

    def _enum(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return True
        info = get_window_info(hwnd)
        if info.rect[2] > 0 and info.rect[3] > 0:
            result.append(info)
        return True

    win32gui.EnumWindows(_enum, None)
    return result


def find_window(
    title_pattern: str | None = None, process_name: str | None = None
) -> WindowInfo | None:
    """タイトル（正規表現・大文字小文字無視）またはプロセス名でウィンドウを探す。"""
    pattern = None
    if title_pattern:
        try:
            pattern = re.compile(title_pattern, re.IGNORECASE)
        except re.error:  # 設定に不正な正規表現が入っていてもリテラルとして扱う
            pattern = re.compile(re.escape(title_pattern), re.IGNORECASE)
    for info in list_windows():
        if process_name and info.process_name.lower() == process_name.lower():
            return info
        if pattern and pattern.search(info.title):
            return info
    return None


def find_ptcgl_window() -> tuple[int, int, int, int] | None:
    """PTCGL ウィンドウの矩形 (left, top, width, height) を返す。見つからなければ None。"""
    info = find_window(title_pattern=re.escape(PTCGL_WINDOW_TITLE), process_name=PTCGL_PROCESS_NAME)
    return info.client_rect if info else None


def is_window_alive(hwnd: int) -> bool:
    """ウィンドウハンドルがまだ有効で可視か。"""
    if sys.platform != "win32":
        return False
    import win32gui

    try:
        return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def is_window_minimized(hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    import win32gui

    try:
        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False


_WDA_NONE = 0x0
_WDA_EXCLUDEFROMCAPTURE = 0x11


def set_exclude_from_capture(hwnd: int, enabled: bool = True) -> bool:
    """自分のウィンドウを画面キャプチャから除外する（Windows 10 2004 以降）。

    表示ウィンドウが PTCGL に重なっていても、キャプチャしたフレームに写り込まないようにする。
    成功すれば True。非対応環境では False を返し、呼び出し側は重ならない配置に頼る。
    """
    if sys.platform != "win32":
        return False
    try:
        affinity = _WDA_EXCLUDEFROMCAPTURE if enabled else _WDA_NONE
        ok = ctypes.windll.user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), affinity)
        return bool(ok)
    except (AttributeError, OSError):
        return False

"""QApplication の起動。"""

from __future__ import annotations

import logging
import sys
import threading

from ptcgl.config import AppConfig, load_config, save_config

log = logging.getLogger(__name__)


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
    icon = load_app_icon()
    if icon is not None and isinstance(app, QApplication):
        app.setWindowIcon(icon)  # タスクバー・タイトルバー・Alt+Tab に出るアイコン
    return app


def load_app_icon():  # noqa: ANN201 - QIcon | None（Qt の import を遅らせる）
    """同梱の icon.png からアイコンを作る。無くても起動は続ける。"""
    from importlib import resources

    from PySide6.QtGui import QIcon, QPixmap

    try:
        data = resources.files("ptcgl.data").joinpath("icon.png").read_bytes()
    except (FileNotFoundError, OSError) as exc:
        log.warning("アイコンを読み込めません: %s", exc)
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        return None
    return QIcon(pixmap)


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


def configure_file_logging() -> None:
    """常駐モードの動作ログをユーザーのデータディレクトリに残す（遅い・認識しない原因の調査用）。"""
    from logging.handlers import RotatingFileHandler

    from ptcgl.config import data_dir

    try:
        path = data_dir() / "ptcgl.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
    except OSError as exc:  # ログが書けなくても起動は続ける
        log.warning("ログファイルを開けません: %s", exc)


def _log_uncaught(exc_type, exc, tb) -> None:  # noqa: ANN001
    """未捕捉例外をログファイルに残す（コンソール無しの exe では標準エラーが見えないため）。"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log.critical("未捕捉の例外で終了します", exc_info=(exc_type, exc, tb))
    sys.__excepthook__(exc_type, exc, tb)


def start_sets_refresh(http) -> threading.Thread:  # noqa: ANN001
    """最新のセット表を裏で取得する（新弾のたびにアプリを配り直さなくて済むように）。

    取得が終わるまで起動も認識も待たせない。失敗しても同梱の表で動き続ける。
    """
    from ptcgl.data.sets import refresh_en_sets

    def run() -> None:
        try:
            refresh_en_sets(http)
        except Exception:  # 終了処理と重なった場合など。セット表の更新失敗でアプリを落とさない
            log.exception("セット表の更新に失敗")

    thread = threading.Thread(target=run, name="sets-refresh", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    """常駐モードでアプリケーションを起動し、終了コードを返す。

    PTCGL のウィンドウを定期的にキャプチャし、拡大表示されたカードを認識して表示する。
    認識はワーカースレッドで行い、UI スレッドはブロックしない。
    """
    from ptcgl.capture.screen import monitor_fingerprint
    from ptcgl.cli import build_services
    from ptcgl.ui.main_window import MainWindow
    from ptcgl.ui.worker import RecognitionWorker, WorkerThread

    configure_file_logging()
    sys.excepthook = _log_uncaught
    cfg = load_config()
    app = _create_app(argv if argv is not None else sys.argv)
    log.info(
        "起動: interval=%dms ocr_threads=%s", cfg.capture.interval_ms, cfg.ocr_threads or "auto"
    )
    try:
        if cfg.check_monitor_change(monitor_fingerprint()):
            log.info("モニター構成が変わりました。表示位置は画面内に補正されます")
    except Exception as exc:  # モニター列挙に失敗しても起動は続ける
        log.warning("モニター構成を取得できません: %s", exc)

    pipeline, http = build_services(cfg)
    sets_refresh = start_sets_refresh(http)
    worker = RecognitionWorker(pipeline, cfg)
    thread = WorkerThread(worker)

    window = MainWindow(cfg)
    worker.status_changed.connect(window.show_watch_status)
    worker.result_ready.connect(window.show_result)
    worker.window_changed.connect(window.show_window_info)
    window.confirm_requested.connect(worker.confirm)
    window.pause_toggled.connect(worker.set_paused)

    window.show()
    thread.start()
    try:
        code = app.exec()
    finally:
        save_config(window.config)
        sets_refresh.join(timeout=1.0)
        if thread.shutdown() and not sets_refresh.is_alive():
            http.close()
        else:
            # まだ動いているスレッドがある間に共有リソースを閉じると例外になるため、閉じずに終了する
            log.warning("通信中のスレッドが終了しないため HTTP クライアントを閉じずに終了します")
    return code

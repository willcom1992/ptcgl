# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 仕様（onedir）。

    pyinstaller packaging/ptcgl.spec

RapidOCR の ONNX モデル・設定ファイルと、パッケージ同梱の en_sets.json・icon.png を含める。
exe のアイコンは packaging/ptcgl.ico（scripts/build_icon.py で生成）。
初回起動時に外部からモデルをダウンロードしない構成にする。

exe とフォルダの名前は「PTCGL 日本語カードビューア」。公式クライアント（Pokémon TCG Live）と
紛らわしい "ptcgl.exe" を避け、ウィンドウ題名（ui/main_window.py の WINDOW_TITLE）と揃える。
バージョン情報（説明・製品名・バージョン）は exe のプロパティとタスクマネージャーに出る。
"""

import os
import pathlib
import re

import rapidfuzz
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

APP_DISPLAY_NAME = "PTCGL 日本語カードビューア"
_SRC = pathlib.Path(SPECPATH).parent / "src" / "ptcgl"


def _read_version() -> str:
    """src/ptcgl/__init__.py の __version__ を読む（パッケージを import せずに済ませる）。"""
    text = (_SRC / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError("__version__ が見つかりません")
    return m.group(1)


def _version_info(version: str) -> VSVersionInfo:
    """Windows のバージョンリソース。"0.1.0" → (0, 1, 0, 0)。"""
    nums = [int(x) for x in re.findall(r"\d+", version)][:4]
    nums += [0] * (4 - len(nums))
    strings = {
        "ProductName": APP_DISPLAY_NAME,
        "FileDescription": APP_DISPLAY_NAME,
        "ProductVersion": version,
        "FileVersion": version,
        "OriginalFilename": f"{APP_DISPLAY_NAME}.exe",
        "InternalName": "ptcgl",
        "CompanyName": "willcom1992",
        "LegalCopyright": "https://github.com/willcom1992/ptcgl",
    }
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums)),
        kids=[
            StringFileInfo(
                [StringTable("041104B0", [StringStruct(k, v) for k, v in strings.items()])]
            ),
            VarFileInfo([VarStruct("Translation", [0x0411, 0x04B0])]),
        ],
    )


APP_VERSION = _read_version()

datas = []
datas += collect_data_files("rapidocr_onnxruntime")  # models/*.onnx, config.yaml
datas += collect_data_files("ptcgl", includes=["data/*.json", "data/*.png"])

binaries = collect_dynamic_libs("onnxruntime")
# rapidfuzz の C++ 拡張は site-packages/rapidfuzz.libs/ の DLL に依存するが、PyInstaller の
# フックが無いため同梱されない（純 Python 実装に黙って落ちる）。明示的に集める
_rf_libs = pathlib.Path(rapidfuzz.__file__).parent.parent / "rapidfuzz.libs"
binaries += [(str(p), "rapidfuzz.libs") for p in _rf_libs.glob("*.dll")]

hiddenimports = []
hiddenimports += collect_submodules("rapidocr_onnxruntime")
hiddenimports += ["win32gui", "win32process", "win32api", "win32con", "pywintypes"]

a = Analysis(
    ["../src/ptcgl/__main__.py"],
    pathex=[os.path.join(SPECPATH, "..", "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "torch", "torchvision", "matplotlib", "IPython",
        # 使わない Qt モジュール（QML/Quick/Network/Pdf/WebEngine 等）。DLL は下の _drop で落とす
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtNetwork",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebChannel", "PySide6.QtMultimedia", "PySide6.QtOpenGLWidgets", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtPrintSupport",
        # Pillow の AVIF プラグイン（7.5 MB）。rapidocr は PIL.Image しか使わない
        "PIL._avif",
    ],
    noarchive=False,
)

# 配布サイズ削減: アプリが使わないバイナリ・データを Analysis 結果から取り除く。
# ここで落とすものは実行時に参照されない（検証: identify --set/--number と GUI 起動）。
_DROP_PATTERNS = (
    "opengl32sw.dll",  # ソフトウェア OpenGL（QtWidgets のみの GUI では不要）
    "qt6quick", "qt6qml", "qt6virtualkeyboard", "qtvirtualkeyboardplugin",  # QML/仮想キーボード
    "qt6network", "qnetworklistmanager", "plugins/tls/",  # Qt の通信層（HTTP は httpx を使う）
    "qt6pdf", "qpdf.dll",  # PDF
    "qt6svg", "qsvg.dll", "qsvgicon.dll",  # SVG（PNG しか表示しない）
    "pyside6/translations/",  # Qt の翻訳（ダイアログも独自文言）
    "opencv_videoio_ffmpeg",  # 動画入出力（headless でも同梱される）
    "pil/_avif",  # Pillow の AVIF プラグイン
)


def _drop(entries):
    kept = []
    for entry in entries:
        name = entry[0].replace("\\", "/").lower()  # パス区切りを / に揃えて照合
        if any(pat in name for pat in _DROP_PATTERNS):
            continue
        kept.append(entry)
    return kept


a.binaries = _drop(a.binaries)
a.datas = _drop(a.datas)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_DISPLAY_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 動作ログは user_data_dir/ptcgl.log に残す（app.configure_file_logging）
    icon=os.path.join(SPECPATH, "ptcgl.ico"),
    version=_version_info(APP_VERSION),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_DISPLAY_NAME,
)

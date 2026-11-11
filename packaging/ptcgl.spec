# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 仕様（onedir）。

    pyinstaller packaging/ptcgl.spec

RapidOCR の ONNX モデル・設定ファイルと、パッケージ同梱の en_sets.json を含める。
初回起動時に外部からモデルをダウンロードしない構成にする。
"""

import os
import pathlib

import rapidfuzz
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = []
datas += collect_data_files("rapidocr_onnxruntime")  # models/*.onnx, config.yaml
datas += collect_data_files("ptcgl", includes=["data/*.json"])

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
    excludes=["tkinter", "torch", "torchvision", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ptcgl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 動作ログは user_data_dir/ptcgl.log に残す（app.configure_file_logging）
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="ptcgl",
)

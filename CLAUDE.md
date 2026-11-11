# ptcgl — PTCGL 英語カード → 日本語カード画像ビューア

## プロジェクト概要
Pokémon TCG Live（PTCGL）の画面に表示されている **英語カード** を自動認識し、
対応する **日本語版ポケモンカードの画像** を表示する Windows デスクトップアプリ。

詳細な要件は [docs/spec.md](docs/spec.md)、設計は [docs/architecture.md](docs/architecture.md) を参照。

## 技術スタック
- Python 3.10（`C:\Users\takum\AppData\Local\Programs\Python\Python310\python.exe`）
- GUI: PySide6（Qt）
- 画面キャプチャ: mss
- 画像処理: OpenCV, Pillow, numpy
- OCR: rapidocr-onnxruntime（Tesseract 等の外部インストール不要）
- HTTP: httpx（カードデータ取得）
- テスト: pytest / Lint & Format: ruff / 型チェック: mypy

## よく使うコマンド
```powershell
# 初回セットアップ
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# アプリ起動
python -m ptcgl

# テスト・Lint・型チェック
pytest
ruff check . && ruff format --check .
mypy src
```

## ディレクトリ構成
```
src/ptcgl/
  __main__.py        # エントリポイント（python -m ptcgl）
  app.py             # QApplication の起動
  config.py          # 設定（キャプチャ間隔、キャッシュパス等）
  capture/           # 画面キャプチャ（mss）・PTCGL ウィンドウ検出
  recognition/       # カード認識（OCR / 画像マッチング）
  data/              # カードDB・日本語画像の取得とキャッシュ
  ui/                # PySide6 のウィジェット
tests/               # pytest
docs/                # 仕様・設計
assets/samples/      # 認識テスト用のサンプル画像（スクリーンショット）
```

## コーディング規約
- Python 3.10 互換。`X | None` 記法を使うファイルには `from __future__ import annotations` を付ける
- すべての公開関数に型ヒントを付ける
- ruff の設定は pyproject.toml に従う（行長 100）
- UI スレッドをブロックしない。キャプチャ・OCR・HTTP は `QThread` / ワーカーで実行する
- ネットワーク取得した画像・JSON は `data/cache.py` 経由でローカルキャッシュする（外部 API に負荷をかけない）
- コミットメッセージ・コメント・ドキュメントは日本語でよい。識別子は英語

## 注意事項
- `.venv/`, `cache/`, スクリーンショット画像（assets/samples 以外）はコミットしない
- 画面キャプチャは PTCGL のウィンドウが表示されている前提。ウィンドウ検出は `capture/window.py`
- 日本語カードデータの出典は [docs/spec.md](docs/spec.md) の「データソース」を参照。利用規約を確認してから新しいソースを追加すること
- 変更後は必ず `ruff check .` と `pytest` を実行してから完了報告する

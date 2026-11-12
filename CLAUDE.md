# ptcgl — PTCGL 英語カード → 日本語カード画像ビューア

## プロジェクト概要
Pokémon TCG Live（PTCGL）の画面に表示されている **英語カード** を自動認識し、
対応する **日本語版ポケモンカードの画像** を表示する Windows デスクトップアプリ。

詳細な要件は [docs/spec.md](docs/spec.md)、設計は [docs/architecture.md](docs/architecture.md)、使い方は [docs/usage.md](docs/usage.md) を参照。

## 技術スタック
- Python 3.12（`.venv` は `py -3.12 -m venv .venv` で作成。コードは 3.10 互換の記法を保つ）
- GUI: PySide6（Qt）
- 画面キャプチャ: mss / ウィンドウ検出: pywin32 + ctypes（DWM, DPI）
- 画像処理: OpenCV, Pillow, numpy
- OCR: rapidocr-onnxruntime（Tesseract 等の外部インストール不要）
- HTTP: httpx + BeautifulSoup（Limitless の HTML 解析）/ 名前照合: rapidfuzz
- 設定・データの保存先: platformdirs（固定パス禁止）
- テスト: pytest / Lint & Format: ruff / 型チェック: mypy / 配布: PyInstaller

## よく使うコマンド
```powershell
# 初回セットアップ
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# アプリ起動（GUI）
python -m ptcgl

# 検証用 CLI
python -m ptcgl identify --set SVI --number 1          # OCR なしで照合と表示
python -m ptcgl -v identify --image shot.png           # 画像から検出→OCR→照合
python -m ptcgl windows                                # ウィンドウ一覧（DPI・矩形）
python -m ptcgl capture --window "TCG Live" --out shot.png
python scripts/evaluate.py assets/samples --out docs/validation-results.md
python scripts/analyze_log.py --since "2026-11-12 10:00"   # 常駐ログの所要時間を経路別に集計
python scripts/build_en_sets.py                        # セット表の再生成（main に push すると配布済みアプリにも届く）
python scripts/build_icon.py                           # アプリアイコン（packaging/ptcgl.ico, data/icon.png）の再生成

# テスト・Lint・型チェック
pytest
ruff check . && ruff format --check .
mypy src

# 配布用ビルド（onedir）
pyinstaller packaging/ptcgl.spec
```

## ディレクトリ構成
```
src/ptcgl/
  __main__.py        # エントリポイント（引数なし=GUI、サブコマンド=CLI）
  app.py             # QApplication の起動（DPI 設定を含む）
  cli.py             # identify / windows / capture / config-path
  config.py          # 設定（JSON, platformdirs）。領域・位置はすべて比率で保存
  pipeline.py        # 検出 → OCR → 英語カード特定 → 日本語照合 の接続
  watch.py           # 常駐モードの監視ロジック（画面変化の判定。Qt 非依存）
  capture/           # screen.py（mss, 物理px）・window.py（ウィンドウ列挙, DWM 矩形, DPI）
  imageio.py         # 非 ASCII パス対応の画像読み書き（cv2.imread は日本語パスを扱えない）
  recognition/       # anchor.py（OCR アンカーでカード領域推定）・ocr.py（RapidOCR）・card.py（OCR 文字列解析）
                     # detector.py（輪郭検出・手動領域。Protocol）
  data/              # limitless.py（HTML 解析）・cards.py（英→日照合）・mapping.py（SQLite）
                     # sets.py + en_sets.json（PTCGL セットコードと番号の分母）・cache.py
  ui/                # PySide6 のウィジェット（main_window.py）とワーカースレッド（worker.py）
tests/               # pytest（fixtures/html に Limitless の保存ページ）
scripts/             # build_en_sets.py, build_icon.py, evaluate.py, detect_spike.py
packaging/           # PyInstaller spec, ptcgl.ico
docs/                # 仕様・設計・検証結果
assets/samples/      # 認識テスト用のスクリーンショットと labels.csv
```

## 設計原則（他の PC でも動かすため）
- 絶対パス・開発機固有の値をコードに書かない。保存先は `config.py` の platformdirs 経由
- 画面座標は mss の物理ピクセルで扱い、保存する領域・位置は対象ウィンドウ／モニターに対する比率
- `recognition/detector.py`（領域検出）と `recognition/ocr.py`（文字認識）は互いに依存しない
- 外部サイトへのアクセスは `data/cache.py` 経由のみ（1 req/s、永続キャッシュ、プリフェッチ禁止）
- セット表（`en_sets.json`）は起動時に main の最新版を 1 日 1 回取得する（`data/sets.py` の `refresh_en_sets()`）。形式を変えるときは配布済みアプリが読めることを保つ

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

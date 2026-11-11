# 設計

## 処理パイプライン
```
[capture]  mss でウィンドウ領域をキャプチャ
    │  numpy 配列 (BGR)
    ▼
[recognition]  前フレームとの差分チェック → 変化があれば OCR
    │  RecognizedCard(name_en, set_code, number, confidence)
    ▼
[data]  英→日 対応付け → 日本語カードの画像 URL を解決 → キャッシュから取得
    │  JapaneseCard(name_ja, image_path, ...)
    ▼
[ui]  CardViewer ウィンドウに表示
```

## モジュール

| モジュール | 役割 | 主なクラス・関数 |
| --- | --- | --- |
| `capture/window.py` | PTCGL ウィンドウのハンドル・矩形取得（pywin32） | `find_ptcgl_window()` |
| `capture/screen.py` | 指定矩形のキャプチャ（mss） | `ScreenCapture.grab()` |
| `recognition/ocr.py` | RapidOCR によるテキスト抽出 | `OcrEngine.read()` |
| `recognition/card.py` | OCR 結果からカード名・セット番号を抽出 | `parse_set_number()` |
| `data/cache.py` | HTTP 取得のディスクキャッシュ | `CachedHttpClient` |
| `data/cards.py` | カードDB の検索と英→日対応付け | `CardRepository.find_japanese()` |
| `ui/main_window.py` | メインウィンドウ（カード画像・履歴） | `MainWindow` |
| `ui/worker.py` | キャプチャ→認識→取得のバックグラウンドループ | `RecognitionWorker(QThread)` |

## スレッド構成
- **UI スレッド**: Qt イベントループのみ
- **RecognitionWorker (QThread)**: キャプチャ・差分判定・OCR・データ取得を一つのループで実行し、
  結果を `card_recognized(JapaneseCard)` シグナルで UI へ通知する

## 設定（`config.py`）
`%APPDATA%\ptcgl\config.toml` に保存。主な項目:
- `capture.interval_ms`（既定 1000）
- `capture.region`（`auto` または手動矩形）
- `cache.dir`（既定 `%LOCALAPPDATA%\ptcgl\cache`）

## 決定事項ログ
| 日付 | 決定 | 理由 |
| --- | --- | --- |
| 2026-11-11 | 言語は Python 3.10 + PySide6 | 画面キャプチャ・OCR・OpenCV のライブラリが揃い、Windows GUI も作れる |
| 2026-11-11 | OCR は rapidocr-onnxruntime | pip だけで導入でき、Tesseract のインストールが不要 |

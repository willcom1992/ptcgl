# 設計

## 処理パイプライン
```
[capture]  window.py で PTCGL ウィンドウの矩形・DPI を取得 → screen.py (mss) で物理 px キャプチャ
    │  numpy 配列 (BGR)
    ▼
[recognition/ocr + anchor]  パス 1: 縮小フレーム全体を OCR → 番号行「MEG EN 076/132」または
    │  HP バッジ（最大の数字）をアンカーにしてカード領域（比率座標 CardBox）を推定
    │  （設定で手動領域が指定されていれば recognition/detector の ManualRegionDetector を使う）
    ▼
[recognition/ocr + card]  パス 2: 領域を元解像度で OCR → セットコード / 番号 / 分母 / 名前 / HP
    │  名前は番号行と左端が揃う行の中で最も大きい文字の行（隣のカードの文字を拾わない）
    │  RecognizedCard(name_en, set_code?, number, total, hp)
    ▼
[pipeline.resolve_en_ref]  分母 TTT → en_sets.json でセット特定（衝突時は名前の類似度で解決）
    │  CardRef("en", "SVI", "1")
    ▼
[data/cards]  Limitless 英語ページの「JP. Prints」→ 日本語候補 → イラストレーター一致で同一絵柄を選ぶ
    │  MatchResult(best: JapaneseCard, same_art, confidence, candidates)
    ▼
[data/cache]  日本語画像を CDN から取得しローカル保存 → [ui] MainWindow に表示（候補一覧つき）
```

## モジュール

| モジュール | 役割 | 主なクラス・関数 |
| --- | --- | --- |
| `capture/window.py` | ウィンドウ列挙、DWM 拡張フレーム矩形、ウィンドウ DPI、DPI 対応宣言 | `list_windows()`, `find_window()`, `ensure_dpi_aware()` |
| `capture/screen.py` | 指定矩形のキャプチャ（物理 px）、モニター構成のフィンガープリント | `ScreenCapture.grab()`, `monitor_fingerprint()` |
| `recognition/detector.py` | カード領域検出（Protocol）。輪郭検出実装と手動領域実装 | `CardDetector`, `ContourCardDetector`, `ManualRegionDetector`, `CardBox` |
| `recognition/ocr.py` | RapidOCR によるテキスト抽出 | `OcrEngine.read()` |
| `recognition/card.py` | OCR 結果からカード名・番号・分母・HP を抽出 | `extract_card_info()`, `parse_number_total()` |
| `data/sets.py` | PTCGL セットコードと番号の分母の表（`en_sets.json`） | `sets_by_official_total()`, `find_set()` |
| `data/limitless.py` | Limitless カードページの HTML 解析 | `parse_card_page()`, `CardRef`, `CardPage` |
| `data/cards.py` | 英→日の照合（同一イラスト優先、候補の順位付け） | `CardRepository.find_japanese()` |
| `data/mapping.py` | 確定した対応付けの保存（SQLite） | `MappingStore` |
| `data/cache.py` | HTTP 取得のディスクキャッシュ・間隔制御 | `CachedHttpClient` |
| `pipeline.py` | 上記の接続。CLI/GUI から呼ぶ | `Pipeline.identify_frame()`, `identify_ref()` |
| `config.py` | 設定の読み書き（JSON, platformdirs） | `AppConfig`, `load_config()`, `save_config()` |
| `ui/main_window.py` | 画像表示・候補選択・最前面切替・位置の比率保存 | `MainWindow.show_match()` |

## PTCGL の拡大表示の種類（2026-11-11 のスクリーンショットで確認）
| 表示 | 出る場面 | 読めるもの | 特定方法 |
| --- | --- | --- | --- |
| フルカード表示 | 手札ホバー、捨て札一覧、場に出した直後、スタジアム | 印刷どおり。下部に `MEG EN 076/132`（セットコード・EN・番号/分母） | 番号行アンカー → セットコード or 分母で特定 |
| バトル場の詳細表示 | バトル場のポケモンを選択 | カードが少し傾く。番号は読める。右に名前・HP のパネル | 同上 |
| 対戦中の詳細表示 | 自分・相手のポケモンを選択 | 名前バー・大きな HP バッジ・ワザ説明パネルが重なり、**番号は隠れる** | HP バッジアンカー → 名前・HP・ワザ名で Limitless 検索（`q=Riolu hp:80 text:Accelerating_Stab unique:cards`） |
| 基本エネルギー | 手札ホバー | `MEE EN 006`（分母なし）が小さく、OCR で分断される | 未対応（Phase 1） |

## 英語カードの特定
- PTCGL の拡大カードには英語版の印刷どおり `番号/分母`（例 `081/182`）が入る前提。
- セットコードが読めればそれを優先する。規制マークが前に付いた `ODRI` のような読みは既知コードの部分文字列で補正する。
  分母 → セット は `en_sets.json`（TCGdex の `cardCount.official` と略号）で引く。
- 分母が衝突するセット（182: DRI/PAR、86: BLK/WHT/CRI、198: SVI/CRE など）は、
  候補ごとに Limitless の英語ページを引いてカード名を OCR 結果と rapidfuzz で比較する。
- Trainer Gallery 等のサブセットは `TG05/TG30` のように接頭辞で区別する。

## 日本語版の照合
- Limitless 英語ページの「JP. Prints」にある日本語印刷をすべて候補にする。
- 候補の優先順: 通常拡張パック（`SV1S`, `SV10`, `M2a` …）→ プロモ／構築済み、同枠内は番号順。
- 順に日本語ページを取得し、イラストレーターが英語版と一致した最初の候補を「同一絵柄」として採用する。
- 一致が無い場合は信頼度 0.5 で別イラスト候補を返し、UI では「別イラスト（要確認）」と表示する。
  ユーザーが候補を選ぶと `CardRepository.confirm()` で `MappingStore` に `source="manual"` で保存され、
  以後はそれを優先する（UI の `candidate_selected` シグナルと `confirm()` の接続は Phase 1 の
  認識ワーカー実装時に行う。Phase 0 の GUI は CLI から渡された結果を表示するだけ）。
- 1 枚につき最大で候補数ぶんのページ取得が発生するが、すべてディスクキャッシュされる。

## 環境非依存のための決まり
- 座標: キャプチャは物理ピクセル。設定に保存する領域・位置は、対象ウィンドウのクライアント矩形／
  モニター矩形に対する比率 `(rx, ry, rw, rh)`。
- DPI: `ensure_dpi_aware()` で Per-Monitor V2 を宣言し、Qt は `PassThrough` で論理↔物理変換する。
- パス: `platformdirs` の `user_config_dir("ptcgl")`, `user_data_dir("ptcgl")` のみ。
- モニター構成: `monitor_fingerprint()` を設定に保存し、変化したら `needs_reconfirm` を立てる。
- 同梱リソース（`en_sets.json`, RapidOCR モデル）は `importlib.resources` / PyInstaller の datas。

## スレッド構成
- **UI スレッド**: Qt イベントループのみ
- **RecognitionWorker (QThread)**（Phase 1）: キャプチャ・差分判定・検出・OCR・データ取得を
  一つのループで実行し、結果を `card_recognized(MatchResult)` シグナルで UI へ通知する

## 設定（`config.py`）
`%LOCALAPPDATA%\ptcgl\config.json`（platformdirs の `user_config_dir("ptcgl", appauthor=False)`）。
`cache_dir` / `images_dir` / `db_path` は既定（`%LOCALAPPDATA%\ptcgl\` 配下）のままなら書き出さない。主な項目:
- `target_window.title_pattern` / `process_name`
- `capture.detection_mode`（`auto` | `manual`）, `capture.card_region_ratio`
- `overlay.monitor_name`, `overlay.ratio_pos`, `overlay.ratio_size`, `overlay.always_on_top`
- `monitor_fingerprint`, `needs_reconfirm`
- `cache_dir`, `images_dir`, `db_path`（既定は user_data_dir 配下）

## 決定事項ログ
| 日付 | 決定 | 理由 |
| --- | --- | --- |
| 2026-11-11 | 言語は Python + PySide6 | 画面キャプチャ・OCR・OpenCV のライブラリが揃い、Windows GUI も作れる |
| 2026-11-11 | OCR は rapidocr-onnxruntime | pip だけで導入でき、Tesseract のインストールが不要 |
| 2026-11-11 | 実行環境は Python 3.12（コードは 3.10 互換） | 新規導入。依存ライブラリはすべて 3.12 対応 |
| 2026-11-11 | 英→日対応付けと日本語画像は Limitless TCG を主体にする | TCGdex には PTCGL 対応表も英日共通 ID も無く、`variantId` は仕上げ種別の ID で照合に使えない。日本語画像の収録率も約 20%（SV10 は 1〜98 のみ、M2a は 0）。Limitless は各カードページに Int./JP. Prints の相互リンクがあり、日本語画像（pokemon-card.com 由来）もほぼ全カード分ある |
| 2026-11-11 | TCGdex は英語セット表（略号・分母）の生成にだけ使う | `/v2/en/sets/{id}` の `abbreviation` と `cardCount.official` が PTCGL の番号表記と一致する |
| 2026-11-11 | 同一イラスト判定はイラストレーター名の一致 | Limitless のページに絵柄の同一性を示す情報が無い。別イラストは自動確定しない |
| 2026-11-11 | 設定は JSON（TOML ではない） | 標準ライブラリに TOML の書き出しが無い |
| 2026-11-11 | 画像は個人利用・ローカルキャッシュのみ、再配布しない | pokemon-card.com の規約が複製・配布を禁止している |
| 2026-11-11 | カード領域は画像処理ではなく OCR アンカー方式で推定する | 輪郭検出・勾配・彩度・色相の 4 方式を試したが、PTCGL の発光枠・丸角・盤面の模様でカード矩形を安定して取れなかった（13 枚中 2 枚）。番号行／HP バッジを手がかりにすると 13 枚中 11 枚で領域を得られた |
| 2026-11-11 | 番号が隠れる対戦中の詳細表示は Limitless 検索（名前・HP・ワザ名）で特定する | `unique:cards` で同一テキストのカードに絞れる。返るのは代表印刷なので絵柄は未確認扱い |

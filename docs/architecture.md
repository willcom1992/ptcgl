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
| `recognition/ocr.py` | RapidOCR によるテキスト抽出。スレッド数は自動で 2〜4 に抑え、角度分類は使わない | `OcrEngine.read()`, `default_ocr_threads()` |
| `recognition/card.py` | OCR 結果からカード名・番号・分母・HP を抽出 | `extract_card_info()`, `parse_number_total()` |
| `data/sets.py` | PTCGL セットコードと番号の分母の表（`en_sets.json`）。起動時に最新版を取得して差し替える | `sets_by_official_total()`, `find_set()`, `refresh_en_sets()` |
| `data/limitless.py` | Limitless カードページの HTML 解析 | `parse_card_page()`, `CardRef`, `CardPage` |
| `data/cards.py` | 英→日の照合（同一イラスト優先、候補の順位付け） | `CardRepository.find_japanese()` |
| `data/mapping.py` | 確定した対応付けの保存（SQLite） | `MappingStore` |
| `data/cache.py` | HTTP 取得のディスクキャッシュ・間隔制御 | `CachedHttpClient` |
| `pipeline.py` | 上記の接続。CLI/GUI から呼ぶ。常駐モード用の高速パスもここ | `Pipeline.identify_frame()`, `identify_fast()`, `identify_ref()` |
| `watch.py` | 常駐モードの監視ロジック（Qt 非依存）。画面変化の判定と 1 ステップの実行 | `FrameWatcher`, `WatchSession.step()` |
| `ui/worker.py` | ワーカースレッド。`WatchSession` を定期実行し結果をシグナルで通知 | `RecognitionWorker`, `WorkerThread` |
| `config.py` | 設定の読み書き（JSON, platformdirs） | `AppConfig`, `load_config()`, `save_config()` |
| `ui/main_window.py` | 画像表示・候補選択・最前面切替・一時停止・位置の比率保存・キャプチャ除外 | `MainWindow.show_match()`, `show_result()` |

## PTCGL の拡大表示の種類（2026-11-11 のスクリーンショットで確認）
| 表示 | 出る場面 | 読めるもの | 特定方法 |
| --- | --- | --- | --- |
| フルカード表示 | 手札ホバー、捨て札一覧、場に出した直後、スタジアム | 印刷どおり。下部に `MEG EN 076/132`（セットコード・EN・番号/分母） | 番号行アンカー → セットコード or 分母で特定 |
| バトル場の詳細表示 | バトル場のポケモンを選択 | カードが少し傾く。番号は読める。右に名前・HP のパネル | 同上 |
| 対戦中の詳細表示 | 自分・相手のポケモンを選択 | 名前バー・大きな HP バッジ・ワザ説明パネルが重なり、**番号は隠れる** | HP バッジアンカー → 名前・HP・ワザ名で Limitless 検索（`q=Riolu hp:80 text:Accelerating_Stab unique:cards`） |
| 基本エネルギー | 手札ホバー | `MEE EN 006`（分母なし）が小さく、OCR で分断される | 対象外（翻訳不要のため対応しない） |

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
  ユーザーが候補を選ぶと `MainWindow.confirm_requested` → `RecognitionWorker.confirm()` →
  `CardRepository.confirm()` の経路で `MappingStore` に `source="manual"` で保存され、以後はそれを優先する。
  保存はワーカースレッドで行い、UI スレッドで SQLite やネットワークに触れない。
- 1 枚につき最大で候補数ぶんのページ取得が発生するが、すべてディスクキャッシュされる。

## 環境非依存のための決まり
- 座標: キャプチャは物理ピクセル。設定に保存する領域・位置は、対象ウィンドウのクライアント矩形／
  モニター矩形に対する比率 `(rx, ry, rw, rh)`。
- DPI: `ensure_dpi_aware()` で Per-Monitor V2 を宣言し、Qt は `PassThrough` で論理↔物理変換する。
- パス: `platformdirs` の `user_config_dir("ptcgl")`, `user_data_dir("ptcgl")` のみ。
- モニター構成: `monitor_fingerprint()` を設定に保存し、変化したら `needs_reconfirm` を立てる。
- 同梱リソース（`en_sets.json`, RapidOCR モデル）は `importlib.resources` / PyInstaller の datas。
- セット表の更新: GUI は起動時に別スレッドで `refresh_en_sets()` を呼び、main の `en_sets.json` を
  `CachedHttpClient` 経由で取得する（1 日 1 回、リトライなし、失敗時は前回の取得分 → 同梱版の順に使う）。
  取得した表は保存前に形式とセットコードの文字種を検証し、`updated`（生成日時）が同梱版より新しいときだけ採用する。

## 常駐モードの認識フロー（遅延を抑える仕組み）
1. **変化領域の特定**（`watch.py`）: 幅 320 px の縮小グレースケールで基準フレームとの差分を取り、
   変化した画素の外接矩形を「変化領域」とする。画面の 6 割以上が変わっていれば全画面扱い。
2. **即時認識**: 画面が静止するのを待たず、変化を検知したティック（既定 250 ms 間隔）で認識する。
3. **高速パス**（`Pipeline.identify_fast()`）: 変化領域に対して文字**検出**だけを行い（長辺 960 px に縮小）、
   番号行らしい小さく横長の箱だけを**認識**する。候補は「下にある順」と「小さい順」の上位 6 個ずつを
   合わせた 1 段目、残りの小さな箱 6 個の 2 段目。番号行が読めたらセットコードで英語カードを確定し、
   セットコードが読めない・分母が衝突する場合だけ番号行と左端が揃う上の行（カード名）を追加で読む。
   傾いた箱（バトル場の詳細表示）は射影変換で起こしてから認識する。
   長辺 900 px 未満の小さな領域は先に長辺 1100 px へ拡大する（画面幅の 2 割程度のカードは番号が 8 px しかなく、
   検出が「056/」で途切れる）。全画面で番号行が読めない場合は、2〜3 桁の数字で終わる大きな箱（HP バッジ。
   名前バーと結合していることもある）の右端からカード領域を推定し、大きい順に 2 つまで拡大して読み直す。
4. **フォールバック**: 変化領域で見つからなければ全画面で高速パス、それでも駄目なら通常パイプライン
   （全行認識。番号が隠れる対戦中の詳細表示は名前・HP の検索で特定し、複数件のときだけワザ名で絞る）。
   常駐モードの通常パイプラインでは元解像度での全行読み直し（約 1.2 秒）を行わない。高速パスが先に
   小さな表示を拾う前提で、カードが無い画面での失敗を 3 秒台から 2 秒程度に縮める。
   高速パスの元解像度での再検出も同じ理由で廃止した（常駐ログの集計で約 150 回中 1 回しか成功しなかった）。
5. **失敗時の扱い**: 動いている最中の失敗は基準フレームを更新せず、静止後に再度「変化」として拾う。
   失敗が続けば試行間隔を 1, 2, 4 … ティック（上限 40）と倍々に延ばす。静止画面での失敗は基準にして
   画面が変わるまで再試行しない。静止画面での失敗が続く場合（演出の合間に止まる盤面）は、次に画面が
   変化してから 2, 4, 8 ティック（上限 8 = 既定 2 秒）だけ待ってから試す。
6. **照合結果のメモリキャッシュ**: 英語カードごとの照合結果（`MatchResult`）を Pipeline が 128 件まで
   覚え、同じカードを続けて認識したときに Limitless ページの再解析を省く。候補を手動で選び直したら
   そのカードの分だけ捨てる。

開発機（WQHD）の擬似計測: 番号が見える表示で 0.4〜1.3 秒（高速パスのみ）、対戦中の詳細表示は
4〜5 秒（通常パイプラインと検索 1〜2 回）。カードが無い画面での失敗は約 2 秒。
常駐モードの動作ログは `user_data_dir/ptcgl.log`（`app.configure_file_logging()`、2 MB × 3 世代）。

## スレッド構成（常駐モード）
- **UI スレッド**: Qt イベントループと `MainWindow` のみ。
- **RecognitionWorker**（`ui/worker.py`、`WorkerThread` 上で動く `QObject`）: `QTimer` で
  `capture.interval_ms` ごとに `WatchSession.step()` を呼ぶ。`Pipeline`（OCR エンジン・HTTP・SQLite）は
  このスレッドが所有し、UI からの依頼（候補の確定・一時停止）はキュー接続のスロットで受ける。
  シグナル: `status_changed(str)`, `result_ready(IdentifyResult)`, `window_changed(WindowInfo | None)`。
- **WatchSession / FrameWatcher**（`watch.py`、Qt 非依存）: 1 ステップで「ウィンドウ探索 → キャプチャ →
  変化判定 → 認識」を行う。詳細は上の「常駐モードの認識フロー」。`change_threshold`（既定 4.0）は
  基準フレームとの平均差分、`stable_threshold`（既定 1.5）は直前フレームとの平均差分の閾値。
- ウィンドウは hwnd を保持して毎ステップ `IsWindow` / `IsIconic` / 矩形を確認し、消えたら再探索する。
  再探索・ウィンドウ切替時は差分の履歴を捨てる。
- 表示ウィンドウ自身は `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` でキャプチャから除外し、
  PTCGL に重ねて置いても認識に写り込まない（`overlay.exclude_from_capture`）。
- 結果の反映（`MainWindow.show_result()`）: 同じ英語カード・同じ日本語版が続けば表示を変えない。
  カードが見つからないときは直前の表示を残す（誤ったカードを出さない）。
- 画面の状態行には操作に関わる状態だけを出す（「監視中」「認識中...」「カードを特定できません」
  「別イラスト（要確認）」「PTCGL が見つかりません」など）。認識秒数・解像度・失敗理由・照合の
  理由文は `ptcgl.log` にのみ残す。配布 exe はコンソール無し（`console=False`）で、未捕捉例外は
  `sys.excepthook` でログファイルに書く。

## 設定（`config.py`）
`%LOCALAPPDATA%\ptcgl\config.json`（platformdirs の `user_config_dir("ptcgl", appauthor=False)`）。
`cache_dir` / `images_dir` / `db_path` は既定（`%LOCALAPPDATA%\ptcgl\` 配下）のままなら書き出さない。主な項目:
- `target_window.title_pattern` / `process_name`
- `capture.interval_ms`, `capture.detection_mode`（`auto` | `manual`）, `capture.card_region_ratio`,
  `capture.change_threshold`, `capture.stable_threshold`
- `overlay.monitor_name`, `overlay.ratio_pos`, `overlay.ratio_size`, `overlay.always_on_top`,
  `overlay.exclude_from_capture`
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
| 2026-11-11 | 常駐モードは画面変化の差分で OCR のタイミングを決める（固定周期で OCR しない） | 全画面 OCR は WQHD で 1 回 5〜12 秒かかり、毎秒実行すると対戦中の CPU を占有する。変化して静止した瞬間だけ認識すれば十分 |
| 2026-11-11 | 監視ロジック（`watch.py`）を Qt から分離し、依存を呼び出し可能オブジェクトで注入する | Win32・画面キャプチャ無しで pytest できるようにするため |
| 2026-11-11 | onnxruntime のスレッド数は自動で 2〜4 に抑え、角度分類（cls）は使わない | 既定の全論理コア（開発機 24）では 1 パス 2.5 秒、4〜8 スレッドで 0.5 秒。PTCGL が動いている負荷時は 4 が最も安定。画面の文字は回転しないので cls は不要 |
| 2026-11-11 | パス 2 は領域の下帯（番号・セットコード）だけ先に OCR し、セットコードで確定できなければ全体を読む | ワザ説明文の行を読むのが pass 2 の大半のコスト。PTCGL のフルカード表示はセットコードが明瞭で、名前は分母が衝突したときだけ必要 |
| 2026-11-11 | 常駐モードは変化領域に対する「検出 + 選択的認識」の高速パスを先に試す | 全行認識は 1 枚 1.5〜3 秒かかり、その大半はワザ説明文の認識。番号行だけ読めばセットコードで確定でき、0.5〜1 秒になる |
| 2026-11-11 | 静止を待たずに変化検知の直後に認識する | 拡大表示は 1 フレームで出終わることが多く、静止待ちの 1 ティック分が丸ごと無駄だった。動いている最中の失敗は基準を更新しないことで静止後に拾える |
| 2026-11-11 | 表示ウィンドウを `WDA_EXCLUDEFROMCAPTURE` でキャプチャから除外する | PTCGL をフルスクリーンにすると表示ウィンドウを重ねざるを得ず、写り込むと OCR が自分の表示を読んでしまう |
| 2026-11-11 | 画像は個人利用・ローカルキャッシュのみ、再配布しない | pokemon-card.com の規約が複製・配布を禁止している |
| 2026-11-11 | カード領域は画像処理ではなく OCR アンカー方式で推定する | 輪郭検出・勾配・彩度・色相の 4 方式を試したが、PTCGL の発光枠・丸角・盤面の模様でカード矩形を安定して取れなかった（13 枚中 2 枚）。番号行／HP バッジを手がかりにすると 13 枚中 11 枚で領域を得られた |
| 2026-11-12 | セット表は同梱に加えて、起動時にリポジトリの main から最新版を取得する | 分母がセット表に無い番号はカードとして採用しないため、新弾が出るたびに約 250 MB の配布物を入れ直す必要があった。表だけなら約 10 KB で、push するだけで全利用者に届く |
| 2026-11-11 | 番号が隠れる対戦中の詳細表示は Limitless 検索（名前・HP・ワザ名）で特定する | `unique:cards` で同一テキストのカードに絞れる。返るのは代表印刷なので絵柄は未確認扱い |
| 2026-11-12 | 表示ウィンドウは通常の枠付き・不透明にする（透過しない） | 背景透過を試したが、Windows の Qt ではタイトルバー付きのまま `WA_TranslucentBackground` にすると透過部分が黒く塗られ、枠なしにすると移動・サイズ変更・終了が分かりにくく使いにくかった |

# 検証結果

## 2026-11-11 Phase 0: 照合（OCR なし、セット・番号指定）

開発機（Windows 11）で `python -m ptcgl identify --set X --number N --no-gui` を実行。
初回はネットワーク取得込み、2 回目以降はキャッシュから返る。

| 英語カード | 日本語版（選択） | 同一絵柄 | 候補数 | 初回時間 | 備考 |
| --- | --- | --- | --- | --- | --- |
| SVI 1 Pineco | SV1V #1 クヌギダマ | ○（Shigenori Negishi） | 2 | 2.8 s | |
| SVI 245 Gardevoir ex (SAR) | SV1S #101 サーナイトex | ○（Jiro Sasumo） | 6 | 5.5 s | 通常絵の SV1S #28 は別イラストレーターとして除外された |
| DRI 81 Team Rocket's Mewtwo ex | SV10 #39 ロケット団のミュウツーex | ○（aky CG Works） | 8 | 2.2 s | 通常枠の #39 が SAR #114/#130 より優先された |
| SVI 185 Poké Ball | 日本語版 32 印刷から選択 | ○ | 32 | 2.5 s | トレーナーズもイラストレーター一致で照合できる |

所要時間の大半は Limitless へのアクセス間隔（1 req/s）。候補ページの取得は同一絵柄が見つかった時点で打ち切る。

## 2026-11-11 Phase 0: カード領域検出の方式比較（画像処理）

対象: `assets/samples/shot01〜14`（WQHD 2560x1440、ウィンドウモード、表示倍率は未確認）。

| 方式 | 内容 | 結果 |
| --- | --- | --- |
| contour（Canny → 4 頂点近似 → 縦横比 63:88） | `ContourCardDetector` 既定値 | 13 枚中 2 枚（捨て札一覧・スタジアムのグレー枠カードのみ） |
| contour-loose（min_height 0.15, 縦横比許容 0.2） | 同上を緩めたもの | 2 枚 + 誤検出 3 枚（盤面の小さいカード・UI 部品） |
| canny_close15 / sobel_close21 | エッジ → 大きめの閉処理 → 外接矩形 | ほぼ画面全体が 1 つの塊になり不可 |
| lowsat_bright（低彩度・高輝度マスク） | トレーナーズのグレー枠を狙う | トレーナーズ 3 枚のみ。ポケモンの色枠は拾えない |
| not_green（盤面の緑色相を除外） | 盤面以外を抽出 | UI 部品・手札と結合して不可 |

結論: PTCGL は発光枠・丸角・盤面の模様・重なる UI のため、画像処理だけで拡大カードの矩形を安定して取るのは
難しい。**OCR の結果（番号行「MEG EN 076/132」または最大の数字＝HP バッジ）を手がかりに領域を推定する
「OCR アンカー方式」**に切り替えた（`recognition/anchor.py`）。テンプレートマッチングは、この方式で十分な
結果が出たため未実施（Phase 1 で高速化が必要になった場合の候補として残す）。

### 判明した PTCGL の表示パターン
- フルカード表示（手札ホバー・捨て札一覧・場に出した直後・スタジアム）: 下部に `セットコード EN 番号/分母`。番号行アンカーで特定できる
- バトル場の詳細表示: カードが少し傾くが番号は読める
- 対戦中の詳細表示（自分・相手のポケモンを選択）: 番号が隠れる。HP バッジと名前からの Limitless 検索で特定する
- 基本エネルギー: `MEE EN 006` が小さく分断されて読めない（対象外。翻訳不要のため対応しない）

## 2026-11-11 Phase 0: 画面からの認識（OCR アンカー方式）

下の「評価結果」は `python scripts/evaluate.py assets/samples --out docs/validation-results.md` の出力。
負例（shot01, shot04）は「見つからない」が正解。shot07（基本エネルギー）は対象外。
対戦中の詳細表示（shot02/03/05）は検索で得た代表印刷が Int. Prints で同一カードなら正解とする。
所要時間は 1 回目（Limitless 取得込み）で、2 回目以降はキャッシュにより短くなる。OCR は 2 パスで 3〜5 秒。

## 2026-11-11 Phase 0: 配布ビルド（PyInstaller onedir）

`pyinstaller packaging/ptcgl.spec` → `dist/ptcgl/ptcgl.exe`（約 344 MB、PySide6 / OpenCV / onnxruntime 同梱）。

- PATH から Python を外した状態で `ptcgl.exe identify --set SVI --number 1 --no-gui` が完走した
- RapidOCR の ONNX モデル 3 点（det / rec / cls）が `_internal/rapidocr_onnxruntime/models/` に同梱され、
  合成カード画像（"Pineco", "HP 60", "001/198" を描画）に対する exe 内 OCR が動作した（OCR 2.9 s、名前・HP・番号をすべて正しく読み取り、SVI #1 → SV1V #1 を照合）
- 既知の問題として `-v` を先頭に置くと GUI が起動してしまうエントリポイントの不具合を修正した

## 評価結果

- サンプル数: 14
- 検出率: 12/14 (86%)
- 英語カード特定率: 11/14 (79%)
- 日本語照合率（ラベルあり分）: 0/0
- 所要時間 中央値: 7208 ms

| file | env | detect | OCR name/number | EN 特定 | JP 照合 | 時間(ms) | 備考 |
|---|---|---|---|---|---|---|---|
| shot01_172654.png | 2560x1440 / window | NG | - | NG (None) | - | 5473 | 拡大なし（盤面のみ・負例） 拡大表示されたカードが見つかりません（番号・HP のいずれも検出できず） |
| shot02_172707.png | 2560x1440 / window | OK | Charmander None/None | OK (PFL #11 (en)) | - | 6790 | 対戦中の詳細表示 Charmander HP80（番号非表示）  |
| shot03_172933.png | 2560x1440 / window | OK | Mega Charizard Y None/None | OK (ASC #22 (en)) | - | 6279 | 対戦中の詳細表示 Mega Charizard Y ex HP360（番号非表示）  |
| shot04_172941.png | 2560x1440 / window | OK | Megy Charizard Y None/None | NG (None) | - | 4510 | 拡大なし（攻撃アニメーション中・負例） 検索で見つかりません: Megy Charizard Y HP360 |
| shot05_173359.png | 2560x1440 / window | OK | Riolu None/None | OK (ASC #112 (en) 同一カード) | - | 6760 | 対戦中の詳細表示 Riolu HP80（番号非表示）  |
| shot06_173408.png | 2560x1440 / window | OK | Riolu 76/132 | OK (MEG #76 (en)) | - | 10187 | 手札ホバーのフルカード Riolu 076/132  |
| shot07_173444.png | 2560x1440 / window | NG | - | NG (None) | - | 3558 | 手札ホバーのフルカード Basic Fighting Energy MEE 006（分母なし） 拡大表示されたカードが見つかりません（番号・HP のいずれも検出できず） |
| shot08_173534.png | 2560x1440 / window | OK | Solrock 75/132 | OK (MEG #75 (en)) | - | 11608 | 手札ホバーのフルカード Solrock 075/132  |
| shot09_173540.png | 2560x1440 / window | OK | TeamRocket'sPetrel 176/182 | OK (DRI #176 (en)) | - | 6593 | 場に出した直後のフルカード Team Rocket's Petrel 176/182  |
| shot10_173544.png | 2560x1440 / window | OK | Salvatore 160/162 | OK (TEF #160 (en)) | - | 8008 | 場に出した直後のフルカード Salvatore 160/162  |
| shot11_173901.png | 2560x1440 / window | OK | Team Rocket'sPetrel 176/182 | OK (DRI #176 (en)) | - | 11427 | 捨て札一覧からのフルカード Team Rocket's Petrel 176/182  |
| shot12_173915.png | 2560x1440 / window | OK | Gravity Mountain 177/191 | OK (SSP #177 (en)) | - | 7627 | スタジアムのフルカード Gravity Mountain 177/191  |
| shot13_173923.png | 2560x1440 / window | OK | Buddy-Buddy Poffin 144/162 | OK (TEF #144 (en)) | - | 7654 | 捨て札一覧からのフルカード Buddy-Buddy Poffin 144/162  |
| shot14_174129.png | 2560x1440 / window | OK | N's Zoroarke 98/159 | OK (JTG #98 (en)) | - | 9692 | バトル場の詳細表示（カードが傾く・右に情報パネル） N's Zoroark ex 098/159  |

## 失敗例

### shot01_172654.png

期待: None

OCR:
```
0.99 Professor
0.99 Riolu
1.00 80
0.99 39:00
1.00 13
0.99 3
0.99 END
0.99 TURN
0.99 Charmander
1.00 80
1.00 3
0.98 Welcometoyourfirstbattle!
0.99 39:00
1.00 110
1.00 3
0.98 ENERGY
1.00 BasicEnergy
0.99 ENERGY
```

### shot04_172941.png

期待: None

OCR:
```
0.97 MegaCharizardY
1.00 360
0.99 ExplosionY
0.87 d3Enengy from this Pakemon.and thisatrack does
0.90 erchedPoker
```

### shot07_173444.png

期待: MEE #6 (en)

OCR:
```
0.60 NOW
0.97 WidePika
0.94 300*
0.76 190 0
0.99 Pecharunt
0.65 PeKeMoYVN
0.72 PeKeMoN.
0.51 RRRR
1.00 47
0.99 20:00
0.95 Basic Energy
1.00 ENERG
1.00 6
0.99 BasicEnergy
0.99 END
0.99 TURN
0.99 Riolu
0.83 80@
1.00 6
0.99 19:28
0.55 A
0.97 Accelerating Stab
1.00 110
0.71 MEEo
0.83 Mega Lucario ey
1.00 340
0.99 TRAINER
0.99 TRAINER
0.97 TRAINER
0.99 TRAINER
0.99 TRAINER
1.00 BasicEnergy
0.87 ENERG
0.99 Salvatore
0.98 Team Rocket's Petrel
1.00 Judge
1.00 Tarragon
0.99 Ultra Ball
```


## 2026-11-12 解像度別の認識率（合成画像による検証）

他の解像度の実機スクリーンショットが無いため、`assets/samples`（2560x1440）の 14 枚を
縮小（INTER_AREA）・拡大（INTER_CUBIC）して各解像度の画面を模した。縮小は実機の描画より文字が
ぼけるので、低解像度側の結果は実機より悪めに出る（実機で再確認が必要）。
採点は evaluate.py と同じ（負例は「見つからない」が正解、同一カードの別印刷は正解扱い）。
「常駐」は watch.py と同じ順序（identify_fast(full_frame=True) → 失敗時 identify_frame）。
Limitless はキャッシュ済みなので所要時間は認識のみ。

| 解像度 | 常駐モード順序 | 通常パス (identify_frame) | fast 中央値 | frame 中央値 |
|---|---|---|---|---|
| 3840x2160 | 12/14 | 12/14 | 649 ms | 2542 ms |
| 2560x1440（元） | 13/14 | 13/14 | 676 ms | 2103 ms |
| 1920x1080 | 13/14 | 12/14 | 753 ms | 1631 ms |
| 1600x900 | 12/14 | 10/14 | 727 ms | 1248 ms |
| 1366x768 | 9/14 | 9/14 | 726 ms | 1120 ms |
| 1280x720 | 8/14 | 8/14 | 937 ms | 1081 ms |

失敗の内訳（shot07 基本エネルギーは対象外のため除く）:
- 3840x2160: shot04（負例）で前のカード ASC #22 を誤検出
- 1920x1080: 通常パスのみ shot06（手札ホバー）が落ちる。常駐順序では高速パスが拾う
- 1600x900: shot06（手札ホバー）。通常パスは shot08/09 も落ちる
- 1366x768: shot06/08（手札ホバー）, shot09/10（場に出した直後）がすべて落ちる
- 1280x720: 上記に加え shot02（Charmander HP80）が ASC #20 に誤照合

原因: 番号行の文字高さ。1440p で手札ホバーは 17px、場に出した直後は 35px（カード高 480px / 1230px）。
1080p で手札ホバーは約 13px でぎりぎり読め、900p 以下（約 11px）で検出・認識が崩れる。
捨て札一覧・スタジアム・バトル場の詳細表示（43〜46px）と、HP バッジ経由の対戦中の詳細表示は
720p まで安定している。1366x768 / 1280x720 で使う場合は、手札ホバーと場に出した直後の表示が
主な未対応になる。

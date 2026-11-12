# PTCGL 日本語カードビューア

Pokémon TCG Live（PTCGL）の画面に表示される英語カードを認識し、対応する日本語版ポケモンカード画像を表示する Windows デスクトップアプリです。
英語が読めなくても、カードの効果を日本語で確認しながら対戦できます。

![PTCGL の画面と、右端に表示された日本語カードビューア](docs/images/overview.jpg)

- ゲーム本体のファイル・メモリ・通信には一切触れず、画面キャプチャだけで動作します
- カード情報と日本語画像の出典は Limitless TCG（日本語画像は pokemon-card.com 由来）です。取得した画像は **個人利用・ローカルキャッシュのみ** とし、再配布しません。アプリ本体にカード画像は含まれていません
- mod ではないので PTCGL が更新されてもそのまま使えます。新しい拡張パックのセット一覧は起動時に自動で取得するため、アプリの入れ直しも不要です

## 使い方（遊ぶ人向け）

Python などのインストールは不要です。

1. [Releases ページ](https://github.com/willcom1992/ptcgl/releases) を開き、最新版の「Assets」にある `ptcgl-windows.zip` をダウンロードします。
2. zip を右クリック →「すべて展開」で展開し、できた `PTCGL 日本語カードビューア` フォルダを好きな場所に置きます。
3. PTCGL を起動してから、フォルダ内の `PTCGL 日本語カードビューア.exe` をダブルクリックします。小さなウィンドウが開き、下部が「監視中」になれば準備完了です。
4. PTCGL でカードを拡大表示（手札にマウスを乗せる、場のポケモンをクリックするなど）すると、約 1 秒で日本語版の画像が表示されます。

初回起動時の警告への対処、画面の見方、認識できる画面、困ったときの対処は **[使い方ガイド](docs/usage.md)** の 1〜6 章にまとめています。

## 必要環境

- Windows 10 / 11
- PTCGL（インストール済みで対戦できる状態）
- インターネット接続（初めて見るカードの情報を取得するため）
- 画面解像度はフルHD（1920x1080）以上を推奨。1600x900 以下では手札にマウスを乗せたときの拡大表示が読めなくなります

## 開発者向け

開発には Python 3.12 が必要です。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

```powershell
python -m ptcgl                                   # GUI（常駐モード）
python -m ptcgl identify --set SVI --number 1     # 英語カードを指定して日本語版を表示
python -m ptcgl -v identify --image shot.png      # スクリーンショットから検出→OCR→照合
python -m ptcgl windows                           # ウィンドウ一覧（矩形・DPI）
python -m ptcgl capture --window "TCG Live" --out shot.png
```

```powershell
pytest                             # テスト
ruff check . ; ruff format .       # Lint / フォーマット
mypy src                           # 型チェック
pyinstaller packaging/ptcgl.spec   # 配布用ビルド（dist/PTCGL 日本語カードビューア/）
```

設定・キャッシュの保存先はユーザーごとのディレクトリです（`python -m ptcgl config-path` で表示）。
精度の計測は `assets/samples/` にスクリーンショットと `labels.csv` を置いて
`python scripts/evaluate.py assets/samples --out docs/validation-results.md` を実行します。

exe の作成と Releases への載せ方は [使い方ガイド](docs/usage.md) の 7 章、コマンドラインと設定は 8 章、要件は [docs/spec.md](docs/spec.md)、設計は [docs/architecture.md](docs/architecture.md) を参照してください。

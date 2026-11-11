# ptcgl

Pokémon TCG Live（PTCGL）の画面に表示される英語カードを自動認識し、対応する日本語版ポケモンカード画像を表示する Windows デスクトップアプリです。

## 必要環境
- Windows 10/11
- Python 3.10 以上

## セットアップ
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 起動
```powershell
python -m ptcgl
```

## 開発
```powershell
pytest                        # テスト
ruff check . ; ruff format .  # Lint / フォーマット
mypy src                      # 型チェック
```

詳細は [docs/spec.md](docs/spec.md) と [docs/architecture.md](docs/architecture.md) を参照してください。

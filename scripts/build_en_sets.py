"""TCGdex の英語セット情報から src/ptcgl/data/en_sets.json を生成する。

使い方:
    python scripts/build_en_sets.py [--cache-dir DIR]

`/v2/en/sets` の一覧には abbreviation（PTCGL/Limitless のセットコード）が無いため、
対象シリーズ（SWSH 以降）の各セット詳細 `/v2/en/sets/{id}` を取得する。取得結果は
--cache-dir に保存し、再実行時はネットワークを使わない。

出力: {"updated": 生成日時（UTC）, "sets": [...]}。sets の各行は code（PTCGL のセットコード）,
tcgdex_id, name, official（番号の分母）, total。配布済みのアプリは起動時に main のこのファイルを
取得し、updated が同梱版より新しければ差し替える（形式は src/ptcgl/data/sets.py が検証する）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.tcgdex.net/v2/en/sets"
OUT_PATH = Path(__file__).resolve().parents[1] / "src" / "ptcgl" / "data" / "en_sets.json"
# PTCGL に収録されているシリーズ（Sword & Shield 以降）
TARGET_PREFIXES = ("swsh", "sv", "me", "30th", "2023", "2024")

# TCGdex の abbreviation が無い／Limitless と異なる場合の上書き
ABBREVIATION_OVERRIDES: dict[str, str] = {}
# Limitless に存在しない（PTCGL でも分母付き番号で表示されない）セットは除外する
EXCLUDED_IDS = {"swshp", "2023sv", "2024sv"}


def fetch_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "ptcgl-dev-script"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def load_set_detail(set_id: str, cache_dir: Path) -> dict:
    cache_path = cache_dir / f"{set_id}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    data = fetch_json(f"{API_BASE}/{set_id}")
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.5)
    return data


def build(details: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for s in details:
        set_id = s.get("id", "")
        if set_id in EXCLUDED_IDS:
            continue
        raw_abbr = s.get("abbreviation")
        if isinstance(raw_abbr, dict):
            raw_abbr = raw_abbr.get("official")
        abbr = ABBREVIATION_OVERRIDES.get(set_id) or raw_abbr
        if not abbr:
            print(f"skip (no abbreviation): {set_id} {s.get('name')}", file=sys.stderr)
            continue
        count = s.get("cardCount", {})
        # "BRS:TG" のようなサブセットは親セットのコード + 番号の接頭辞（TG01/TG30）で表す
        number_prefix = ""
        if ":" in str(abbr):
            abbr, number_prefix = str(abbr).split(":", 1)
        rows.append(
            {
                "code": str(abbr).upper(),
                "tcgdex_id": set_id,
                "name": s.get("name", ""),
                "official": int(count.get("official") or 0),
                "total": int(count.get("total") or 0),
                "number_prefix": number_prefix.upper(),
                "release_date": s.get("releaseDate", ""),
            }
        )
    rows.sort(key=lambda r: (r["release_date"], r["tcgdex_id"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/tcgdex_sets"))
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    listing_path = args.cache_dir / "_sets.json"
    if listing_path.exists():
        listing = json.loads(listing_path.read_text(encoding="utf-8"))
    else:
        listing = fetch_json(API_BASE)
        listing_path.write_text(json.dumps(listing, ensure_ascii=False), encoding="utf-8")

    target_ids = [s["id"] for s in listing if s["id"].startswith(TARGET_PREFIXES)]
    details = [load_set_detail(set_id, args.cache_dir) for set_id in target_ids]
    rows = build(details)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "sets": rows}
    args.out.write_bytes((json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(f"{len(rows)} sets -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

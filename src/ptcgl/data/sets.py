"""英語セット表（PTCGL のセットコード・番号の分母）。

`en_sets.json` は scripts/build_en_sets.py で TCGdex から生成する。パッケージ同梱
データなので、PyInstaller 実行時も importlib.resources で参照できる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


@dataclass(frozen=True)
class EnSet:
    code: str  # PTCGL / Limitless のセットコード（例 "SVI"）
    tcgdex_id: str
    name: str
    official: int  # カード番号の分母（"123/198" の 198）
    total: int
    release_date: str
    number_prefix: str = ""  # Trainer Gallery 等のサブセット番号の接頭辞（"TG01/TG30" の "TG"）


@lru_cache(maxsize=1)
def load_en_sets() -> tuple[EnSet, ...]:
    text = resources.files("ptcgl.data").joinpath("en_sets.json").read_text(encoding="utf-8")
    rows = json.loads(text)
    return tuple(
        EnSet(
            code=r["code"],
            tcgdex_id=r["tcgdex_id"],
            name=r["name"],
            official=int(r["official"]),
            total=int(r["total"]),
            release_date=r.get("release_date", ""),
            number_prefix=r.get("number_prefix", ""),
        )
        for r in rows
    )


def find_set(code: str) -> EnSet | None:
    code = code.upper()
    for s in load_en_sets():
        if s.code == code and not s.number_prefix:
            return s
    return None


def sets_by_official_total(total: int, number_prefix: str = "") -> list[EnSet]:
    """番号の分母が一致するセットを新しい順に返す（衝突することがある）。"""
    prefix = number_prefix.upper()
    matches = [s for s in load_en_sets() if s.official == total and s.number_prefix == prefix]
    matches.sort(key=lambda s: s.release_date, reverse=True)
    return matches


def known_codes() -> frozenset[str]:
    return frozenset(s.code for s in load_en_sets())

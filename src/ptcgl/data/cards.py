"""カードDB の検索と英→日の対応付け。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ptcgl.recognition.card import RecognizedCard


@dataclass(frozen=True)
class JapaneseCard:
    name_ja: str
    image_path: Path
    set_code: str | None = None
    number: str | None = None


class CardRepository:
    """英語カード情報から日本語カードを検索する。

    対応付けの方式は docs/spec.md の「データソース」参照。現状は未実装。
    """

    def find_japanese(self, card: RecognizedCard) -> JapaneseCard | None:
        raise NotImplementedError("英→日の対応付けは未実装")

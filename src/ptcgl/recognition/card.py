"""OCR テキストからカード情報を抽出する。"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 例: "SVI 123/198", "PAL 045/193"
_SET_NUMBER_RE = re.compile(r"\b([A-Z]{2,4}\d?)\s+(\d{1,3})\s*/\s*(\d{1,3})\b")


@dataclass(frozen=True)
class RecognizedCard:
    name_en: str | None
    set_code: str | None
    number: str | None
    confidence: float = 0.0


def parse_set_number(text: str) -> tuple[str, str] | None:
    """テキストからセットコードとカード番号を抽出する。"""
    m = _SET_NUMBER_RE.search(text)
    if not m:
        return None
    return m.group(1), m.group(2)

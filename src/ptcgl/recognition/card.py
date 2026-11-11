"""OCR テキストからカード情報（名前・セット番号・HP）を抽出する。

画面に依存する処理（領域検出）は recognition/detector.py、文字認識は recognition/ocr.py に
分離し、このモジュールは文字列だけを扱う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ptcgl.data.sets import find_set
from ptcgl.recognition.ocr import OcrLine

# 例: "SVI 123/198", "PAL 045/193", PTCGL の印字 "MEG EN 076/132", "MEGEN 076/132"
_SET_NUMBER_RE = re.compile(r"\b(?!EN\b)([A-Z]{2,6}\d?)\s*(?:EN)?\s+(\d{1,3})\s*/\s*(\d{1,3})\b")
# 分母の無い番号（基本エネルギー）: "MEE EN 006"
_SET_NUMBER_NO_TOTAL_RE = re.compile(r"\b([A-Z]{2,4}\d?)\s*EN\s*(\d{3})\b")
# 例: "123/198", "081 / 182", "TG05/TG30"
_NUMBER_TOTAL_RE = re.compile(
    r"(?<![\dA-Z/])(?P<prefix>[A-Z]{2})?(?P<number>\d{1,3})\s*/\s*(?P=prefix)?(?P<total>\d{2,3})(?![\d/])"
)
# 例: "HP 280", "280 HP", "HP280"（OCR で H/P が崩れることがあるので緩めに）
_HP_RE = re.compile(r"(?:H[PR]\s*(\d{2,3}))|(?:(\d{2,3})\s*H[PR])")
# Trainer Gallery / Galarian Gallery / Shiny Vault の番号接頭辞
KNOWN_NUMBER_PREFIXES = frozenset({"TG", "GG", "SV"})
_NON_NAME_RE = re.compile(r"^(?:HP|\d+|[^A-Za-z]*)$")
# カード種別などのラベル（名前ではない）
LABEL_WORDS = {
    "basic",
    "stage",
    "stage1",
    "stage2",
    "stage 1",
    "stage 2",
    "supporter",
    "item",
    "stadium",
    "trainer",
    "tool",
    "pokemon tool",
    "energy",
    "basic energy",
    "special energy",
    "ability",
    "mega",
    "ex",
    "evolves from",
    "weakness",
    "resistance",
    "retreat",
    "pokemon",
}


@dataclass(frozen=True)
class RecognizedCard:
    name_en: str | None
    set_code: str | None
    number: str | None
    total: int | None = None
    hp: int | None = None
    number_prefix: str = ""
    confidence: float = 0.0

    @property
    def has_number(self) -> bool:
        return self.number is not None


def parse_set_number(text: str) -> tuple[str, str] | None:
    """テキストからセットコードとカード番号を抽出する。"""
    m = _SET_NUMBER_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    m = _SET_NUMBER_NO_TOTAL_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    return None


def parse_number_total(text: str) -> tuple[str, int, str] | None:
    """ "123/198" 形式から (番号, 分母, 接頭辞) を抽出する。番号は先頭のゼロを除く。"""
    m = _NUMBER_TOTAL_RE.search(text)
    if not m:
        return None
    prefix = (m.group("prefix") or "").upper()
    if prefix not in KNOWN_NUMBER_PREFIXES:
        prefix = ""  # "EF160/162"（TEF EN の読み崩れ）のような接頭辞は無視する
    number = m.group("number").lstrip("0") or "0"
    if prefix:
        number = f"{prefix}{m.group('number')}"
    return number, int(m.group("total")), prefix


def parse_hp(text: str) -> int | None:
    m = _HP_RE.search(text)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def merge_rows(lines: list[OcrLine]) -> list[str]:
    """同じ行（縦位置が重なる）の OCR 行を左から結合したテキストを返す。

    "DRI EN" と "176/182" のように、1 行の印字が別々の行として読まれることがあるため。
    """
    remaining = sorted(lines, key=_box_top)
    rows: list[str] = []
    while remaining:
        base = remaining.pop(0)
        base_cy = _box_top(base) + _box_height(base) / 2
        group = [base]
        rest: list[OcrLine] = []
        for ln in remaining:
            cy = _box_top(ln) + _box_height(ln) / 2
            if abs(cy - base_cy) <= max(_box_height(base), _box_height(ln)) * 0.6:
                group.append(ln)
            else:
                rest.append(ln)
        remaining = rest
        group.sort(key=lambda ln: min(p[0] for p in ln.box))
        rows.append(" ".join(ln.text for ln in group))
    return rows


def _box_top(line: OcrLine) -> float:
    return min(p[1] for p in line.box)


def _box_height(line: OcrLine) -> float:
    ys = [p[1] for p in line.box]
    return max(ys) - min(ys)


def _clean_name(text: str) -> str:
    text = re.sub(r"\bHP\s*\d+.*$", "", text)
    text = re.sub(r"[^A-Za-z0-9\u00C0-\u017F'’\-.&: ]", " ", text)  # é 等は残す
    return " ".join(text.split()).strip()


def _box_left(line: OcrLine) -> float:
    return min(p[0] for p in line.box)


def extract_card_info(lines: list[OcrLine], frame_height: float | None = None) -> RecognizedCard:
    """OCR 行からカード名・番号・HP を推定する。

    番号は "NNN/TTT" 形式、セットコードは "SVI 123/198" のように併記されている場合のみ。
    名前は、番号行が見つかればそれと左端が揃う（同じカードの左余白にある）行の中で、
    番号行より上にあり最も文字高の大きいものを採用する。番号行が無ければ上部 30% から選ぶ。
    """
    if not lines:
        return RecognizedCard(None, None, None)

    height = frame_height or max(max(p[1] for p in ln.box) for ln in lines) or 1.0
    joined = " ".join(ln.text for ln in lines)

    set_code: str | None = None
    number: str | None = None
    total: int | None = None
    prefix = ""
    number_conf = 0.0
    # 単独の行 → 同じ段の結合テキスト の順にセットコード付き番号を探す。
    # 既知のセットコードを含むものがあればそれを優先する（"EN 076/132" より "MEG EN 076/132"）
    found: list[tuple[str, float, tuple[str, str]]] = []
    for text, conf in [(ln.text, ln.confidence) for ln in lines] + [
        (row, 0.8) for row in merge_rows(lines)
    ]:
        sn = parse_set_number(text)
        if sn:
            found.append((text, conf, sn))
    if found:
        known = [f for f in found if find_set(f[2][0])]
        text, number_conf, sn = (known or found)[0]
        set_code, number = sn[0], sn[1].lstrip("0") or "0"
        nt = parse_number_total(text)
        total = nt[1] if nt else None
    if number is None:
        for ln in lines:
            nt = parse_number_total(ln.text)
            if nt:
                number, total, prefix = nt
                number_conf = ln.confidence
                break

    hp: int | None = None
    for ln in lines:
        hp = parse_hp(ln.text)
        if hp is not None:
            break
    if hp is None:
        hp = parse_hp(joined)

    name: str | None = None
    name_conf = 0.0
    number_line = next(
        (ln for ln in lines if parse_set_number(ln.text) or parse_number_total(ln.text)), None
    )
    if number_line is not None:
        # カード幅 ≈ 高さ × 63/88。左端のずれがカード幅の 12% 以内なら同じカードの行とみなす
        num_left = _box_left(number_line)
        num_top = _box_top(number_line)
        num_h = max(_box_height(number_line), 1.0)
        # 左端のずれは番号行の高さの 4 倍（カード幅の約 1 割）まで。名前は番号より十分上にある
        top_lines = [
            ln
            for ln in lines
            if abs(_box_left(ln) - num_left) <= num_h * 4 and _box_top(ln) < num_top - num_h * 8
        ]
    else:
        top_lines = [ln for ln in lines if _box_top(ln) <= height * 0.3]
    candidates = []
    for ln in top_lines:
        cleaned = _clean_name(ln.text)
        if len(cleaned) < 3 or _NON_NAME_RE.match(cleaned):
            continue
        if cleaned.lower() in LABEL_WORDS or cleaned.lower().startswith("evolves from"):
            continue
        if len(cleaned.split()) > 5 or len(cleaned) > 32:  # 説明文
            continue
        candidates.append((_box_height(ln), ln.confidence, cleaned))
    if candidates:
        candidates.sort(reverse=True)
        _, name_conf, name = candidates[0]

    parts = [c for c in (number_conf, name_conf) if c > 0]
    confidence = sum(parts) / len(parts) if parts else 0.0
    return RecognizedCard(
        name_en=name,
        set_code=set_code,
        number=number,
        total=total,
        hp=hp,
        number_prefix=prefix,
        confidence=confidence,
    )

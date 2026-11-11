"""OCR 結果を手がかりに、画面内の拡大カードの領域と主要テキストを特定する。

PTCGL の拡大表示は 2 系統ある。
- フルカード表示（手札ホバー、捨て札、場に出した直後）: 印刷どおりのカードで、下部に
  「MEG EN 076/132」のようにセットコード・番号・分母が入る。→ 番号行をアンカーにする
- 対戦中の詳細表示: 名前バーと大きな HP バッジ、ワザ説明パネルが重なり、番号は隠れる。
  → 画面内で最も大きい数字（HP バッジ）をアンカーにし、その左にある名前行を取る

座標はすべてフレームに対する比率で扱い、解像度に依存しない。
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from ptcgl.data.sets import find_set, sets_by_official_total
from ptcgl.recognition.detector import CARD_ASPECT, CardBox
from ptcgl.recognition.ocr import OcrLine

# "MEG EN 076/132", "MEGEN076/132", "JIG098/159"（OCR の崩れ込み）, "076/132", "TG05/TG30"
# セットコードは非貪欲にして "MEGE" + "N076" のような誤分割を防ぐ。番号の接頭辞は既知のものだけ
_NUMBER_LINE_RE = re.compile(
    r"(?:(?P<set>[A-Z]{2,4}?)\s*(?:EN|FN|EM)?\s*)?"
    r"(?P<number>(?:TG|GG|SV)?\d{1,3})\s*/\s*(?P<total>(?:TG|GG|SV)?\d{2,3})"
)
# 分母の無い番号（基本エネルギー）: "MEE EN 006", "MEEEN006"
_NUMBER_NO_TOTAL_RE = re.compile(r"(?P<set>[A-Z]{2,4})\s*EN\s*(?P<number>\d{3})(?!\d|/)")
_HP_BADGE_RE = re.compile(r"^\d{2,3}$")
_ALPHA_RE = re.compile(r"[A-Za-z]{3,}")

# 番号テキストの高さに対するカード全体の高さ（PTCGL の描画で実測: 21〜24 倍）
# 番号行の高さは OCR の囲み方で 17〜34 倍とぶれるため ROI は上方向に広く取り、
# 名前行はパス 2 で番号行と左端が揃う行から選ぶ（card.py）
_CARD_HEIGHT_PER_NUMBER_HEIGHT = 40.0


@dataclass(frozen=True)
class LineGeom:
    line: OcrLine
    x: float  # 左端（比率）
    y: float  # 上端（比率）
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def text(self) -> str:
        return self.line.text


@dataclass
class Anchor:
    kind: str  # "number" | "hp_badge"
    roi: CardBox
    name: str | None = None
    hp: int | None = None
    set_code: str | None = None
    number: str | None = None
    total: int | None = None
    number_prefix: str = ""  # Trainer Gallery 等の接頭辞（"TG05/TG30" の "TG"）
    text_lines: list[str] = field(default_factory=list)
    confidence: float = 0.0
    line: OcrLine | None = None  # アンカーの元になった行（番号行または HP バッジ）


def to_geoms(lines: list[OcrLine], width: float, height: float) -> list[LineGeom]:
    out: list[LineGeom] = []
    for ln in lines:
        xs = [p[0] for p in ln.box]
        ys = [p[1] for p in ln.box]
        out.append(
            LineGeom(
                ln,
                min(xs) / width,
                min(ys) / height,
                (max(xs) - min(xs)) / width,
                (max(ys) - min(ys)) / height,
            )
        )
    return out


def _clamp_box(x: float, y: float, w: float, h: float, conf: float) -> CardBox:
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + w), min(1.0, y + h)
    return CardBox(x0, y0, max(x1 - x0, 0.01), max(y1 - y0, 0.01), conf)


def find_number_anchor(geoms: list[LineGeom], frame_aspect: float) -> Anchor | None:
    """番号行（NNN/TTT）を探し、そこからカード領域を推定する。"""
    # (行高, 行, セットコード, 番号, 分母, 接頭辞)
    best: tuple[float, LineGeom, str | None, str, int | None, str] | None = None
    for g in geoms:
        # 元のテキスト → 空白を除いたテキスト の順に試す（空白除去は "MEG EN" の読み崩れ対策）
        m = _NUMBER_LINE_RE.search(g.text.upper()) or _NUMBER_LINE_RE.search(
            g.text.replace(" ", "").upper()
        )
        if not m:
            continue
        set_code, number, total = m.group("set"), m.group("number"), m.group("total")
        prefix = "".join(itertools.takewhile(str.isalpha, number))
        total_prefix = "".join(itertools.takewhile(str.isalpha, total))
        total_digits = total[len(total_prefix) :]
        if total_prefix and prefix != total_prefix:
            # "TG05/TG30" でセット部分が "TG" を取ってしまった場合は番号側に戻す
            if set_code != total_prefix:
                continue
            set_code, prefix, number = None, total_prefix, total_prefix + number
        # 分母が既知のセットの枚数と一致しないもの（"10/280" のような残り HP 表示）は番号ではない
        if not total_digits.isdigit() or not sets_by_official_total(int(total_digits), prefix):
            continue
        if best is None or g.h > best[0]:
            best = (g.h, g, set_code, number, int(total_digits), prefix)
    if best is None:
        # 分母の無い番号（基本エネルギー）。セットコードが既知のものだけ採用する
        for g in geoms:
            m2 = _NUMBER_NO_TOTAL_RE.search(g.text.replace(" ", "").upper())
            if m2 and find_set(m2.group("set")) and (best is None or g.h > best[0]):
                best = (g.h, g, m2.group("set"), m2.group("number"), None, "")
    if best is None:
        return None
    _, g, set_code, raw_number, total, prefix = best
    # 番号行の高さからカード高さを推定（比率座標なので横方向はアスペクトで換算）
    card_h = g.h * _CARD_HEIGHT_PER_NUMBER_HEIGHT
    card_w = card_h * CARD_ASPECT / frame_aspect
    roi = _clamp_box(
        g.x - card_w * 0.08,
        g.y + g.h - card_h,
        card_w * 1.18,
        card_h * 1.06,
        g.line.confidence,
    )
    number = raw_number if prefix else (raw_number.lstrip("0") or "0")
    return Anchor(
        kind="number",
        roi=roi,
        set_code=set_code,
        number=number,
        total=total,
        number_prefix=prefix,
        confidence=g.line.confidence,
        line=g.line,
    )


def find_hp_badge_anchor(geoms: list[LineGeom], frame_aspect: float) -> Anchor | None:
    """最も大きい数字（HP バッジ）と、その左にある名前行からカード領域を推定する。"""
    digits = [g for g in geoms if _HP_BADGE_RE.match(g.text.strip()) and g.h >= 0.035]
    if not digits:
        return None
    hp_g = max(digits, key=lambda g: g.h)
    # 名前: HP バッジと縦に重なり、左側にある英字行のうち最も近いもの
    names = [
        g
        for g in geoms
        if _ALPHA_RE.search(g.text)
        and g.x < hp_g.x
        and hp_g.x - (g.x + g.w) < 0.25
        and abs(g.cy - hp_g.cy) < hp_g.h
        and g.h >= hp_g.h * 0.3
    ]
    if not names:
        return None
    name_g = min(names, key=lambda g: hp_g.x - (g.x + g.w))
    card_h = hp_g.h * 11.0  # HP バッジ高さ ≈ カード高さの 1/11（実測）
    card_w = card_h * CARD_ASPECT / frame_aspect
    left = name_g.x - 0.10 * card_w  # ワザ説明パネルは名前より左に張り出す
    top = min(name_g.y, hp_g.y) - 0.04 * card_h
    roi = _clamp_box(
        left, top, card_w * 1.25, card_h * 1.15, min(hp_g.line.confidence, name_g.line.confidence)
    )
    # カード列内のテキスト（ワザ名・特性名の照合に使う）
    column = [
        g.text
        for g in geoms
        if g.x >= roi.rx - 0.05
        and g.x + g.w <= roi.rx + roi.rw + 0.05
        and roi.ry <= g.y <= roi.ry + roi.rh
        and g is not hp_g
    ]
    return Anchor(
        kind="hp_badge",
        roi=roi,
        name=_clean_name(name_g.text),
        hp=int(hp_g.text.strip()),
        text_lines=column,
        confidence=min(hp_g.line.confidence, name_g.line.confidence),
        line=hp_g.line,
    )


def _clean_name(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9\u00C0-\u017F'’\-.&: ]", " ", text)  # é 等は残す
    return " ".join(text.split()).strip()


def find_anchor(lines: list[OcrLine], width: float, height: float) -> Anchor | None:
    geoms = to_geoms(lines, width, height)
    frame_aspect = width / height
    return find_number_anchor(geoms, frame_aspect) or find_hp_badge_anchor(geoms, frame_aspect)

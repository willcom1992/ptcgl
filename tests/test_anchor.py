"""OCR アンカー（番号行・HP バッジ）からのカード領域推定。実スクリーンショットの OCR 結果を模す。"""

from ptcgl.recognition.anchor import find_anchor, find_hp_badge_anchor, to_geoms
from ptcgl.recognition.ocr import OcrLine

W, H = 1600, 900


def _line(text: str, x: float, y: float, w: float, h: float, conf: float = 0.95) -> OcrLine:
    """比率座標で OCR 行を作る。"""
    x0, y0, x1, y1 = x * W, y * H, (x + w) * W, (y + h) * H
    return OcrLine(text=text, confidence=conf, box=((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def test_number_anchor_from_full_card_view() -> None:
    # shot09: 場に出した直後のフルカード。番号行 176/182 が y=0.77、名前が y=0.41 に同じ左端で並ぶ
    lines = [
        _line("6", 0.97, 0.46, 0.02, 0.04),
        _line("PeKeMoy", 0.69, 0.22, 0.05, 0.03),
        _line("190", 0.51, 0.20, 0.03, 0.028),
        _line("176/182", 0.42, 0.77, 0.05, 0.024),
        _line("TeamRocket'sPetrel", 0.42, 0.41, 0.11, 0.022),
    ]
    anchor = find_anchor(lines, W, H)
    assert anchor is not None and anchor.kind == "number"
    assert anchor.number == "176" and anchor.total == 182
    roi = anchor.roi
    # 名前行も番号行も領域に含まれる
    assert roi.rx <= 0.42 <= roi.rx + roi.rw
    assert roi.ry <= 0.41 and roi.ry + roi.rh >= 0.77 + 0.024


def test_number_anchor_with_set_code_and_ocr_noise() -> None:
    lines = [_line("JIG098/159", 0.14, 0.79, 0.09, 0.032), _line("280", 0.30, 0.11, 0.07, 0.08)]
    anchor = find_anchor(lines, W, H)
    assert anchor is not None and anchor.kind == "number"
    assert anchor.set_code == "JIG" and anchor.number == "98" and anchor.total == 159


def test_hp_like_fraction_is_not_a_number_anchor() -> None:
    # "10/280"（残り HP 表示）は番号ではない。番号が無ければ HP バッジにフォールバック
    lines = [
        _line("10/280", 0.90, 0.24, 0.06, 0.03),
        _line("280", 0.30, 0.11, 0.07, 0.08),
        _line("N'sZoroarke", 0.18, 0.12, 0.11, 0.045),
    ]
    anchor = find_anchor(lines, W, H)
    assert anchor is not None and anchor.kind == "hp_badge"
    assert anchor.hp == 280 and anchor.name == "N'sZoroarke"


def test_hp_badge_anchor_from_battle_view() -> None:
    # shot02: HP バッジ "80" が最大の数字、名前 "Charmander" はその左で同じ高さ
    lines = [
        _line("80", 0.39, 0.13, 0.04, 0.064, 1.0),
        _line("Charmander", 0.27, 0.15, 0.09, 0.033, 0.99),
        _line("20", 0.44, 0.62, 0.02, 0.029),
        _line("80", 0.52, 0.20, 0.03, 0.028),  # 隣の小さいカードの HP
        _line("Professor", 0.01, 0.02, 0.05, 0.026),
        _line("Agile", 0.29, 0.45, 0.03, 0.024),
        _line("If this Pokemon has no Energy attached,it has no", 0.24, 0.48, 0.21, 0.023),
        _line("Live Coal", 0.28, 0.60, 0.04, 0.024),
    ]
    anchor = find_hp_badge_anchor(to_geoms(lines, W, H), W / H)
    assert anchor is not None
    assert anchor.name == "Charmander" and anchor.hp == 80
    assert "Agile" in anchor.text_lines and "Live Coal" in anchor.text_lines
    assert "Professor" not in anchor.text_lines
    assert anchor.roi.rx <= 0.27 and anchor.roi.ry <= 0.13


def test_no_anchor_on_plain_board() -> None:
    lines = [_line("Professor", 0.01, 0.02, 0.05, 0.026), _line("3", 0.3, 0.37, 0.01, 0.02)]
    assert find_anchor(lines, W, H) is None

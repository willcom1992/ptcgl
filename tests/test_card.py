import pytest

from ptcgl.recognition.card import (
    extract_card_info,
    parse_hp,
    parse_number_total,
    parse_set_number,
)
from ptcgl.recognition.ocr import OcrLine


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SVI 123/198", ("SVI", "123")),
        ("Charizard ex  PAL 054/193", ("PAL", "054")),
        ("OBF 125 / 197", ("OBF", "125")),
        ("no set number here", None),
    ],
)
def test_parse_set_number(text: str, expected: tuple[str, str] | None) -> None:
    assert parse_set_number(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("081/182", ("81", 182, "")),
        ("001 / 198", ("1", 198, "")),
        ("TG05/TG30", ("TG05", 30, "TG")),
        ("Weakness x2 081/182 I", ("81", 182, "")),
        ("2023 Pokemon", None),
        ("30/06/2024", None),
    ],
)
def test_parse_number_total(text: str, expected) -> None:
    assert parse_number_total(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("HP 280", 280), ("HP280", 280), ("60 HP", 60), ("HR 120", 120), ("Pineco", None)],
)
def test_parse_hp(text: str, expected: int | None) -> None:
    assert parse_hp(text) == expected


def _line(text: str, top: float, height: float = 20, conf: float = 0.9) -> OcrLine:
    box = ((0, top), (100, top), (100, top + height), (0, top + height))
    return OcrLine(text=text, confidence=conf, box=box)


def test_extract_card_info_picks_name_number_hp() -> None:
    lines = [
        _line("Basic", 10, height=12),
        _line("Pineco", 30, height=40),
        _line("HP 60", 32, height=20),
        _line("Guard Press 10", 300),
        _line("Illus. Shigenori Negishi  001/198  G", 780, height=14, conf=0.8),
    ]
    rec = extract_card_info(lines, frame_height=825)
    assert rec.name_en == "Pineco"
    assert rec.number == "1"
    assert rec.total == 198
    assert rec.hp == 60
    assert rec.set_code is None
    assert rec.confidence == pytest.approx((0.9 + 0.8) / 2)


def test_extract_card_info_with_explicit_set_code() -> None:
    lines = [_line("Charizard ex", 20, height=40), _line("PAL 054/193", 800, height=12)]
    rec = extract_card_info(lines, frame_height=825)
    assert rec.set_code == "PAL"
    assert rec.number == "54"
    assert rec.total == 193


def test_extract_card_info_empty() -> None:
    rec = extract_card_info([])
    assert rec.number is None and rec.name_en is None

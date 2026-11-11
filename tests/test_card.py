import pytest

from ptcgl.recognition.card import parse_set_number


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

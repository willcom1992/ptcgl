from pathlib import Path

import pytest

from ptcgl.data.limitless import CardRef, en_image_url, jp_image_url, parse_card_page

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_en_card_basic_fields() -> None:
    page = parse_card_page(load("en_SVI_1.html"), ref_hint=CardRef("en", "SVI", "1"))
    assert page.ref == CardRef("en", "SVI", "1")
    assert page.name == "Pineco"
    assert page.set_name == "Scarlet & Violet"
    assert page.illustrator == "Shigenori Negishi"
    assert page.hp == 60
    assert page.rarity == "Common"
    assert page.image_url == (
        "https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com/tpci/SVI/SVI_001_R_EN_LG.png"
    )


def test_parse_en_card_jp_prints() -> None:
    page = parse_card_page(load("en_SVI_1.html"), ref_hint=CardRef("en", "SVI", "1"))
    refs = [p.ref for p in page.jp_prints]
    assert refs == [CardRef("jp", "SV1V", "1"), CardRef("jp", "SVD", "1")]
    assert page.jp_prints[0].set_name == "Violet ex"
    assert page.jp_prints[1].set_name == "ex Starter Decks"


def test_parse_en_trainer_many_jp_prints_ignores_toggle_row() -> None:
    page = parse_card_page(load("en_SVI_185.html"), ref_hint=CardRef("en", "SVI", "185"))
    assert page.name == "Poké Ball"
    assert page.hp is None
    assert page.illustrator == "Studio Bora Inc."
    assert len(page.jp_prints) == 32
    assert all(p.ref.lang == "jp" for p in page.jp_prints)
    assert CardRef("jp", "SVP", "10") in [p.ref for p in page.jp_prints]


def test_parse_en_card_with_apostrophe_and_multiple_jp_prints() -> None:
    page = parse_card_page(load("en_DRI_81.html"), ref_hint=CardRef("en", "DRI", "81"))
    assert page.name == "Team Rocket's Mewtwo ex"
    assert page.illustrator == "aky CG Works"
    assert page.hp == 280
    refs = [p.ref for p in page.jp_prints]
    assert CardRef("jp", "SV10", "114") in refs
    assert CardRef("jp", "M2a", "63") in refs


def test_parse_jp_card_page() -> None:
    page = parse_card_page(load("jp_SV1V_1.html"), ref_hint=CardRef("jp", "SV1V", "1"))
    assert page.ref.lang == "jp"
    assert page.name == "クヌギダマ"
    assert page.set_name == "Violet ex"
    assert page.illustrator == "Shigenori Negishi"
    assert page.image_url == (
        "https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com/tpc/SV1V/SV1V_1_R_JP_LG.png"
    )
    assert [p.ref for p in page.int_prints] == [CardRef("en", "SVI", "1")]


def test_parse_jp_card_illustrator_for_alt_art() -> None:
    page = parse_card_page(load("jp_SV10_114.html"), ref_hint=CardRef("jp", "SV10", "114"))
    assert page.name == "ロケット団のミュウツーex"
    assert page.illustrator == "aky CG Works"


def test_parse_promo_page() -> None:
    page = parse_card_page(load("en_SVP_1.html"), ref_hint=CardRef("en", "SVP", "1"))
    assert page.name == "Sprigatito"
    assert [p.ref for p in page.jp_prints] == [CardRef("jp", "SVP", "213")]


def test_image_url_builders() -> None:
    assert jp_image_url(CardRef("jp", "M2a", "1")).endswith("/tpc/M2a/M2a_1_R_JP_LG.png")
    assert en_image_url(CardRef("en", "SVI", "1")).endswith("/tpci/SVI/SVI_001_R_EN_LG.png")


def test_parse_rejects_non_card_page() -> None:
    with pytest.raises(ValueError):
        parse_card_page("<html><head><title>Limitless</title></head></html>")


def test_parse_jp_set_index_reads_release_dates() -> None:
    from ptcgl.data.limitless import parse_jp_set_index

    dates = parse_jp_set_index(load("jp_sets.html"))
    assert dates["SLD"] == "2022-02-25"
    assert dates["SV1V"] == "2023-01-20"
    assert dates["M6a"] == "2026-09-16"
    assert "MP" not in dates  # プロモは発売日の欄が空
    with pytest.raises(ValueError):
        parse_jp_set_index("<html><body><p>maintenance</p></body></html>")


def test_name_variants_strip_glued_suffix_only_from_last_word() -> None:
    from ptcgl.data.limitless import name_variants

    # 詳細表示で "Oricorio ex" が "Oricorioe" / "Oricorioex" と読まれる
    assert name_variants("Oricorioe") == ["Oricorioe", "Oricorio"]
    assert name_variants("Oricorioex") == ["Oricorioex", "Oricorio"]
    assert name_variants("Team Rockets Mewtwoex") == [
        "Team Rockets Mewtwoex",
        "Team Rockets Mewtwo",
    ]
    # 空白で分かれた "ex" や、語尾の無い名前には手を付けない
    assert name_variants("Charizard ex") == ["Charizard ex"]
    assert name_variants("Pikachu") == ["Pikachu"]

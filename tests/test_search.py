from pathlib import Path

from ptcgl.data.limitless import CardRef, build_search_query, parse_search_results, search_url

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def test_build_search_query() -> None:
    q = build_search_query("Riolu", 80, ["Accelerating Stab"])
    assert q == "Riolu hp:80 text:Accelerating_Stab unique:cards"
    assert search_url(q).startswith("https://limitlesstcg.com/cards?q=Riolu+hp%3A80")


def test_parse_search_results_charmander() -> None:
    refs, _ = parse_search_results(
        (FIXTURES / "search_charmander_hp80.html").read_text(encoding="utf-8")
    )
    assert set(refs) == {
        CardRef("en", "ASC", "20"),
        CardRef("en", "PFL", "11"),
        CardRef("en", "MEP", "38"),
    }


def test_parse_search_results_riolu_unique() -> None:
    refs, _ = parse_search_results(
        (FIXTURES / "search_riolu_hp80.html").read_text(encoding="utf-8")
    )
    assert refs == [CardRef("en", "ASC", "112")]

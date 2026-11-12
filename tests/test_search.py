from pathlib import Path

from ptcgl.data.cards import CardRepository
from ptcgl.data.limitless import (
    CardRef,
    build_search_query,
    normalize_search_text,
    parse_search_results,
    search_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def test_build_search_query() -> None:
    q = build_search_query("Riolu", 80, ["Accelerating Stab"])
    assert q == "Riolu hp:80 text:Accelerating_Stab unique:cards"
    assert search_url(q).startswith("https://limitlesstcg.com/cards?q=Riolu+hp%3A80")


def test_normalize_search_text_restores_spaces_lost_by_ocr() -> None:
    assert normalize_search_text("MegaLucario") == "Mega Lucario"
    assert normalize_search_text("LunarCycle") == "Lunar Cycle"
    assert normalize_search_text("Complex Poison") == "Complex Poison"
    assert normalize_search_text("Charizard ex") == "Charizard ex"
    assert normalize_search_text("Mega Lucario") == "Mega Lucario"
    assert normalize_search_text("Lunatone") == "Lunatone"
    assert normalize_search_text("HP") == "HP"
    assert normalize_search_text("Team Rockets Mewtwo ex") == "Team Rockets Mewtwo ex"


def _repo_with_results(
    tmp_path: Path, pages: dict[str, list[CardRef]]
) -> tuple[CardRepository, list[str]]:
    """クエリ文字列 → 検索結果 を返す偽の Limitless。実際に投げたクエリも記録する。"""
    import httpx

    from ptcgl.data.cache import CachedHttpClient

    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("q", "")
        queries.append(q)
        refs = pages.get(q, [])
        rows = "".join(
            f'<tr><td><a href="/cards/{r.set_code}/{r.number}">x</a></td></tr>' for r in refs
        )
        body = f"<html><body><p>{len(refs)} cards found</p><table>{rows}</table></body></html>"
        return httpx.Response(200, content=body.encode("utf-8"))

    http = CachedHttpClient(tmp_path / "cache", min_interval=0)
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    return CardRepository(http, tmp_path / "images"), queries


def test_search_english_uses_name_and_hp_first(tmp_path: Path) -> None:
    repo, queries = _repo_with_results(
        tmp_path, {"Lunatone hp:110 unique:cards": [CardRef("en", "ASC", "105")]}
    )
    # 1 件に絞れたらワザ名の検索はしない（OCR の揺れが大きい条件を投げない）
    refs = repo.search_english("Lunatone", 110, ["LunarCycle", "Abnry"])
    assert refs == [CardRef("en", "ASC", "105")]
    assert queries == ["Lunatone hp:110 unique:cards"]


def test_search_english_narrows_with_terms_only_when_ambiguous(tmp_path: Path) -> None:
    a, b = CardRef("en", "ASC", "113"), CardRef("en", "MEG", "1")
    repo, queries = _repo_with_results(
        tmp_path,
        {
            "Mega Lucario hp:340 unique:cards": [a, b],
            "Mega Lucario hp:340 text:Aura_Jab unique:cards": [a],
        },
    )
    # OCR で空白が落ちた名前・ワザ名は直してから投げる。複数件のときだけワザ名で絞る
    assert repo.search_english("MegaLucario", 340, ["AuraJab"]) == [a]
    assert queries == [
        "Mega Lucario hp:340 unique:cards",
        "Mega Lucario hp:340 text:Aura_Jab unique:cards",
    ]


def test_search_english_keeps_base_results_when_terms_miss(tmp_path: Path) -> None:
    a, b = CardRef("en", "ASC", "113"), CardRef("en", "MEG", "1")
    repo, queries = _repo_with_results(tmp_path, {"Mega Lucario hp:340 unique:cards": [a, b]})
    # ワザ名の読み違いで 0 件なら条件を 1 つずつ外し、それでも駄目なら名前と HP の結果を返す
    assert repo.search_english("Mega Lucario", 340, ["EvolvesfromRiolu", "Clefairy"]) == [a, b]
    assert len(queries) == 3


def test_search_english_drops_uncertain_terms_until_it_narrows(tmp_path: Path) -> None:
    a, b = CardRef("en", "ASC", "113"), CardRef("en", "MEG", "1")
    repo, queries = _repo_with_results(
        tmp_path,
        {
            "Mega Lucario hp:340 unique:cards": [a, b],
            "Mega Lucario hp:340 text:Aura_Jab unique:cards": [a],
        },
    )
    # 末尾（最も不確か）の条件 "Abnry" を外した 2 回目の絞り込みで 1 件になる
    assert repo.search_english("Mega Lucario", 340, ["Aura Jab", "Abnry"]) == [a]
    assert len(queries) == 3


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


def test_search_english_retries_without_glued_suffix(tmp_path: Path) -> None:
    a = CardRef("en", "PFL", "18")
    repo, queries = _repo_with_results(tmp_path, {"Oricorio hp:190 unique:cards": [a]})
    # 詳細表示で "Oricorio ex" が "Oricorioex" と読まれる。0 件なら末尾の ex を外して引き直す
    assert repo.search_english("Oricorioex", 190, ["Excited Turbo"]) == [a]
    assert queries == ["Oricorioex hp:190 unique:cards", "Oricorio hp:190 unique:cards"]
    # 外せる語尾が無ければ 1 回だけ
    assert repo.search_english("Pikachu", 60, []) == []
    assert queries[2:] == ["Pikachu hp:60 unique:cards"]

"""CardRepository の照合ロジック（ネットワーク不使用）。"""

from pathlib import Path

import httpx
import pytest

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.cards import CardRepository, rank_candidates
from ptcgl.data.limitless import CardRef, PrintEntry
from ptcgl.data.mapping import MappingStore

FIXTURES = Path(__file__).parent / "fixtures" / "html"

# URL → フィクスチャ。未登録の URL は 404 を返す
ROUTES = {
    "https://limitlesstcg.com/cards/SVI/1": "en_SVI_1.html",
    "https://limitlesstcg.com/cards/jp/SV1V/1": "jp_SV1V_1.html",
    "https://limitlesstcg.com/cards/jp/SVD/1": "jp_SVD_1.html",
    "https://limitlesstcg.com/cards/SVI/245": "en_SVI_245.html",
    "https://limitlesstcg.com/cards/jp/SV1S/101": "jp_SV1S_101.html",
    "https://limitlesstcg.com/cards/DRI/81": "en_DRI_81.html",
    "https://limitlesstcg.com/cards/jp/SV10/114": "jp_SV10_114.html",
    "https://limitlesstcg.com/cards/jp": "jp_sets.html",  # 日本語セット一覧（発売日）
}


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url in ROUTES:
        return httpx.Response(200, content=(FIXTURES / ROUTES[url]).read_bytes())
    if url.endswith(".png"):
        return httpx.Response(200, content=b"\x89PNG fake")
    return httpx.Response(404)


@pytest.fixture
def repo(tmp_path: Path) -> CardRepository:
    http = CachedHttpClient(tmp_path / "cache", min_interval=0)
    http._client = httpx.Client(transport=httpx.MockTransport(_handler))
    return CardRepository(http, tmp_path / "images", MappingStore(tmp_path / "map.sqlite3"))


def test_rank_candidates_prefers_main_expansions_then_lower_numbers() -> None:
    entries = (
        PrintEntry(CardRef("jp", "MC", "290"), "Start Deck 100"),
        PrintEntry(CardRef("jp", "SV10", "114"), "Glory of Team Rocket"),
        PrintEntry(CardRef("jp", "MP", "43"), "Promo"),
        PrintEntry(CardRef("jp", "SV10", "39"), "Glory of Team Rocket"),
        PrintEntry(CardRef("jp", "M2a", "63"), "Mega Dream ex"),
    )
    ranked = [e.ref.key for e in rank_candidates(entries)]
    assert ranked == ["jp:SV10:39", "jp:M2a:63", "jp:SV10:114", "jp:MP:43", "jp:MC:290"]


def test_find_japanese_same_illustrator(repo: CardRepository) -> None:
    result = repo.find_japanese(CardRef("en", "SVI", "1"))
    assert result.best is not None
    assert result.best.ref == CardRef("jp", "SV1V", "1")
    assert result.best.name_ja == "クヌギダマ"
    assert result.same_art is True
    assert result.should_auto_show
    assert [c.ref for c in result.candidates] == [
        CardRef("jp", "SV1V", "1"),
        CardRef("jp", "SVD", "1"),
    ]


def test_find_japanese_saves_auto_mapping_and_reuses_it(repo: CardRepository) -> None:
    first = repo.find_japanese(CardRef("en", "SVI", "1"))
    assert first.source == "auto"
    second = repo.find_japanese(CardRef("en", "SVI", "1"))
    assert second.source == "cache"
    assert second.best is not None and second.best.ref == first.best.ref


def test_find_japanese_skips_pages_that_fail(repo: CardRepository) -> None:
    # SV10/114 だけフィクスチャがあり、それ以前の候補（SV10/39 等）は 404 になる
    result = repo.find_japanese(CardRef("en", "DRI", "81"))
    assert result.best is not None
    assert result.best.ref == CardRef("jp", "SV10", "114")
    assert result.same_art is True
    assert any(c.ref == CardRef("jp", "SV10", "39") for c in result.candidates)


def test_manual_confirm_overrides_auto(repo: CardRepository) -> None:
    repo.find_japanese(CardRef("en", "SVI", "1"))
    alt = repo.get_japanese_card(CardRef("jp", "SVD", "1"))
    repo.confirm(CardRef("en", "SVI", "1"), alt, same_art=True)
    result = repo.find_japanese(CardRef("en", "SVI", "1"))
    assert result.source == "manual"
    assert result.best is not None and result.best.ref == CardRef("jp", "SVD", "1")


def test_download_image_writes_under_images_dir(repo: CardRepository, tmp_path: Path) -> None:
    card = repo.get_japanese_card(CardRef("jp", "SV1V", "1"))
    downloaded = repo.download_image(card)
    assert downloaded.image_path == tmp_path / "images" / "jp" / "SV1V_1.png"
    assert downloaded.image_path.exists()


def test_same_illustrator_ignores_case_and_spacing() -> None:
    from ptcgl.data.cards import same_illustrator

    assert same_illustrator("Ryuta Fuse", "ryuta fuse")
    assert same_illustrator("aky CG Works", "AKY_CG-Works")
    assert not same_illustrator("Ryuta Fuse", "GOSSAN")
    assert not same_illustrator(None, "GOSSAN")
    assert not same_illustrator("", "")


def test_rank_candidates_prefers_prints_released_just_before_the_english_set() -> None:
    entries = (
        PrintEntry(CardRef("jp", "SV1a", "95"), "Triplet Beat"),
        PrintEntry(CardRef("jp", "SEK", "17"), "Starter Set"),  # 発売日不明
        PrintEntry(CardRef("jp", "S9", "97"), "Star Birth"),
        PrintEntry(CardRef("jp", "SLD", "20"), "Starter Set Darkrai VSTAR"),
        PrintEntry(CardRef("jp", "SV4a", "181"), "Shiny Treasure ex"),
        PrintEntry(CardRef("jp", "MP", "144"), "Promo"),  # 発売日不明
    )
    releases = {
        "SV1a": "2023-03-10",
        "S9": "2022-01-14",
        "SLD": "2022-02-25",
        "SV4a": "2023-12-01",
    }
    # BRS（2022-02-25）: 同日の SLD → 直前の S9 → 後に出た SV1a, SV4a → 発売日不明は番号順
    ranked = [e.ref.key for e in rank_candidates(entries, "2022-02-25", releases)]
    assert ranked == [
        "jp:SLD:20",
        "jp:S9:97",
        "jp:SV1a:95",
        "jp:SV4a:181",
        "jp:SEK:17",
        "jp:MP:144",
    ]
    # 英語版の発売日が分からなければ従来の順（通常拡張 → その他、番号順）
    assert [e.ref.key for e in rank_candidates(entries, None, releases)][:2] == [
        "jp:SV1a:95",
        "jp:SV4a:181",
    ]


def _recording_repo(tmp_path: Path, routes: dict[str, str]) -> tuple[CardRepository, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url in routes:
            return httpx.Response(200, content=(FIXTURES / routes[url]).read_bytes())
        if url.endswith(".png"):
            return httpx.Response(200, content=b"\x89PNG fake")
        return httpx.Response(404)

    http = CachedHttpClient(tmp_path / "cache", min_interval=0)
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    repo = CardRepository(http, tmp_path / "images", MappingStore(tmp_path / "map.sqlite3"))
    return repo, requested


def test_find_japanese_fetches_candidates_in_release_order(tmp_path: Path) -> None:
    repo, requested = _recording_repo(tmp_path, ROUTES)
    # DRI（2025-05-30）の JP. Prints: MP/43, SV10/39, 114, 125, 130, M2a/63, 237, MC/290。
    # 直前に出た SV10（2025-04-18）から順に取り、後に出た M2a（2025-11-28）は一致後なので取らない
    result = repo.find_japanese(CardRef("en", "DRI", "81"))
    assert result.best is not None and result.best.ref == CardRef("jp", "SV10", "114")
    index = "https://limitlesstcg.com/cards/jp"
    jp_pages = [u for u in requested if u.startswith(index + "/")]
    assert jp_pages[:2] == [index + "/SV10/39", index + "/SV10/114"]
    assert not any("/M2a/" in u for u in requested)
    assert requested.count(index) == 1
    # 2 回目は保存済みの対応を使うので、一覧も候補ページも取りに行かない
    before = len(requested)
    assert repo.find_japanese(CardRef("en", "DRI", "81")).source == "cache"
    assert len(requested) == before


def test_find_japanese_falls_back_when_set_index_is_unavailable(tmp_path: Path) -> None:
    routes = {k: v for k, v in ROUTES.items() if not k.endswith("/cards/jp")}
    repo, requested = _recording_repo(tmp_path, routes)
    # 一覧が取れなくても（404）、従来の順で照合を続ける
    result = repo.find_japanese(CardRef("en", "SVI", "1"))
    assert result.best is not None and result.best.ref == CardRef("jp", "SV1V", "1")
    assert result.same_art is True
    assert "https://limitlesstcg.com/cards/jp" in requested

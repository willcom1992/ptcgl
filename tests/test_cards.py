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

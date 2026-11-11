"""Pipeline の英語カード特定（セット衝突の解決）。OCR と画像は使わない。"""

from pathlib import Path

import httpx
import pytest

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.cards import CardRepository
from ptcgl.data.limitless import CardRef
from ptcgl.pipeline import Pipeline
from ptcgl.recognition.card import RecognizedCard

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == "https://limitlesstcg.com/cards/DRI/81":
        return httpx.Response(200, content=(FIXTURES / "en_DRI_81.html").read_bytes())
    if url == "https://limitlesstcg.com/cards/PAR/81":
        # 同じ分母 182 の別セット。名前が違うページとして SVI/1 を流用
        return httpx.Response(200, content=(FIXTURES / "en_SVI_1.html").read_bytes())
    return httpx.Response(404)


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    http = CachedHttpClient(tmp_path / "cache", min_interval=0)
    http._client = httpx.Client(transport=httpx.MockTransport(_handler))
    repo = CardRepository(http, tmp_path / "images")
    return Pipeline(repo, ocr=object(), detector=None)  # type: ignore[arg-type]


def test_resolve_with_explicit_set_code(pipeline: Pipeline) -> None:
    ref, _ = pipeline.resolve_en_ref(RecognizedCard("Pineco", "SVI", "1", 198))
    assert ref == CardRef("en", "SVI", "1")


def test_resolve_unique_total(pipeline: Pipeline) -> None:
    # 分母 193 は PAL（Paldea Evolved）だけ。ページ取得なしで決まる
    ref, reason = pipeline.resolve_en_ref(RecognizedCard("Sprigatito", None, "1", 193))
    assert ref == CardRef("en", "PAL", "1")
    assert "一意" in reason


def test_resolve_colliding_total_by_name(pipeline: Pipeline) -> None:
    ref, reason = pipeline.resolve_en_ref(RecognizedCard("Team Rockets Mewtwo ex", None, "81", 182))
    assert ref == CardRef("en", "DRI", "81")
    assert "DRI" in reason


def test_resolve_colliding_total_without_name_fails(pipeline: Pipeline) -> None:
    ref, reason = pipeline.resolve_en_ref(RecognizedCard(None, None, "81", 182))
    assert ref is None
    assert "複数" in reason


def test_resolve_without_number_fails(pipeline: Pipeline) -> None:
    ref, _ = pipeline.resolve_en_ref(RecognizedCard("Pineco", None, None))
    assert ref is None

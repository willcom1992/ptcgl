"""レビューで見つかった不具合の回帰テスト。"""

from pathlib import Path

import httpx
import pytest

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.cards import CardRepository
from ptcgl.data.limitless import CardRef
from ptcgl.data.mapping import MappingRecord, MappingStore
from ptcgl.pipeline import Pipeline, _normalize_set_code
from ptcgl.recognition.anchor import find_anchor
from ptcgl.recognition.card import extract_card_info, parse_set_number
from ptcgl.recognition.ocr import OcrLine

FIXTURES = Path(__file__).parent / "fixtures" / "html"
W, H = 1600, 900


def _line(text: str, x: float, y: float, w: float, h: float, conf: float = 0.95) -> OcrLine:
    x0, y0, x1, y1 = x * W, y * H, (x + w) * W, (y + h) * H
    return OcrLine(text=text, confidence=conf, box=((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


# --- 指摘 1: 番号行に "MEG EN" が同居しても読める ---
@pytest.mark.parametrize("text", ["MEG EN 076/132", "MEGEN076/132", "MEG EN 076 / 132"])
def test_number_anchor_with_set_and_en_token(text: str) -> None:
    anchor = find_anchor([_line(text, 0.4, 0.8, 0.08, 0.025)], W, H)
    assert anchor is not None and anchor.kind == "number"
    assert anchor.set_code == "MEG" and anchor.number == "76" and anchor.total == 132


# --- 指摘 5: Trainer Gallery 番号 ---
def test_number_anchor_trainer_gallery() -> None:
    anchor = find_anchor([_line("TG05/TG30", 0.4, 0.8, 0.08, 0.025)], W, H)
    assert anchor is not None and anchor.kind == "number"
    assert anchor.number == "TG05" and anchor.total == 30 and anchor.number_prefix == "TG"


# --- 指摘 2: セットコードの補正は先頭からの部分文字列も試す ---
@pytest.mark.parametrize(
    ("raw", "expected"), [("MEGEN", "MEG"), ("DRIEN", "DRI"), ("ODRI", "DRI"), ("EN", None)]
)
def test_normalize_set_code(raw: str, expected: str | None) -> None:
    assert _normalize_set_code(raw) == expected


# --- 指摘 6: "EN" 単独をセットコードにしない。既知コードを含む行を優先 ---
def test_parse_set_number_rejects_en_token() -> None:
    assert parse_set_number("EN 076/132") is None


def test_extract_prefers_known_set_code_over_split_lines() -> None:
    lines = [
        _line("Pikachu", 0.0, 0.05, 0.3, 0.05),
        _line("MEG", 0.0, 0.9, 0.05, 0.02),
        _line("EN 076/132", 0.06, 0.9, 0.1, 0.02),
    ]
    rec = extract_card_info(lines, frame_height=H)
    assert rec.set_code == "MEG" and rec.number == "76" and rec.total == 132


# --- 指摘 4 / 7: 画像やページが取れなくても照合結果を失わない ---
def _handler_factory(fail_png: bool, fail_jp_page: bool):
    routes = {
        "https://limitlesstcg.com/cards/SVI/1": "en_SVI_1.html",
        "https://limitlesstcg.com/cards/jp/SV1V/1": "jp_SV1V_1.html",
        "https://limitlesstcg.com/cards/jp/SVD/1": "jp_SVD_1.html",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".png"):
            return httpx.Response(404 if fail_png else 200, content=b"png")
        if fail_jp_page and "/cards/jp/" in url:
            return httpx.Response(404)
        if url in routes:
            return httpx.Response(200, content=(FIXTURES / routes[url]).read_bytes())
        return httpx.Response(404)

    return handler


def _repo(tmp_path: Path, handler) -> CardRepository:
    http = CachedHttpClient(tmp_path / "cache", min_interval=0, max_retries=0)
    http._client = httpx.Client(transport=httpx.MockTransport(handler))
    return CardRepository(http, tmp_path / "images", MappingStore(tmp_path / "map.sqlite3"))


def test_identify_ref_keeps_match_when_image_download_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _handler_factory(fail_png=True, fail_jp_page=False))
    pipeline = Pipeline(repo, ocr=object())  # type: ignore[arg-type]
    progress: list[str] = []
    pipeline.on_progress = progress.append
    result = pipeline.identify_ref(CardRef("en", "SVI", "1"))
    assert result.match is not None and result.match.best is not None
    assert result.match.best.image_path is None
    assert len(result.match.candidates) == 2
    assert result.error is None
    # 取得に入る前に状態を知らせる（画像が無い結果はキャッシュされないので 2 回目も知らせる）
    assert progress == ["SVI #1 を特定。日本語版を取得中..."]
    pipeline.identify_ref(CardRef("en", "SVI", "1"))
    assert len(progress) == 2


def test_saved_mapping_survives_missing_jp_page(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _handler_factory(fail_png=False, fail_jp_page=True))
    repo._mapping.put(
        MappingRecord(
            en=CardRef("en", "SVI", "1"),
            jp=CardRef("jp", "SV1V", "1"),
            same_art=True,
            source="auto",
        )
    )
    result = repo.find_japanese(CardRef("en", "SVI", "1"))
    assert result.best is not None and result.best.ref == CardRef("jp", "SV1V", "1")
    assert result.source == "cache"


# --- 指摘 13: 手動選択を自動結果で上書きしない ---
def test_auto_match_does_not_override_manual(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _handler_factory(fail_png=False, fail_jp_page=False))
    alt = repo.get_japanese_card(CardRef("jp", "SVD", "1"))
    repo.confirm(CardRef("en", "SVI", "1"), alt, same_art=False)
    repo.find_japanese(CardRef("en", "SVI", "1"), use_saved=False)
    saved = repo._mapping.get(CardRef("en", "SVI", "1"))
    assert saved is not None and saved.source == "manual" and saved.jp == CardRef("jp", "SVD", "1")


# --- 指摘 10: 最後の試行の後は待たない ---
def test_retry_does_not_sleep_after_last_attempt(tmp_path: Path, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("ptcgl.data.cache.time.sleep", lambda s: sleeps.append(s))
    client = CachedHttpClient(tmp_path, min_interval=0, max_retries=2)
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, content=b"x"))
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.get_bytes("https://example.com/a")
    assert sleeps == [1.0, 2.0]

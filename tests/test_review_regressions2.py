"""2 回目のレビューで見つかった不具合の回帰テスト。"""

import json
import time
from pathlib import Path

import httpx
import pytest

from ptcgl.config import AppConfig, load_config, save_config
from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.cards import CardRepository
from ptcgl.data.limitless import CardRef, build_search_query, parse_print_href
from ptcgl.recognition.card import _clean_name


# --- 指摘 2: 構造が壊れた設定でも既定値で起動する ---
@pytest.mark.parametrize(
    "payload",
    [
        '{"capture": {"card_region_ratio": [0.1, 0.2]}}',
        '{"target_window": "oops"}',
        '["not", "an", "object"]',
        '{"overlay": {"ratio_pos": "x"}}',
    ],
)
def test_load_config_tolerates_broken_structures(tmp_path: Path, payload: str) -> None:
    p = tmp_path / "config.json"
    p.write_text(payload, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.capture.detection_mode == "auto"


# --- 指摘 14: 既定のパスは書き出さない ---
def test_default_paths_not_persisted(tmp_path: Path) -> None:
    cfg = AppConfig()
    path = save_config(cfg, tmp_path / "config.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "cache_dir" not in data and "images_dir" not in data and "db_path" not in data
    cfg.images_dir = tmp_path / "img"
    path = save_config(cfg, tmp_path / "config.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["images_dir"] == str(tmp_path / "img")
    assert "cache_dir" not in data


# --- 指摘 9: CardRef は URL・ファイル名に安全な値しか受け付けない ---
@pytest.mark.parametrize(
    ("set_code", "number"), [("..\\..\\x", "1"), ("SVI", "1?a=b#c"), ("SV I", "1"), ("", "1")]
)
def test_cardref_rejects_unsafe_tokens(set_code: str, number: str) -> None:
    with pytest.raises(ValueError):
        CardRef("en", set_code, number)


def test_parse_print_href_ignores_unsafe() -> None:
    assert parse_print_href("/cards/jp/SV1V/1", "en") == CardRef("jp", "SV1V", "1")
    assert parse_print_href("/cards/jp/SV1V/1?x=../y", "en") == CardRef("jp", "SV1V", "1")
    assert parse_print_href("/cards/jp/SV 1V/1", "en") is None


# --- 指摘 10: é を残す。先頭の演算子文字を落とす ---
def test_clean_name_keeps_accents() -> None:
    assert _clean_name("Pokémon Tool: Rescue Board") == "Pokémon Tool: Rescue Board"


def test_search_query_strips_leading_operators() -> None:
    assert build_search_query("-:Riolu", 80, None, unique=False) == "Riolu hp:80"


# --- 指摘 5: 403 はリトライしない ---
def test_client_errors_are_not_retried(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr("ptcgl.data.cache.time.sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403)

    client = CachedHttpClient(tmp_path, min_interval=0, max_retries=2)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        client.get_bytes("https://example.com/a")
    assert len(calls) == 1


# --- 指摘 4: 解析できないページはキャッシュに残さない。古い HTML は取り直す ---
def test_unparseable_page_is_not_kept_in_cache(tmp_path: Path) -> None:
    client = CachedHttpClient(tmp_path / "cache", min_interval=0, max_retries=0)
    client._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"<html><title>Maintenance</title></html>")
        )
    )
    repo = CardRepository(client, tmp_path / "images")
    with pytest.raises(ValueError):
        repo.get_card_page(CardRef("en", "SVI", "1"))
    assert not client.is_cached("https://limitlesstcg.com/cards/SVI/1")


def test_max_age_refetches_stale_html(tmp_path: Path) -> None:
    calls: list[int] = []
    client = CachedHttpClient(tmp_path, min_interval=0)
    client._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (calls.append(1), httpx.Response(200, content=b"x"))[1]
        )
    )
    url = "https://example.com/page"
    client.get_text(url, max_age=60)
    client.get_text(url, max_age=60)
    assert len(calls) == 1
    old = time.time() - 120
    import os

    os.utime(client._path_for(url), (old, old))
    client.get_text(url, max_age=60)
    assert len(calls) == 2


# --- 指摘 11: 画像は images_dir にだけ保存する ---
def test_get_file_does_not_duplicate_into_generic_cache(tmp_path: Path) -> None:
    client = CachedHttpClient(tmp_path / "cache", min_interval=0)
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"png"))
    )
    dest = client.get_file("https://example.com/a.png", tmp_path / "images" / "a.png")
    assert dest.read_bytes() == b"png"
    assert not client.is_cached("https://example.com/a.png")

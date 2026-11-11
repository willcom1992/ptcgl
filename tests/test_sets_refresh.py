"""セット表のオンライン更新（新弾のたびにアプリを配り直さなくて済むようにする仕組み）。"""

from __future__ import annotations

import copy
import json
import os
import time
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import httpx
import pytest

from ptcgl.data import sets
from ptcgl.data.cache import CachedHttpClient

NEW_SET = {
    "code": "ZZZ",
    "tcgdex_id": "zz01",
    "name": "Future Set",
    "official": 777,
    "total": 800,
    "number_prefix": "",
    "release_date": "2026-12-01",
}
OLDER = "2020-01-01T00:00:00Z"


def bundled_doc() -> dict:
    text = resources.files("ptcgl.data").joinpath("en_sets.json").read_text(encoding="utf-8")
    return json.loads(text)


def newer_doc(extra: list[dict] | None = None) -> dict:
    """同梱版より新しい表（updated を今の時刻にして、行を足す）。"""
    doc = bundled_doc()
    doc["updated"] = time.strftime(sets.UPDATED_FORMAT, time.gmtime())
    assert doc["updated"] > bundled_doc()["updated"]
    doc["sets"] += [NEW_SET] if extra is None else extra
    return doc


@pytest.fixture(autouse=True)
def _reset_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sets, "_active", None)  # テスト後は元の表に戻る


def make_client(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> tuple[CachedHttpClient, list[str]]:
    calls: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    client = CachedHttpClient(tmp_path, min_interval=0.0)
    client._client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return client, calls


def serve(body: object) -> Callable[[httpx.Request], httpx.Response]:
    content = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return lambda request: httpx.Response(200, content=content)


def offline(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("offline", request=request)


def expire_cache(client: CachedHttpClient) -> None:
    old = time.time() - 2 * sets.REMOTE_MAX_AGE
    os.utime(client._path_for(sets.REMOTE_SETS_URL), (old, old))


def test_refresh_adopts_table_with_new_set(tmp_path: Path) -> None:
    assert sets.sets_by_official_total(777) == []
    client, calls = make_client(tmp_path, serve(newer_doc()))

    assert sets.refresh_en_sets(client) is True

    assert [s.code for s in sets.sets_by_official_total(777)] == ["ZZZ"]
    assert sets.find_set("zzz") is not None
    assert "ZZZ" in sets.known_codes()
    assert calls == [sets.REMOTE_SETS_URL]


def test_refresh_adopts_correction_without_new_set(tmp_path: Path) -> None:
    """セットが増えていなくても、新しい版なら値の修正が届く。"""
    doc = newer_doc(extra=[])
    svi = next(r for r in doc["sets"] if r["code"] == "SVI")
    svi["official"] = 199
    client, _ = make_client(tmp_path, serve(doc))

    assert sets.refresh_en_sets(client) is True
    found = sets.find_set("SVI")
    assert found is not None and found.official == 199


def test_refresh_fetches_once_per_max_age(tmp_path: Path) -> None:
    client, calls = make_client(tmp_path, serve(newer_doc()))
    sets.refresh_en_sets(client)
    sets.refresh_en_sets(client)
    assert len(calls) == 1


def test_refresh_keeps_bundled_when_same_version(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, serve(bundled_doc()))
    assert sets.refresh_en_sets(client) is False


def test_refresh_ignores_table_not_newer_than_bundled(tmp_path: Path) -> None:
    """アプリ更新前の古いキャッシュや、手元で先に直した表を、古い取得分で上書きしない。"""
    doc = bundled_doc()
    doc["updated"] = OLDER
    doc["sets"] = copy.deepcopy(doc["sets"]) + [NEW_SET]
    next(r for r in doc["sets"] if r["code"] == "SVI")["official"] = 1
    client, _ = make_client(tmp_path, serve(doc))
    before = sets.load_en_sets()

    assert sets.refresh_en_sets(client) is False
    assert sets.load_en_sets() == before


def test_refresh_keeps_bundled_when_offline(tmp_path: Path) -> None:
    client, calls = make_client(tmp_path, offline)
    before = sets.load_en_sets()

    assert sets.refresh_en_sets(client) is False

    assert sets.load_en_sets() == before
    assert len(calls) == 1  # 起動を遅らせないよう、リトライしない


def test_refresh_keeps_bundled_on_404(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, lambda request: httpx.Response(404))
    assert sets.refresh_en_sets(client) is False
    assert sets.find_set("SVI") is not None


@pytest.mark.parametrize(
    "failure", [offline, serve(b"<html>Sign in to the network</html>"), serve(b"[]")]
)
def test_refresh_uses_previous_download_when_fetch_fails(
    tmp_path: Path, failure: Callable[[httpx.Request], httpx.Response]
) -> None:
    """翌日の起動で取得に失敗しても（オフライン・ログイン画面）、前回の取得分は消えない。"""
    client, _ = make_client(tmp_path, serve(newer_doc()))
    sets.refresh_en_sets(client)
    expire_cache(client)
    sets._active = None  # 再起動: 表は同梱版に戻っている
    client._client = httpx.Client(transport=httpx.MockTransport(failure))

    assert sets.refresh_en_sets(client) is True
    assert [s.code for s in sets.sets_by_official_total(777)] == ["ZZZ"]
    assert client.is_cached(sets.REMOTE_SETS_URL)


def _broken(**changes: object) -> dict:
    doc = newer_doc()
    doc["sets"][-1] = {**NEW_SET, **changes}
    return doc


def _oversized() -> dict:
    doc = newer_doc()
    doc["sets"][-1] = {**NEW_SET, "name": "x" * 1_000_001}
    return doc


BROKEN_TABLES: dict[str, object] = {
    "html": b"<html>Sign in to the network</html>",
    "not-utf8": b"\xff\xfe\x00",
    "legacy-list": bundled_doc()["sets"],
    "empty-sets": {"updated": "2026-11-12T00:00:00Z", "sets": []},
    "no-updated": {"sets": bundled_doc()["sets"]},
    "bad-updated": {**newer_doc(), "updated": "tomorrow"},
    "future-updated": {**newer_doc(), "updated": "2999-01-01T00:00:00Z"},
    "row-not-object": {**newer_doc(), "sets": ["SVI"]},
    "missing-field": {**newer_doc(), "sets": [{"code": "SVI"}]},
    "bad-code": _broken(code="../x"),
    "code-not-string": _broken(code=123),
    "official-string": _broken(official="many"),
    "official-bool": _broken(official=True),
    "official-too-big": _broken(official=12345),
    "bad-date": _broken(release_date="9999"),
    "duplicate-set": {**newer_doc(), "sets": [*newer_doc()["sets"], NEW_SET]},
    "only-one-set": {**newer_doc(), "sets": [NEW_SET]},
    "infinity": json.dumps(newer_doc()).replace("777", "Infinity").encode(),
    "deep-nesting": b"[" * 100_000,
    "oversized": _oversized(),
}


@pytest.mark.parametrize("body", BROKEN_TABLES.values(), ids=BROKEN_TABLES.keys())
def test_refresh_rejects_broken_table(tmp_path: Path, body: object) -> None:
    client, _ = make_client(tmp_path, serve(body))
    before = sets.load_en_sets()

    assert sets.refresh_en_sets(client) is False

    assert sets.load_en_sets() == before
    # 壊れた内容はキャッシュに残さず、次回の起動で取り直す
    assert not client.is_cached(sets.REMOTE_SETS_URL)


def test_oversized_table_is_rejected_by_size_alone() -> None:
    """大きさ以外は正しい表が、大きさの上限で弾かれること。"""
    data = json.dumps(_oversized()).encode("utf-8")
    assert sets.parse_set_table(data.decode("utf-8")).sets  # 形式は正しい
    with pytest.raises(ValueError, match="大きすぎます"):
        sets._parse_remote(data, sets._table())


def test_bundled_table_passes_remote_validation() -> None:
    """同梱版がリモート用の検証に通ること（通らないと push しても誰にも反映されない）。"""
    text = resources.files("ptcgl.data").joinpath("en_sets.json").read_text(encoding="utf-8")
    table = sets._parse_remote(text.encode("utf-8"), sets._table())
    assert len(table.sets) > 40


def test_refresh_does_not_raise_from_start_helper(tmp_path: Path) -> None:
    """GUI 起動時の裏スレッドが、取得結果にかかわらず終了すること。"""
    from ptcgl.app import start_sets_refresh

    client, _ = make_client(tmp_path, serve(newer_doc()))
    thread = start_sets_refresh(client)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert sets.find_set("ZZZ") is not None


def test_stale_cache_is_returned_only_when_requested(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, serve(b"v1"))
    url = "https://example.com/data.json"
    assert client.get_bytes(url, max_age=60) == b"v1"
    old = time.time() - 3600
    os.utime(client._path_for(url), (old, old))
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )

    assert client.get_bytes(url, max_age=60, stale_if_error=True, max_retries=0) == b"v1"
    with pytest.raises(httpx.HTTPStatusError):
        client.get_bytes(url, max_age=60, max_retries=0)


def test_validate_failure_does_not_overwrite_cache(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, serve(b"good"))
    url = "https://example.com/data.json"
    client.get_bytes(url)
    client._client = httpx.Client(transport=httpx.MockTransport(serve(b"bad")))

    def reject(content: bytes) -> None:
        raise ValueError("bad content")

    assert client.get_bytes(url, max_age=-1, stale_if_error=True, validate=reject) == b"good"
    with pytest.raises(ValueError):
        client.get_bytes(url, max_age=-1, validate=reject)
    assert client._path_for(url).read_bytes() == b"good"

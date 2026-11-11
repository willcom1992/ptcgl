from pathlib import Path

import httpx

from ptcgl.data.cache import CachedHttpClient


def test_get_bytes_uses_cache_on_second_call(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"image-bytes")

    client = CachedHttpClient(tmp_path)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    url = "https://example.com/card.png"
    assert client.get_bytes(url) == b"image-bytes"
    assert client.get_bytes(url) == b"image-bytes"
    assert calls == [url]
    client.close()

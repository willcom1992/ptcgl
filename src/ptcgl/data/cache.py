"""HTTP 取得結果のディスクキャッシュ。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx


class CachedHttpClient:
    """URL をキーにレスポンス本文をディスクへキャッシュする HTTP クライアント。"""

    def __init__(self, cache_dir: Path, timeout: float = 10.0) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / digest

    def get_bytes(self, url: str) -> bytes:
        path = self._path_for(url)
        if path.exists():
            return path.read_bytes()
        response = self._client.get(url)
        response.raise_for_status()
        path.write_bytes(response.content)
        return response.content

    def close(self) -> None:
        self._client.close()

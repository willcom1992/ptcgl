"""HTTP 取得結果のディスクキャッシュ。

外部サイト（Limitless, TCGdex）へのアクセスはすべてこのクライアントを経由し、
同一 URL の再取得を避ける。ネットワーク取得には最小間隔を設け、負荷をかけない。
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import httpx

USER_AGENT = "ptcgl-overlay/0.1 (personal, non-commercial card lookup tool; low request rate)"


class CachedHttpClient:
    """URL をキーにレスポンス本文をディスクへキャッシュする HTTP クライアント。"""

    def __init__(
        self,
        cache_dir: Path,
        timeout: float = 15.0,
        min_interval: float = 1.0,
        max_retries: int = 2,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._last_request_at = 0.0

    def _path_for(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / digest

    def is_cached(self, url: str) -> bool:
        return self._path_for(url).exists()

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _fetch(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(url)
                if response.status_code == 404:
                    response.raise_for_status()
                if response.status_code >= 500 or response.status_code == 429:
                    response.raise_for_status()
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500 and status != 429:
                    raise  # 404/403/400 等はリトライしても変わらない
                last_error = exc
            except httpx.TransportError as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(1.0 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def get_bytes(self, url: str, max_age: float | None = None) -> bytes:
        """キャッシュがあればそれを、無ければ取得して保存してから返す。

        `max_age`（秒）を指定すると、それより古いキャッシュは取り直す（HTML ページ向け。
        画像は不変なので None でよい）。
        """
        path = self._path_for(url)
        if path.exists() and not self._expired(path, max_age):
            return path.read_bytes()
        with self._lock:
            if path.exists() and not self._expired(path, max_age):
                return path.read_bytes()
            content = self._fetch(url)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
            return content

    @staticmethod
    def _expired(path: Path, max_age: float | None) -> bool:
        if max_age is None:
            return False
        try:
            return (time.time() - path.stat().st_mtime) > max_age
        except OSError:
            return True

    def invalidate(self, url: str) -> None:
        """解析できないページ（メンテナンス画面等）を保存してしまった場合に捨てる。"""
        try:
            self._path_for(url).unlink()
        except FileNotFoundError:
            pass

    def get_text(self, url: str, encoding: str = "utf-8", max_age: float | None = None) -> str:
        return self.get_bytes(url, max_age=max_age).decode(encoding, errors="replace")

    def get_file(self, url: str, dest: Path) -> Path:
        """URL の内容を dest に保存して返す（既存ならそのまま返す）。"""
        if dest.exists():
            return dest
        with self._file_lock:
            if dest.exists():
                return dest
            with self._lock:  # 汎用キャッシュには二重保存せず、dest だけに書く
                content = self._fetch(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(dest)
        return dest

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CachedHttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

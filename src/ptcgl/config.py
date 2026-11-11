"""アプリ設定。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ptcgl" / "cache"


@dataclass
class CaptureConfig:
    interval_ms: int = 1000
    # None なら PTCGL ウィンドウを自動検出。(left, top, width, height) で手動指定も可
    region: tuple[int, int, int, int] | None = None


@dataclass
class AppConfig:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    cache_dir: Path = field(default_factory=default_cache_dir)

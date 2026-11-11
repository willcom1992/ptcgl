"""アプリ設定。

保存先はユーザーごとの設定ディレクトリ（platformdirs）。固定パスは使わない。
領域やウィンドウ位置はすべて比率で保存し、解像度・DPI が変わっても意味が保たれるようにする。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

from ptcgl.recognition.detector import CardBox

log = logging.getLogger(__name__)

APP_NAME = "ptcgl"
SCHEMA_VERSION = 1


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.json"


def default_cache_dir() -> Path:
    return data_dir() / "cache"


def default_images_dir() -> Path:
    return data_dir() / "images"


def default_db_path() -> Path:
    return data_dir() / "mapping.sqlite3"


@dataclass
class TargetWindowConfig:
    title_pattern: str = "Pok[eé]mon TCG Live"
    process_name: str = "Pokemon TCG Live.exe"


@dataclass
class CaptureConfig:
    interval_ms: int = 1000
    detection_mode: str = "auto"  # "auto" | "manual"
    # 対象ウィンドウのクライアント領域に対する比率 (rx, ry, rw, rh)。None なら自動検出
    card_region_ratio: tuple[float, float, float, float] | None = None

    def manual_box(self) -> CardBox | None:
        if self.card_region_ratio is None:
            return None
        return CardBox(*self.card_region_ratio)


@dataclass
class OverlayConfig:
    monitor_name: str | None = None
    ratio_pos: tuple[float, float] | None = None  # モニターに対する比率
    ratio_size: tuple[float, float] = (0.22, 0.6)
    always_on_top: bool = True


@dataclass
class AppConfig:
    schema_version: int = SCHEMA_VERSION
    target_window: TargetWindowConfig = field(default_factory=TargetWindowConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    monitor_fingerprint: str | None = None
    needs_reconfirm: bool = False
    cache_dir: Path = field(default_factory=default_cache_dir)
    images_dir: Path = field(default_factory=default_images_dir)
    db_path: Path = field(default_factory=default_db_path)
    http_min_interval: float = 1.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # 既定のまま（ユーザー別ディレクトリ配下）のパスは書き出さない。別 PC へ設定を持ち出しても
        # 元ユーザーの絶対パスが残らないようにするため
        defaults = {
            "cache_dir": default_cache_dir(),
            "images_dir": default_images_dir(),
            "db_path": default_db_path(),
        }
        for key, default in defaults.items():
            if getattr(self, key) == default:
                d.pop(key)
            else:
                d[key] = str(d[key])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> AppConfig:
        cfg = cls()
        if not isinstance(d, dict):
            raise TypeError("設定ファイルのトップレベルはオブジェクトである必要があります")
        tw = d.get("target_window") or {}
        cfg.target_window = TargetWindowConfig(
            title_pattern=tw.get("title_pattern", cfg.target_window.title_pattern),
            process_name=tw.get("process_name", cfg.target_window.process_name),
        )
        cap = d.get("capture") or {}
        region = cap.get("card_region_ratio")
        if region is not None and len(region) != 4:
            region = None
        cfg.capture = CaptureConfig(
            interval_ms=int(cap.get("interval_ms", cfg.capture.interval_ms)),
            detection_mode=cap.get("detection_mode", cfg.capture.detection_mode),
            card_region_ratio=(
                (float(region[0]), float(region[1]), float(region[2]), float(region[3]))
                if region
                else None
            ),
        )
        ov = d.get("overlay") or {}
        pos = ov.get("ratio_pos")
        size = ov.get("ratio_size")
        cfg.overlay = OverlayConfig(
            monitor_name=ov.get("monitor_name"),
            ratio_pos=(float(pos[0]), float(pos[1])) if pos else None,
            ratio_size=(float(size[0]), float(size[1])) if size else cfg.overlay.ratio_size,
            always_on_top=bool(ov.get("always_on_top", True)),
        )
        cfg.monitor_fingerprint = d.get("monitor_fingerprint")
        cfg.needs_reconfirm = bool(d.get("needs_reconfirm", False))
        for key in ("cache_dir", "images_dir", "db_path"):
            if d.get(key):
                setattr(cfg, key, Path(d[key]))
        cfg.http_min_interval = float(d.get("http_min_interval", cfg.http_min_interval))
        return cfg

    def check_monitor_change(self, current_fingerprint: str) -> bool:
        """モニター構成が変わっていれば要再確認フラグを立てて True を返す。"""
        if self.monitor_fingerprint is None:
            self.monitor_fingerprint = current_fingerprint
            return False
        if self.monitor_fingerprint != current_fingerprint:
            self.needs_reconfirm = True
            self.monitor_fingerprint = current_fingerprint
            return True
        return False


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.exists():
        return AppConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig.from_dict(data)
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        log.warning("設定を読み込めないため既定値を使います: %s (%s)", path, exc)
        return AppConfig()


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path

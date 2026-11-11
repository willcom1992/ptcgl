from pathlib import Path

from ptcgl.config import AppConfig, load_config, save_config


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.capture.detection_mode = "manual"
    cfg.capture.card_region_ratio = (0.3, 0.1, 0.4, 0.8)
    cfg.overlay.ratio_pos = (0.7, 0.1)
    cfg.overlay.monitor_name = "\\\\.\\DISPLAY2"
    cfg.images_dir = tmp_path / "img"
    path = save_config(cfg, tmp_path / "config.json")

    loaded = load_config(path)
    assert loaded.capture.detection_mode == "manual"
    assert loaded.capture.card_region_ratio == (0.3, 0.1, 0.4, 0.8)
    assert loaded.capture.manual_box() is not None
    assert loaded.overlay.ratio_pos == (0.7, 0.1)
    assert loaded.overlay.monitor_name == "\\\\.\\DISPLAY2"
    assert loaded.images_dir == tmp_path / "img"


def test_load_config_missing_or_broken_returns_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "none.json").schema_version == 1
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_config(broken).capture.detection_mode == "auto"


def test_monitor_change_sets_reconfirm_flag() -> None:
    cfg = AppConfig()
    assert cfg.check_monitor_change("abc") is False
    assert cfg.check_monitor_change("abc") is False
    assert cfg.check_monitor_change("xyz") is True
    assert cfg.needs_reconfirm is True
    assert cfg.monitor_fingerprint == "xyz"

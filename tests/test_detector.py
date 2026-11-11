import numpy as np
import pytest

from ptcgl.recognition.detector import (
    CARD_ASPECT,
    CardBox,
    ContourCardDetector,
    ManualRegionDetector,
    crop,
)


def _frame_with_card(
    width: int, height: int, card_h_ratio: float = 0.6
) -> tuple[np.ndarray, CardBox]:
    """背景に 1 枚のカード形状（明るい矩形）を描いた合成フレームを作る。"""
    frame = np.full((height, width, 3), 40, dtype=np.uint8)
    ch = int(height * card_h_ratio)
    cw = int(ch * CARD_ASPECT)
    x = (width - cw) // 2
    y = (height - ch) // 2
    frame[y : y + ch, x : x + cw] = (230, 220, 210)
    return frame, CardBox.from_pixels(x, y, cw, ch, width, height)


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (2560, 1440), (3840, 2160)])
def test_contour_detector_finds_card_at_any_resolution(width: int, height: int) -> None:
    frame, expected = _frame_with_card(width, height)
    box = ContourCardDetector().detect(frame)
    assert box is not None
    assert box.rx == pytest.approx(expected.rx, abs=0.01)
    assert box.ry == pytest.approx(expected.ry, abs=0.01)
    assert box.rw == pytest.approx(expected.rw, abs=0.01)
    assert box.rh == pytest.approx(expected.rh, abs=0.01)


def test_contour_detector_returns_none_without_card() -> None:
    frame = np.full((720, 1280, 3), 40, dtype=np.uint8)
    assert ContourCardDetector().detect(frame) is None


def test_crop_and_manual_detector() -> None:
    frame, expected = _frame_with_card(1280, 720)
    det = ManualRegionDetector(expected)
    box = det.detect(frame)
    assert box == expected
    cropped = crop(frame, box)
    assert cropped.shape[0] == pytest.approx(720 * 0.6, abs=1)
    assert cropped.shape[1] == pytest.approx(720 * 0.6 * CARD_ASPECT, abs=1)

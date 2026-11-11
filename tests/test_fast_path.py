"""高速パス（文字検出 + 選択的な認識）のテスト。OCR モデルは使わず、検出と認識の結果を偽装する。"""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np
import pytest

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.cards import CardRepository
from ptcgl.data.limitless import CardRef
from ptcgl.pipeline import Pipeline
from ptcgl.recognition.ocr import Box, crop_box

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == "https://limitlesstcg.com/cards/DRI/81":
        return httpx.Response(200, content=(FIXTURES / "en_DRI_81.html").read_bytes())
    if url == "https://limitlesstcg.com/cards/PAR/81":
        return httpx.Response(200, content=(FIXTURES / "en_SVI_1.html").read_bytes())
    return httpx.Response(404)


def _box(x0: float, y0: float, x1: float, y1: float) -> Box:
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


class _FakeOcr:
    """箱の幅で行を識別し、決められた文字列を返す偽 OCR。"""

    def __init__(self, boxes: dict[str, Box], texts: dict[str, str]) -> None:
        self.boxes = boxes
        self.texts = texts
        self.detect_calls: list[int | None] = []
        self.recognized: list[str] = []

    def detect(self, image: np.ndarray, max_side: int | None = None) -> list[Box]:
        self.detect_calls.append(max_side)
        return list(self.boxes.values())

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        out = []
        for c in crops:
            width = c.shape[1] - 4  # crop_box の余白 2px × 2
            key = next(k for k, b in self.boxes.items() if abs((b[1][0] - b[0][0]) - width) <= 1)
            self.recognized.append(key)
            out.append((self.texts[key], 0.95))
        return out

    def read(self, image: np.ndarray):  # noqa: ANN201
        raise AssertionError("高速パスは全行認識を使わない")


def _pipeline(tmp_path: Path, ocr: _FakeOcr) -> Pipeline:
    http = CachedHttpClient(tmp_path / "cache", min_interval=0)
    http._client = httpx.Client(transport=httpx.MockTransport(_handler))
    return Pipeline(CardRepository(http, tmp_path / "images"), ocr=ocr)  # type: ignore[arg-type]


BOXES = {
    "name": _box(60, 100, 400, 140),  # 高さ 40
    "label": _box(60, 160, 180, 180),  # "Basic"
    "description": _box(60, 600, 740, 620),  # 横長の説明文（1 段目では読まない）
    "number": _box(60, 950, 200, 974),  # 高さ 24
}


def test_fast_path_reads_number_line_only(tmp_path: Path) -> None:
    ocr = _FakeOcr(
        BOXES,
        {
            "name": "Team Rockets Mewtwo ex",
            "label": "Basic",
            "description": "Search your deck for a card",
            "number": "DRI EN 081/182",
        },
    )
    frame = np.zeros((1000, 800, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame)
    assert result.en_ref == CardRef("en", "DRI", "81")
    assert result.recognized is not None and result.recognized.number == "81"
    assert "description" not in ocr.recognized  # 説明文は読まない
    assert ocr.detect_calls == [960]  # 縮小検出で済む（1000px 以下なので実際は縮小なし）
    assert result.card_box is not None


def test_fast_path_reads_name_when_set_code_is_missing(tmp_path: Path) -> None:
    ocr = _FakeOcr(
        BOXES,
        {
            "name": "Team Rockets Mewtwo ex",
            "label": "Basic",
            "description": "Search your deck for a card",
            "number": "081/182",  # 分母 182 は DRI と PAR で衝突 → 名前が必要
        },
    )
    frame = np.zeros((1000, 800, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame)
    assert result.en_ref == CardRef("en", "DRI", "81")
    assert "name" in ocr.recognized
    assert "ocr_name" in result.timings_ms


def test_fast_path_reports_failure_without_number(tmp_path: Path) -> None:
    boxes = {k: v for k, v in BOXES.items() if k != "number"}
    ocr = _FakeOcr(boxes, {"name": "Riolu", "label": "Basic", "description": "text text text"})
    frame = np.zeros((1000, 800, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame)
    assert not result.ok and result.error is not None and "番号行" in result.error


def test_fast_path_does_not_redetect_at_native_resolution(tmp_path: Path) -> None:
    # 元解像度の再検出は成功率が極めて低く（常駐ログで約 150 回に 1 回）、
    # 失敗を 0.6 秒遅らせるため行わない
    ocr = _FakeOcr({"label": _box(60, 160, 180, 180)}, {"label": "Basic"})
    frame = np.zeros((1400, 1200, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame)
    assert ocr.detect_calls == [960]
    assert result.hp_badge_rois is None  # 全画面でなければ HP バッジは評価しない


def test_fast_path_reports_hp_badge_candidates_on_full_frame(tmp_path: Path) -> None:
    # 番号行が無く HP バッジ候補も無い全画面: 候補数 0 を返す（ログ用）
    ocr = _FakeOcr({"label": _box(60, 160, 180, 180)}, {"label": "Basic"})
    frame = np.zeros((1000, 1600, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame, full_frame=True)
    assert not result.ok and result.hp_badge_rois == 0

    # HP バッジらしい大きな箱（2〜3 桁の数字で終わる）があれば候補数 > 0
    boxes = {"label": _box(60, 160, 180, 180), "hp": _box(1200, 100, 1300, 140)}
    ocr = _FakeOcr(boxes, {"label": "Basic", "hp": "110"})
    result = _pipeline(tmp_path, ocr).identify_fast(frame, full_frame=True)
    assert not result.ok and result.hp_badge_rois == 1


@pytest.mark.parametrize("angle", [0.0, 12.0])
def test_crop_box_straightens_rotated_boxes(angle: float) -> None:
    image = np.zeros((200, 300, 3), dtype=np.uint8)
    center = np.array([150.0, 100.0])
    rect = np.array([[-60, -10], [60, -10], [60, 10], [-60, 10]], dtype=np.float32)
    rad = np.deg2rad(angle)
    rot = np.array([[np.cos(rad), -np.sin(rad)], [np.sin(rad), np.cos(rad)]], dtype=np.float32)
    pts = rect @ rot.T + center
    crop = crop_box(image, tuple(tuple(p) for p in pts))
    assert crop.shape[0] in (24, 25) and crop.shape[1] in (124, 125)

"""高速パス（文字検出 + 選択的な認識）のテスト。OCR モデルは使わず、検出と認識の結果を偽装する。"""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np
import pytest

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.cards import CardRepository
from ptcgl.data.limitless import CardRef
from ptcgl.pipeline import IdentifyResult, Pipeline
from ptcgl.recognition.ocr import Box, crop_box

FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if request.url.path == "/cards" and request.url.params.get("q"):
        # 名前+HP の検索: 1 件だけ返す
        body = (
            "<html><body><p>1 cards found</p>"
            '<table><tr><td><a href="/cards/DRI/81">x</a></td></tr></table></body></html>'
        )
        return httpx.Response(200, content=body.encode("utf-8"))
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


def test_fast_path_identifies_detail_view_by_hp_badge(tmp_path: Path) -> None:
    # 対戦中の詳細表示: 番号は隠れ、大きな HP バッジとその左の名前バーがある。
    # 全行認識せずに名前・HP・ワザ名の検索で特定する
    boxes = {
        "name": _box(700, 105, 1150, 155),  # 高さ 50、HP の左
        "hp": _box(1200, 100, 1300, 160),  # 高さ 60 = 6%（HP バッジの条件 3.5% 以上）
        "attack": _box(720, 400, 900, 440),
        "description": _box(60, 800, 1500, 830),  # 横長の説明文
    }
    ocr = _FakeOcr(
        boxes,
        {"name": "Charmander", "hp": "80", "attack": "Agile", "description": "Energy to attack,"},
    )
    frame = np.zeros((1000, 1600, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame, full_frame=True)
    assert result.anchor_kind == "hp_badge"
    assert result.en_ref == CardRef("en", "DRI", "81")
    assert result.recognized is not None and result.recognized.name_en == "Charmander"
    assert result.recognized.hp == 80
    assert "hp_badge" in result.timings_ms and "hp_roi" not in result.timings_ms
    # 全画面の検出 1 回 + 推定したカード領域の番号読み直し 1 回（番号が無いので検索へ）
    assert len(ocr.detect_calls) == 2 and ocr.detect_calls[0] == 1600
    assert "description" not in ocr.recognized


def test_fast_path_prefers_number_over_search_when_hp_badge_found(tmp_path: Path) -> None:
    # HP バッジと名前が見えていても、推定したカード領域を拡大して番号が読めればそちらで確定する
    # （検索は複数の印刷から先頭を選ぶので、番号の方が確実）
    boxes = {
        "name": _box(700, 105, 1150, 155),
        "hp": _box(1200, 100, 1300, 160),
        "number": _box(60, 950, 200, 974),
    }

    class _OcrNumberOnlyWhenEnlarged(_FakeOcr):
        def detect(self, image: np.ndarray, max_side: int | None = None) -> list[Box]:
            # 全画面（幅 1600）では番号が小さくて検出されず、カード領域の拡大画像でだけ検出される
            found = super().detect(image, max_side)
            return found if image.shape[1] < 1200 else [b for b in found if b != boxes["number"]]

    ocr = _OcrNumberOnlyWhenEnlarged(
        boxes, {"name": "Charmander", "hp": "80", "number": "DRI EN 081/182"}
    )
    frame = np.zeros((1000, 1600, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame, full_frame=True)
    assert result.en_ref == CardRef("en", "DRI", "81")
    assert result.anchor_kind == "number" and result.recognized is not None
    assert result.recognized.number == "81"
    assert len(ocr.detect_calls) == 2  # 全画面 + 推定したカード領域
    assert result.card_box is not None and 0.35 < result.card_box.rx < 0.6  # フレーム比率に戻る


def test_fast_path_does_not_remember_roi_when_only_matching_failed(tmp_path: Path) -> None:
    # 番号は読めたが照合（通信）に失敗した領域は記憶しない。次は通るかもしれないため
    boxes = {"label": _box(60, 160, 180, 180), "hp": _box(1200, 100, 1300, 140)}
    ocr = _FakeOcr(boxes, {"label": "Basic", "hp": "110"})
    frame = np.zeros((1000, 1600, 3), dtype=np.uint8)
    pipeline = _pipeline(tmp_path, ocr)
    original = pipeline.identify_fast

    def roi_reads_number_but_match_fails(frame, full_frame=False, cancel=None, _depth=0):  # noqa: ANN001, ANN202
        if _depth == 1:
            return IdentifyResult(en_ref=CardRef("en", "DRI", "81"), error="照合に失敗: timeout")
        return original(frame, full_frame=full_frame, cancel=cancel, _depth=_depth)

    pipeline.identify_fast = roi_reads_number_but_match_fails  # type: ignore[method-assign]
    assert not pipeline.identify_fast(frame, full_frame=True).ok
    assert not pipeline._failed_rois


def test_fast_path_reads_remaining_boxes_before_giving_up(tmp_path: Path) -> None:
    # 1〜2 段目の条件（横長すぎる行は除外）から漏れた番号行を、3 段目（残りすべて）で拾う
    boxes = {
        "name": _box(60, 100, 400, 140),
        "number": _box(60, 950, 500, 974),  # 幅 440 / 高さ 24 = 18 倍（2 段目の上限 16 を超える）
    }
    ocr = _FakeOcr(boxes, {"name": "Riolu", "number": "DRI EN 081/182 Illus. Someone"})
    frame = np.zeros((1000, 800, 3), dtype=np.uint8)
    pipeline = _pipeline(tmp_path, ocr)
    assert not pipeline.identify_fast(frame).ok  # 変化領域（全画面でない）では 3 段目は無い
    result = pipeline.identify_fast(frame, full_frame=True)
    assert result.en_ref == CardRef("en", "DRI", "81")
    assert "ocr_rest" in result.timings_ms
    assert ocr.detect_calls == [960, 1600]  # 検出のやり直しはしない


def test_fast_path_skips_hp_badge_regions_that_failed_before(tmp_path: Path) -> None:
    # 盤面のポケモンの HP バッジは演出のたびに候補になるが、同じ場所・同じ見た目なら読み直さない
    boxes = {"label": _box(60, 160, 180, 180), "hp": _box(1200, 100, 1300, 140)}
    ocr = _FakeOcr(boxes, {"label": "Basic", "hp": "110"})
    frame = np.zeros((1000, 1600, 3), dtype=np.uint8)
    pipeline = _pipeline(tmp_path, ocr)
    first = pipeline.identify_fast(frame, full_frame=True)
    assert not first.ok and first.hp_badge_rois == 1
    assert len(ocr.detect_calls) == 2  # 全画面 + HP バッジ領域の拡大
    second = pipeline.identify_fast(frame, full_frame=True)
    assert not second.ok and second.hp_badge_rois == 1
    assert len(ocr.detect_calls) == 3  # 同じ領域は省く
    # 見た目が変われば（カードが重なった等）読み直す
    changed = frame.copy()
    changed[100:600, 900:1300] = 255
    pipeline.identify_fast(changed, full_frame=True)
    assert len(ocr.detect_calls) == 5


def test_fast_path_stops_when_cancelled_between_stages(tmp_path: Path) -> None:
    boxes = {"label": _box(60, 160, 180, 180), "hp": _box(1200, 100, 1300, 140)}
    ocr = _FakeOcr(boxes, {"label": "Basic", "hp": "110"})
    frame = np.zeros((1000, 1600, 3), dtype=np.uint8)
    result = _pipeline(tmp_path, ocr).identify_fast(frame, full_frame=True, cancel=lambda: True)
    assert result.cancelled and not result.ok
    assert ocr.detect_calls == [1600]  # 番号行の段階だけ行い、HP バッジの段階に進まない
    assert "hp_badge" not in result.timings_ms

"""検出 → OCR → 英語カード特定 → 日本語カード照合 のパイプライン。

各段は独立したモジュールで、ここで接続だけを行う。

画面からの特定は 2 パス:
1. 縮小フレーム全体を OCR し、番号行または HP バッジを手がかり（アンカー）にカード領域を推定
2. 領域を元解像度で切り出して再度 OCR し、セットコード・番号・名前を読む
番号が隠れる対戦中の詳細表示では、名前・HP・ワザ名で Limitless を検索する。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from rapidfuzz import fuzz

from ptcgl.data.cards import CardRepository, MatchResult
from ptcgl.data.limitless import CardRef
from ptcgl.data.sets import find_set, sets_by_official_total
from ptcgl.recognition.anchor import Anchor, find_anchor
from ptcgl.recognition.card import LABEL_WORDS, RecognizedCard, extract_card_info
from ptcgl.recognition.detector import CardBox, CardDetector, crop
from ptcgl.recognition.ocr import OcrEngine, OcrLine

log = logging.getLogger(__name__)

NAME_MATCH_THRESHOLD = 70
OCR_PASS1_WIDTH = 1600
_ATTACK_LINE_RE = re.compile(r"^[A-Z][A-Za-z'’\- ]{2,30}$")


def _normalize_set_code(raw: str) -> str | None:
    """余計な文字が付いたセットコードを既知のコードに寄せる（"ODRI" = 規制マーク I + DRI）。"""
    raw = raw.upper()
    if find_set(raw):
        return raw
    for start in range(0, len(raw) - 1):
        for end in range(len(raw), start + 1, -1):
            cand = raw[start:end]
            if 2 <= len(cand) <= 4 and find_set(cand):
                return cand
    return None


@dataclass
class IdentifyResult:
    recognized: RecognizedCard | None = None
    card_box: CardBox | None = None
    anchor_kind: str | None = None
    en_ref: CardRef | None = None
    en_candidates: list[CardRef] = field(default_factory=list)
    match: MatchResult | None = None
    ocr_lines: list[OcrLine] = field(default_factory=list)
    error: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.match is not None and self.match.best is not None


class Pipeline:
    def __init__(
        self,
        repo: CardRepository,
        ocr: OcrEngine | None = None,
        detector: CardDetector | None = None,
    ) -> None:
        self.repo = repo
        self.ocr = ocr or OcrEngine()
        self.detector = detector  # 設定で手動領域が指定された場合などに使う。None ならアンカー方式

    # --- 英語カードの特定 ---------------------------------------------------

    def resolve_en_ref(
        self, rec: RecognizedCard, ocr_texts: list[str] | None = None
    ) -> tuple[CardRef | None, str]:
        """OCR 結果から Limitless の英語カード参照を決める。戻り値は (参照, 理由)。

        分母が衝突する場合は候補ページのカード名を、OCR の名前候補と全行の両方に対して比較する。
        """
        if rec.number is None:
            return None, "カード番号を読み取れませんでした"

        if rec.set_code:
            code = _normalize_set_code(rec.set_code)
            if code:
                return CardRef("en", code, rec.number), f"セットコード {code} を直接読み取り"

        if rec.total is None:
            return None, "番号の分母が無くセットを特定できません"

        candidates = sets_by_official_total(rec.total, rec.number_prefix)
        if not candidates:
            return None, f"分母 {rec.total} に一致するセットがありません"
        if len(candidates) == 1:
            return CardRef("en", candidates[0].code, rec.number), f"分母 {rec.total} が一意"

        # 分母が衝突: 各候補のカード名と OCR 名（および OCR 全行）を比較する
        texts = [t for t in [rec.name_en, *(ocr_texts or [])] if t and len(t) >= 3]
        if not texts:
            return None, f"分母 {rec.total} のセットが複数あり、名前も読み取れません"
        best: tuple[float, CardRef] | None = None
        for s in candidates:
            ref = CardRef("en", s.code, rec.number)
            try:
                page = self.repo.get_card_page(ref)
            except Exception as exc:
                log.debug("候補 %s を取得できません: %s", ref, exc)
                continue
            score = max(fuzz.ratio(t.lower(), page.name.lower()) for t in texts)
            if best is None or score > best[0]:
                best = (score, ref)
        if best is None or best[0] < NAME_MATCH_THRESHOLD:
            return None, f"分母 {rec.total} の候補とカード名が一致しません"
        return best[1], f"名前の類似度 {best[0]:.0f} で {best[1].set_code} を選択"

    def resolve_by_search(self, anchor: Anchor) -> tuple[list[CardRef], str]:
        """名前・HP・ワザ名で検索して英語カード候補を返す（番号が読めない場合）。"""
        if not anchor.name:
            return [], "カード名を読み取れませんでした"
        attack_terms = [
            t
            for t in anchor.text_lines
            if _ATTACK_LINE_RE.match(t)
            and t != anchor.name
            and t.lower() not in LABEL_WORDS
            and not t.isupper()
        ]
        # 長い行は説明文なので除外し、ワザ名らしい短い行を最大 2 つ使う
        attack_terms = [t for t in attack_terms if len(t.split()) <= 3][:2]
        log.info(
            "検索: name=%r hp=%s terms=%s (lines=%s)",
            anchor.name,
            anchor.hp,
            attack_terms,
            anchor.text_lines,
        )
        refs = self.repo.search_english(anchor.name, anchor.hp, attack_terms)
        if not refs:
            return [], f"検索で見つかりません: {anchor.name} HP{anchor.hp}"
        return refs, f"名前+HP+ワザ名で検索: {len(refs)} 件"

    # --- 実行 ---------------------------------------------------------------

    def identify_ref(self, ref: CardRef) -> IdentifyResult:
        """セット・番号指定で照合だけ行う（OCR を使わない検証用）。"""
        result = IdentifyResult(en_ref=ref)
        t0 = time.perf_counter()
        try:
            match = self.repo.find_japanese(ref)
            if match.best is not None:
                try:
                    best = self.repo.download_image(match.best)
                except Exception as exc:  # 画像が無くても照合結果と候補は残す
                    log.warning("日本語カード画像を取得できません: %s (%s)", match.best.ref, exc)
                    best = match.best
                for i, c in enumerate(match.candidates):
                    if c.ref == best.ref:
                        match.candidates[i] = best
                match.best = best
            result.match = match
        except Exception as exc:
            log.exception("照合に失敗")
            result.error = f"照合に失敗: {exc}"
        result.timings_ms["match"] = (time.perf_counter() - t0) * 1000
        return result

    def _ocr_downscaled(self, frame_bgr: np.ndarray) -> tuple[list[OcrLine], int, int]:
        h, w = frame_bgr.shape[:2]
        scale = min(1.0, OCR_PASS1_WIDTH / w)
        small = (
            cv2.resize(frame_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if scale < 1.0
            else frame_bgr
        )
        return self.ocr.read(small), small.shape[1], small.shape[0]

    def identify_frame(self, frame_bgr: np.ndarray, box: CardBox | None = None) -> IdentifyResult:
        """画面フレーム（またはカード画像）から日本語カードを特定する。"""
        result = IdentifyResult()
        t0 = time.perf_counter()
        anchor: Anchor | None = None

        if box is None and self.detector is not None:
            box = self.detector.detect(frame_bgr)

        if box is None:
            # パス 1: 縮小フレーム全体を OCR してアンカーを探す
            lines, sw, sh = self._ocr_downscaled(frame_bgr)
            result.ocr_lines = lines
            anchor = find_anchor(lines, sw, sh)
            if anchor is None and frame_bgr.shape[1] > OCR_PASS1_WIDTH:
                # 手札ホバーなど小さめの表示では番号が縮小で潰れるため、元解像度で再試行する
                lines = self.ocr.read(frame_bgr)
                result.ocr_lines = lines
                anchor = find_anchor(lines, frame_bgr.shape[1], frame_bgr.shape[0])
            result.timings_ms["ocr_pass1"] = (time.perf_counter() - t0) * 1000
            if anchor is None:
                result.error = (
                    "拡大表示されたカードが見つかりません（番号・HP のいずれも検出できず）"
                )
                return result
            box = anchor.roi
            result.anchor_kind = anchor.kind

        result.card_box = box
        card_img = crop(frame_bgr, box)
        result.timings_ms["detect"] = (time.perf_counter() - t0) * 1000

        # パス 2: カード領域を元解像度で OCR
        t1 = time.perf_counter()
        lines = self.ocr.read(card_img)
        result.ocr_lines = lines
        rec = extract_card_info(lines, frame_height=card_img.shape[0])
        if anchor is not None and anchor.kind == "number":
            # パス 1 で読めた番号・セットを補完する
            rec = RecognizedCard(
                name_en=rec.name_en,
                set_code=rec.set_code or anchor.set_code,
                number=rec.number or anchor.number,
                total=rec.total or anchor.total,
                hp=rec.hp,
                number_prefix=rec.number_prefix or anchor.number_prefix,
                confidence=max(rec.confidence, anchor.confidence),
            )
        if anchor is not None and anchor.kind == "hp_badge":
            # パス 2 で番号が読めていればそれを使い、読めなかった場合だけ名前・HP の検索に回す
            rec = RecognizedCard(
                name_en=anchor.name or rec.name_en,
                set_code=rec.set_code,
                number=rec.number,
                total=rec.total,
                hp=anchor.hp,
                number_prefix=rec.number_prefix,
                confidence=rec.confidence if rec.number else anchor.confidence,
            )
        result.recognized = rec
        result.timings_ms["ocr_pass2"] = (time.perf_counter() - t1) * 1000

        ref, reason = self.resolve_en_ref(rec, [ln.text for ln in lines])
        if ref is None and anchor is not None and anchor.kind == "hp_badge":
            refs, reason = self.resolve_by_search(anchor)
            result.en_candidates = refs
            if refs:
                ref = refs[0]
        if ref is None:
            result.error = reason
            return result
        log.info("英語カード特定: %s (%s)", ref, reason)
        sub = self.identify_ref(ref)
        result.en_ref = ref
        result.match = sub.match
        result.error = sub.error
        result.timings_ms.update(sub.timings_ms)
        return result

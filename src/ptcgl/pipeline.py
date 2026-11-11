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

from ptcgl.data.cards import CardRepository, JapaneseCard, MatchResult
from ptcgl.data.limitless import CardRef
from ptcgl.data.sets import find_set, sets_by_official_total
from ptcgl.recognition.anchor import Anchor, find_anchor, find_number_anchor, to_geoms
from ptcgl.recognition.card import LABEL_WORDS, RecognizedCard, extract_card_info
from ptcgl.recognition.detector import CARD_ASPECT, CardBox, CardDetector, crop
from ptcgl.recognition.ocr import Box, OcrEngine, OcrLine, box_rect, crop_box

log = logging.getLogger(__name__)

NAME_MATCH_THRESHOLD = 70
MATCH_CACHE_SIZE = 128  # 照合結果のメモリキャッシュ件数
OCR_PASS1_WIDTH = 1600
# 高速パス: 文字検出だけ行い、番号行らしい横長の箱だけ認識する
# （1 段目は下から 6 個 + 小さい順 6 個、2 段目は残り 6 個が上限）
FAST_TIER1_BOXES = 6
FAST_MAX_NAME_BOXES = 6
FAST_NUMBER_MIN_ASPECT = 2.5
FAST_NUMBER_MAX_ASPECT = 12.0  # 1 段目はこれより横長の行（説明文）を除外。認識は幅に比例して遅い
FAST_TIER2_MAX_ASPECT = 16.0
FAST_TIER2_BOXES = 6
FAST_NUMBER_MAX_HEIGHT = 0.035  # 番号行は小さい（画像高さに対する比率）。最低 20px
FAST_DET_MAX_SIDE_REGION = 960  # 変化領域（カード 1 枚程度）の検出は縮小して行う
FAST_DET_MAX_SIDE_FRAME = 1600  # 全画面
# 小さな領域は OCR の前に拡大する。カードが画面幅の 2 割程度だと番号の文字が 8px しかなく、
# 検出が途切れる（"056/" だけ読める）。長辺をこの長さまで拡大すると 14px 前後になる
FAST_UPSCALE_TARGET = 1100
FAST_UPSCALE_BELOW = 900
FAST_UPSCALE_MAX = 2.5
# HP バッジ（大きな数字）からカード領域を推定するときの箱の条件
FAST_HP_MIN_HEIGHT = 0.018  # 画像高さに対する比率
FAST_HP_MAX_ASPECT = 12.0  # 名前バーと結合した箱も拾う
FAST_HP_MAX_TRIES = 2
FAST_HP_CARD_HEIGHT_RATIO = 18.0
FAST_HP_MAX_CANDIDATES = 16
# 2〜3 桁の数字で終わる（HP バッジ）。"39:00" のタイマーや "076/132" の番号は除く
_TRAILING_HP_RE = re.compile(r"(?<![\d/:])(\d{2,3})\s*$")
# パス 2 はまず領域の下帯（セットコード・番号がある）だけを OCR し、セットコードで確定できたら
# そこで終える。確定できないとき（コードが読めない・分母が衝突）だけ領域全体を読んで名前を使う。
# 領域はアンカーの番号行から上へ推定するため、番号行は常に下端付近にある
PASS2_BOTTOM_RATIO = 0.2
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
    upscale: float = 1.0  # 高速パスで OCR 前に画像を拡大した倍率
    # 高速パス（全画面）で見つけた HP バッジ候補の数。未評価なら None
    hp_badge_rois: int | None = None

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
        # 英語カード → 照合結果のメモリキャッシュ。同じカードを続けて認識したときに
        # Limitless ページの再解析（約 25 ms）を省く。手動で候補を選び直したら捨てる
        self._match_cache: dict[CardRef, MatchResult] = {}

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
        cached = self._match_cache.get(ref)
        if cached is not None and cached.best is not None and cached.best.image_path is not None:
            if cached.best.image_path.exists():
                result.match = cached
                result.timings_ms["match"] = (time.perf_counter() - t0) * 1000
                log.info(
                    "照合（キャッシュ）: %s -> %s same_art=%s (%s)",
                    ref,
                    cached.best.ref,
                    cached.same_art,
                    cached.reason,
                )
                return result
            self._match_cache.pop(ref, None)  # 画像が消されていたら取り直す
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
            log.info(
                "照合: %s -> %s same_art=%s (%s)",
                ref,
                match.best.ref if match.best is not None else None,
                match.same_art,
                match.reason,
            )
            if match.best is not None and match.best.image_path is not None:
                # 画像まで揃った結果だけ覚える（取得に失敗した場合は次回また試す）
                if len(self._match_cache) >= MATCH_CACHE_SIZE:
                    self._match_cache.pop(next(iter(self._match_cache)))
                self._match_cache[ref] = match
        except Exception as exc:
            log.exception("照合に失敗")
            result.error = f"照合に失敗: {exc}"
        result.timings_ms["match"] = (time.perf_counter() - t0) * 1000
        return result

    def confirm(self, en_ref: CardRef, card: JapaneseCard, same_art: bool) -> None:
        """ユーザーが選んだ日本語版を保存し、その英語カードのキャッシュを捨てる。"""
        self.repo.confirm(en_ref, card, same_art)
        self._match_cache.pop(en_ref, None)

    def _ocr_downscaled(self, frame_bgr: np.ndarray) -> tuple[list[OcrLine], int, int]:
        h, w = frame_bgr.shape[:2]
        scale = min(1.0, OCR_PASS1_WIDTH / w)
        small = (
            cv2.resize(frame_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            if scale < 1.0
            else frame_bgr
        )
        return self.ocr.read(small), small.shape[1], small.shape[0]

    # --- 高速パス -------------------------------------------------------------

    def _find_number_fast(
        self, frame_bgr: np.ndarray, boxes: list[Box], tiers: int = 2
    ) -> tuple[list[OcrLine], Anchor | None]:
        """検出した箱のうち番号行らしいものから順に認識し、番号アンカーを探す。

        1 段目: 小さく横長（説明文ほど長くない）な箱を下から最大 6 個。ここで大半は決まる
        2 段目: 残りの小さな箱（他の行と結合した番号行などを拾う）
        """
        h, w = frame_bgr.shape[:2]
        max_h = max(FAST_NUMBER_MAX_HEIGHT * h, 20.0)
        small: list[tuple[float, float, Box]] = []  # (bottom, aspect, box)
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            bh = max(y1 - y0, 1.0)
            aspect = (x1 - x0) / bh
            if bh <= max_h and aspect >= FAST_NUMBER_MIN_ASPECT:
                small.append((y1, aspect, b))
        # 1 段目: 「下にある順」と「小さい順」の上位を合わせる。
        # 番号行はカード下端の最小の文字なので、手札など他の文字が混ざってもどちらかで上位に入る
        narrow = [(y1, a, b) for y1, a, b in small if a <= FAST_NUMBER_MAX_ASPECT]
        by_bottom = [b for _, _, b in sorted(narrow, key=lambda t: -t[0])][:FAST_TIER1_BOXES]
        by_height = [
            b
            for _, _, b in sorted(narrow, key=lambda t: box_rect(t[2])[3] - box_rect(t[2])[1])
            if (box_rect(b)[3] - box_rect(b)[1]) >= 9
        ][:FAST_TIER1_BOXES]
        tier1 = by_bottom + [b for b in by_height if b not in by_bottom]
        tier2 = [
            b
            for _, a, b in sorted(small, key=lambda t: -t[0])
            if b not in tier1 and a <= FAST_TIER2_MAX_ASPECT
        ][:FAST_TIER2_BOXES]
        lines: list[OcrLine] = []
        for tier in (tier1, tier2)[:tiers]:
            if not tier:
                continue
            texts = self.ocr.recognize([crop_box(frame_bgr, b) for b in tier])
            lines.extend(
                OcrLine(text=t, confidence=c, box=b)
                for b, (t, c) in zip(tier, texts, strict=True)
                if t
            )
            anchor = find_number_anchor(to_geoms(lines, w, h), w / h)
            if anchor is not None:
                return lines, anchor
        return lines, None

    def identify_fast(
        self, frame_bgr: np.ndarray, full_frame: bool = False, _depth: int = 0
    ) -> IdentifyResult:
        """文字検出 + 選択的な認識で、番号行が見えるカードを最短で特定する。

        全行を認識する通常のパイプラインに比べて 3〜5 倍速い。番号が隠れる対戦中の詳細表示は
        扱えないので、失敗したら呼び出し側で identify_frame() にフォールバックする。
        `full_frame` が True（画面全体）で番号行が読めないときは HP バッジ（大きな数字）から
        カード領域を推定し、そこを拡大して再試行する。
        """
        result = IdentifyResult()
        h, w = frame_bgr.shape[:2]
        if max(h, w) < FAST_UPSCALE_BELOW:
            factor = min(FAST_UPSCALE_TARGET / max(h, w), FAST_UPSCALE_MAX)
            frame_bgr = cv2.resize(
                frame_bgr, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC
            )
            h, w = frame_bgr.shape[:2]
            result.upscale = factor
        if result.upscale > 1.0:
            max_side = max(h, w)  # 拡大した意味が無くなるので検出で縮小しない
        elif full_frame:
            max_side = FAST_DET_MAX_SIDE_FRAME
        else:
            max_side = FAST_DET_MAX_SIDE_REGION
        t0 = time.perf_counter()
        boxes = self.ocr.detect(frame_bgr, max_side=max_side)
        result.timings_ms["detect"] = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        lines, anchor = self._find_number_fast(frame_bgr, boxes)
        result.timings_ms["ocr_number"] = (time.perf_counter() - t1) * 1000
        if anchor is None and _depth == 0 and full_frame:
            # 全画面で番号行が読めない: HP バッジからカード領域を推定し、そこを拡大して読み直す
            # （小さく表示されたカードは番号が 8px 程度で、全画面の検出では途切れる）。
            # 変化領域に対しては行わない（領域自体を拡大して読むので不要で、失敗時の無駄が大きい）
            t3 = time.perf_counter()
            rois = self._hp_badge_rois(frame_bgr, boxes)
            result.hp_badge_rois = len(rois)
            for roi in rois:
                sub = self.identify_fast(crop(frame_bgr, roi), _depth=1)
                if sub.ok:
                    for key, value in result.timings_ms.items():
                        sub.timings_ms.setdefault(f"frame_{key}", value)
                    sub.timings_ms["hp_roi"] = (time.perf_counter() - t3) * 1000
                    if sub.card_box is not None:
                        b = sub.card_box
                        sub.card_box = CardBox(
                            roi.rx + b.rx * roi.rw,
                            roi.ry + b.ry * roi.rh,
                            b.rw * roi.rw,
                            b.rh * roi.rh,
                            b.confidence,
                        )
                    return sub
            result.timings_ms["hp_roi"] = (time.perf_counter() - t3) * 1000
        # 元解像度での再検出は行わない（常駐ログの集計で約 150 回中 1 回しか成功せず、
        # 失敗 1 回あたり 0.6 秒を費やしていた。小さな表示は HP バッジ経由の拡大で拾う）
        result.ocr_lines = lines
        if anchor is None:
            result.error = "番号行が見つかりません（高速パス）"
            return result
        result.anchor_kind = "number"
        result.card_box = anchor.roi
        rec = RecognizedCard(
            name_en=None,
            set_code=anchor.set_code,
            number=anchor.number,
            total=anchor.total,
            hp=None,
            number_prefix=anchor.number_prefix,
            confidence=anchor.confidence,
        )
        ref, reason = self.resolve_en_ref(rec, [])
        if ref is None:
            # セットコードが読めない・分母が衝突 → 番号行と左端が揃う上の行（名前）を読む
            t2 = time.perf_counter()
            name_boxes = self._name_boxes_above(boxes, anchor, w)
            name_texts = self.ocr.recognize([crop_box(frame_bgr, b) for b in name_boxes])
            name_lines = [
                OcrLine(text=t, confidence=c, box=b)
                for b, (t, c) in zip(name_boxes, name_texts, strict=True)
                if t
            ]
            result.ocr_lines = lines + name_lines
            rec = extract_card_info(result.ocr_lines, frame_height=h)
            rec = RecognizedCard(
                name_en=rec.name_en,
                set_code=rec.set_code or anchor.set_code,
                number=rec.number or anchor.number,
                total=rec.total or anchor.total,
                hp=None,
                number_prefix=rec.number_prefix or anchor.number_prefix,
                confidence=anchor.confidence,
            )
            ref, reason = self.resolve_en_ref(rec, [ln.text for ln in result.ocr_lines])
            result.timings_ms["ocr_name"] = (time.perf_counter() - t2) * 1000
        result.recognized = rec
        if ref is None:
            result.error = reason
            return result
        return self._finish(result, ref, reason)

    def _hp_badge_rois(self, frame_bgr: np.ndarray, boxes: list[Box]) -> list[CardBox]:
        """大きな数字で終わる箱（HP バッジ。名前バーと結合していることもある）からカード領域を推定。

        拡大表示のカードは左上に名前、右上に HP バッジがある。バッジの右端からカード幅ぶん左、
        下にカード高さぶんの矩形を、バッジの大きい順に返す。
        場のカードの HP バッジも候補に入るため、呼び出し側は順に試す。
        """
        h, w = frame_bgr.shape[:2]
        cands: list[tuple[float, Box]] = []
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            bh, bw = y1 - y0, x1 - x0
            if bh >= FAST_HP_MIN_HEIGHT * h and bw / max(bh, 1.0) <= FAST_HP_MAX_ASPECT:
                cands.append((bh, b))
        cands.sort(key=lambda t: -t[0])
        cands = cands[:FAST_HP_MAX_CANDIDATES]
        if not cands:
            return []
        texts = self.ocr.recognize([crop_box(frame_bgr, b) for _, b in cands])
        rois: list[CardBox] = []
        for (bh, b), (t, _) in zip(cands, texts, strict=True):
            if not _TRAILING_HP_RE.search(t):
                continue
            x0, y0, x1, y1 = box_rect(b)
            # バッジ高さに対するカード高さは表示の種類で 11〜18 倍。大きめに取る
            card_h = bh * FAST_HP_CARD_HEIGHT_RATIO
            card_w = card_h * CARD_ASPECT
            left = max(x1 - card_w * 1.15, 0.0)
            top = max(y0 - card_h * 0.06, 0.0)
            rois.append(
                CardBox(
                    left / w,
                    top / h,
                    min(card_w * 1.3, w - left) / w,
                    min(card_h * 1.15, h - top) / h,
                    0.8,
                )
            )
        return rois[:FAST_HP_MAX_TRIES]

    @staticmethod
    def _name_boxes_above(boxes: list[Box], anchor: Anchor, w: int) -> list[Box]:
        """番号行の上にある、カード名らしい大きめの箱を選ぶ。

        ポケモンの名前は「BASIC」などの進化段階ラベルの右に置かれ、番号行と左端が揃わない。
        そのため左端は番号行からカード幅の 4 割までの右寄りを許し、高さが番号行より十分大きい
        箱を大きい順に返す。
        """
        if anchor.line is None:
            return []
        num_rect = box_rect(anchor.line.box)
        nh = max(num_rect[3] - num_rect[1], 1.0)
        card_w = anchor.roi.rw * w
        out: list[tuple[float, Box]] = []
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            bh = y1 - y0
            if bh < nh * 1.1 or bh > nh * 6:
                continue
            if x0 < num_rect[0] - nh * 4 or x0 > num_rect[0] + card_w * 0.4:
                continue
            if y0 >= num_rect[1] - nh * 8 or y0 < num_rect[1] - nh * 70:
                continue
            out.append((bh, b))
        out.sort(key=lambda t: -t[0])
        return [b for _, b in out[:FAST_MAX_NAME_BOXES]]

    @staticmethod
    def _bottom_band(card_img: np.ndarray) -> np.ndarray:
        h = card_img.shape[0]
        band_h = int(h * PASS2_BOTTOM_RATIO)
        return card_img[h - band_h :] if band_h >= 16 else card_img

    def identify_frame(
        self, frame_bgr: np.ndarray, box: CardBox | None = None, native_retry: bool = True
    ) -> IdentifyResult:
        """画面フレーム（またはカード画像）から日本語カードを特定する。

        `native_retry` を False にすると、縮小フレームでアンカーが見つからなくても元解像度で
        全行を読み直さない（約 1.2 秒）。常駐モードでは高速パスが先に小さな表示を拾うため不要で、
        カードが無い画面での失敗を短くするために使う。
        """
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
            if anchor is None and native_retry and frame_bgr.shape[1] > OCR_PASS1_WIDTH:
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

        # パス 2a: 番号アンカーなら下帯だけ OCR し、セットコードで確定できればそこで終える
        t1 = time.perf_counter()
        if anchor is not None and anchor.kind == "number":
            band = self._bottom_band(card_img)
            band_lines = self.ocr.read(band)
            band_rec = extract_card_info(band_lines, frame_height=band.shape[0])
            quick = RecognizedCard(
                name_en=None,  # 下帯の説明文を名前と誤認しないよう、名前は使わない
                set_code=band_rec.set_code or anchor.set_code,
                number=band_rec.number or anchor.number,
                total=band_rec.total or anchor.total,
                hp=None,
                number_prefix=band_rec.number_prefix or anchor.number_prefix,
                confidence=max(band_rec.confidence, anchor.confidence),
            )
            ref, reason = self.resolve_en_ref(quick, [])
            if ref is not None:
                result.recognized = quick
                result.ocr_lines = band_lines
                result.timings_ms["ocr_pass2"] = (time.perf_counter() - t1) * 1000
                return self._finish(result, ref, reason)
            log.debug("下帯では確定できず、領域全体を OCR します: %s", reason)

        # パス 2b: カード領域全体を元解像度で OCR（名前・HP も読む）
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
        return self._finish(result, ref, reason)

    def _finish(self, result: IdentifyResult, ref: CardRef, reason: str) -> IdentifyResult:
        """英語カードが決まった後の日本語照合（共通の終端処理）。"""
        log.info("英語カード特定: %s (%s)", ref, reason)
        sub = self.identify_ref(ref)
        result.en_ref = ref
        result.match = sub.match
        result.error = sub.error
        result.timings_ms.update(sub.timings_ms)
        return result

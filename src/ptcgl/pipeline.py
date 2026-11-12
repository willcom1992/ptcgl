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
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import cv2
import numpy as np
from rapidfuzz import fuzz

from ptcgl.data.cards import CardRepository, JapaneseCard, MatchResult
from ptcgl.data.limitless import CardRef
from ptcgl.data.sets import find_set, sets_by_official_total
from ptcgl.recognition.anchor import (
    HP_BADGE_MIN_HEIGHT,
    Anchor,
    find_anchor,
    find_hp_badge_anchor,
    find_number_anchor,
    is_hp_badge_text,
    to_geoms,
)
from ptcgl.recognition.card import LABEL_WORDS, RecognizedCard, extract_card_info
from ptcgl.recognition.detector import CARD_ASPECT, CardBox, CardDetector, crop, to_frame_box
from ptcgl.recognition.ocr import Box, OcrEngine, OcrLine, box_rect, crop_box

log = logging.getLogger(__name__)

NAME_MATCH_THRESHOLD = 70
MATCH_CACHE_SIZE = 128  # 照合結果のメモリキャッシュ件数
OCR_PASS1_WIDTH = 1600
# 高速パス: 文字検出だけ行い、番号行らしい横長の箱だけ認識する
# （1 段目は下から 6 個 + 小さい順 6 個、2 段目は残り 6 個が上限、3 段目は残りの箱すべて）
FAST_TIER1_BOXES = 6
FAST_MAX_NAME_BOXES = 6
FAST_NUMBER_MIN_ASPECT = 2.5
FAST_NUMBER_MAX_ASPECT = 12.0  # 1 段目はこれより横長の行（説明文）を除外。認識は幅に比例して遅い
FAST_TIER2_MAX_ASPECT = 16.0
FAST_TIER2_BOXES = 6
FAST_TIER3_MAX_BOXES = 40  # 3 段目（残りすべて）の上限。全画面の検出は WQHD の盤面で 10〜50 箱
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
FAST_HP_NAME_MAX_GAP = 0.25  # 名前行の右端から HP バッジまでの最大間隔（フレーム幅比）
# 失敗した HP バッジ領域の記憶（位置の許容差は比率、見た目の差は縮小グレースケールの平均絶対差分）
FAILED_ROI_CACHE_SIZE = 16
FAILED_ROI_TTL_S = 120.0  # これより古い記憶は使わない（盤面はターンごとに変わる）
FAILED_ROI_POS_TOLERANCE = 0.01
FAILED_ROI_MAX_DIFF = 10.0
FAILED_ROI_THUMB_SIZE = (24, 32)
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


# 常駐モードの中断判定。読んでいる文字の箱（フレーム比率）を受け取り、打ち切るなら True
CancelCheck = Callable[[Sequence[CardBox] | None], bool]


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
    cancelled: bool = False  # 呼び出し側の要求で途中で打ち切った（画面が変わった）

    ABORT_MESSAGE = "画面が変わったため中断"

    @classmethod
    def aborted(cls) -> IdentifyResult:
        """呼び出し側の要求で打ち切ったことを表す結果。"""
        return cls(error=cls.ABORT_MESSAGE, cancelled=True)

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
        # 時間のかかる段階に入るときに呼ぶ（常駐モードの状態表示用）。初めて見るカードの
        # 日本語版取得は Limitless に 1 秒 1 回のアクセスで 2〜5 秒かかるため、その間の状態を
        # 知らせる
        self.on_progress: Callable[[str], None] | None = None
        # 英語カード → 照合結果のメモリキャッシュ。同じカードを続けて認識したときに
        # Limitless ページの再解析（約 25 ms）を省く。手動で候補を選び直したら捨てる
        self._match_cache: dict[CardRef, MatchResult] = {}
        # (領域, 縮小グレースケール, 記録時刻)
        self._failed_rois: deque[tuple[CardBox, np.ndarray, float]] = deque(
            maxlen=FAILED_ROI_CACHE_SIZE
        )

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
        if self.on_progress is not None:
            self.on_progress(f"{ref.set_code} #{ref.number} を特定。日本語版を取得中...")
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

    def _read_boxes(
        self, frame_bgr: np.ndarray, boxes: list[Box], read: dict[Box, OcrLine | None]
    ) -> list[OcrLine]:
        """まだ読んでいない箱だけをまとめて認識し、読めた行を返す。結果は `read` に記録する。"""
        todo = [b for b in boxes if b not in read]
        if not todo:
            return []
        texts = self.ocr.recognize([crop_box(frame_bgr, b) for b in todo])
        out: list[OcrLine] = []
        for b, (t, c) in zip(todo, texts, strict=True):
            line = OcrLine(text=t, confidence=c, box=b) if t else None
            read[b] = line
            if line is not None:
                out.append(line)
        return out

    @staticmethod
    def _lines(read: dict[Box, OcrLine | None], reading_order: bool = False) -> list[OcrLine]:
        """読めた行。`reading_order` なら上から下・左から右に並べる（全行認識と同じ順）。"""
        lines = [ln for ln in read.values() if ln is not None]
        if reading_order:
            lines.sort(key=lambda ln: (box_rect(ln.box)[1], box_rect(ln.box)[0]))
        return lines

    def _find_number_fast(
        self,
        frame_bgr: np.ndarray,
        boxes: list[Box],
        read: dict[Box, OcrLine | None],
        tiers: int = 2,
    ) -> Anchor | None:
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
        for tier in (tier1, tier2)[:tiers]:
            if not tier:
                continue
            self._read_boxes(frame_bgr, tier, read)
            anchor = find_number_anchor(to_geoms(self._lines(read), w, h), w / h)
            if anchor is not None:
                return anchor
        return None

    @staticmethod
    def _cancelled(
        cancel: CancelCheck | None, result: IdentifyResult, boxes: Sequence[CardBox]
    ) -> bool:
        """呼び出し側（常駐モード）が中断を求めていれば結果に印を付けて True。

        `boxes` は読んでいる文字の箱（フレーム比率）。呼び出し側はこの中が変わったかで判定できる。
        """
        if cancel is not None and cancel(boxes):
            result.cancelled = True
            result.error = IdentifyResult.ABORT_MESSAGE
            return True
        return False

    @staticmethod
    def _boxes_to_ratio(boxes: list[Box], width: int, height: int) -> list[CardBox]:
        out: list[CardBox] = []
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            out.append(CardBox(x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height))
        return out

    def identify_fast(
        self,
        frame_bgr: np.ndarray,
        full_frame: bool = False,
        cancel: CancelCheck | None = None,
        _depth: int = 0,
    ) -> IdentifyResult:
        """文字検出 + 選択的な認識で、画面のカードを最短で特定する（常駐モードの主経路）。

        文字検出は 1 回だけ行い、箱を「番号行らしい順」に少しずつ認識する。
        1. 番号行（1〜2 段目: 小さく横長の箱）→ セットコード・分母で英語カードを確定
        2. `full_frame`（画面全体）のときだけ:
           a. HP バッジアンカー: 大きな 2〜3 桁の数字とその左の名前行から、名前・HP・ワザ名で検索
              （番号が隠れる対戦中の詳細表示）
           b. HP バッジ（小さな表示も含む）の右端からカード領域を推定し、拡大して番号を読み直す
              （画面幅の 2 割程度の手札ホバーは番号が 8px で全画面の検出では途切れる）
           c. 3 段目: 残りの箱をすべて認識して番号行を探す（通常パイプラインの全行認識と同じ範囲）
        `cancel` は段階の切れ目で、検出した文字の箱（フレーム比率）を添えて呼ばれ、True を返すと
        残りを省いて `cancelled` 付きの結果を返す（画面が変わってしまった古いフレームに時間を
        使わないため）。番号行の 1〜2 段目は中断しない。
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
        read: dict[Box, OcrLine | None] = {}
        t1 = time.perf_counter()
        anchor = self._find_number_fast(frame_bgr, boxes, read)
        result.timings_ms["ocr_number"] = (time.perf_counter() - t1) * 1000
        fallback_error: str | None = None
        if anchor is None and _depth == 0 and full_frame:
            text_boxes = self._boxes_to_ratio(boxes, w, h)
            if self._cancelled(cancel, result, text_boxes):
                return result
            # a. HP バッジアンカー（番号が隠れる詳細表示）。大きな箱を読み、名前・HP で検索する
            t2 = time.perf_counter()
            hp_lines = self._read_large_boxes(frame_bgr, boxes, read)
            found, fallback_error = self._identify_by_hp_badge(
                frame_bgr, boxes, hp_lines, read, result
            )
            result.timings_ms["hp_badge"] = (time.perf_counter() - t2) * 1000
            if found is not None:
                return found
            # b. HP バッジからカード領域を推定し、拡大して番号を読み直す（小さな表示）
            t3 = time.perf_counter()
            rois = self._hp_badge_rois(frame_bgr, hp_lines)
            result.hp_badge_rois = len(rois)
            roi_diffs: list[float | None] = []  # 記憶した失敗領域との差分（None は記憶なし）
            for roi in rois:
                if self._cancelled(cancel, result, text_boxes):
                    return result
                sub = self._identify_roi(frame_bgr, roi, roi_diffs)
                if sub is not None:
                    for key, value in result.timings_ms.items():
                        sub.timings_ms.setdefault(f"frame_{key}", value)
                    sub.timings_ms["hp_roi"] = (time.perf_counter() - t3) * 1000
                    return sub
            result.timings_ms["hp_roi"] = (time.perf_counter() - t3) * 1000
            if rois:
                # 失敗領域の記憶がどれだけ効いているかを残す（しきい値の調整材料）
                skipped = sum(1 for d in roi_diffs if d is not None and d < FAILED_ROI_MAX_DIFF)
                log.info(
                    "HP バッジ領域の拡大読み直しで番号なし: %d 件"
                    "（省略 %d, 記憶との差分 %s, %.0fms）",
                    len(rois),
                    skipped,
                    [None if d is None else round(d) for d in roi_diffs],
                    result.timings_ms["hp_roi"],
                )
            # c. 3 段目: 残りの箱をすべて読む。全行認識（通常パイプラインのパス 1）と同じ範囲を
            # 検出をやり直さずに済ませる。小さい順に読み、上限を超える大きな箱は省く
            if self._cancelled(cancel, result, text_boxes):
                return result
            t4 = time.perf_counter()
            rest = sorted(
                (b for b in boxes if b not in read),
                key=lambda b: box_rect(b)[3] - box_rect(b)[1],
            )[:FAST_TIER3_MAX_BOXES]
            self._read_boxes(frame_bgr, rest, read)
            anchor = find_number_anchor(to_geoms(self._lines(read), w, h), w / h)
            result.timings_ms["ocr_rest"] = (time.perf_counter() - t4) * 1000
            if anchor is not None and anchor.line is not None:
                # 1〜2 段目で読めなかった理由を残す（段階の選び方を直す材料にする）
                x0, y0, x1, y1 = box_rect(anchor.line.box)
                log.info(
                    "番号行を 3 段目で発見: %r h=%.1f%% aspect=%.1f bottom=%.2f boxes=%d",
                    anchor.line.text,
                    (y1 - y0) / h * 100,
                    (x1 - x0) / max(y1 - y0, 1.0),
                    y1 / h,
                    len(boxes),
                )
        # 元解像度での再検出は行わない（常駐ログの集計で約 150 回中 1 回しか成功せず、
        # 失敗 1 回あたり 0.6 秒を費やしていた。小さな表示は HP バッジ経由の拡大で拾う）
        result.ocr_lines = self._lines(read)
        if anchor is None:
            result.error = fallback_error or "番号行が見つかりません（高速パス）"
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
            t5 = time.perf_counter()
            name_boxes = self._name_boxes_above(boxes, anchor, w)
            self._read_boxes(frame_bgr, name_boxes, read)
            result.ocr_lines = self._lines(read)
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
            result.timings_ms["ocr_name"] = (time.perf_counter() - t5) * 1000
        result.recognized = rec
        if ref is None:
            result.error = reason
            return result
        return self._finish(result, ref, reason)

    def _identify_roi(
        self, frame_bgr: np.ndarray, roi: CardBox, diffs: list[float | None] | None = None
    ) -> IdentifyResult | None:
        """カード領域と推定した矩形を拡大して番号を読み直す。

        成功した結果（座標はフレーム比率）か None を返す。

        最近同じ場所・同じ見た目で読めなかった領域は省く。読めなかった領域は記憶するが、
        番号は読めて照合だけ失敗した（通信エラー等）場合は記憶しない（次は通るかもしれない）。
        `diffs` を渡すと、記憶した失敗領域との最小差分（記憶が無ければ None）を追記する。
        """
        diff = self._failed_roi_difference(frame_bgr, roi)
        if diffs is not None:
            diffs.append(diff)
        if diff is not None and diff < FAILED_ROI_MAX_DIFF:
            log.debug("最近失敗した HP バッジ領域なので省略: %s diff=%.1f", roi, diff)
            return None
        sub = self.identify_fast(crop(frame_bgr, roi), _depth=1)
        if sub.ok:
            sub.card_box = to_frame_box(sub.card_box, roi)
            return sub
        if sub.en_ref is None:
            self._remember_failed_roi(frame_bgr, roi)
        return None

    def _read_large_boxes(
        self, frame_bgr: np.ndarray, boxes: list[Box], read: dict[Box, OcrLine | None]
    ) -> list[OcrLine]:
        """HP バッジの候補になる大きめの箱（大きい順に最大 16 個）を読んで返す。"""
        h = frame_bgr.shape[0]
        cands: list[tuple[float, Box]] = []
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            bh, bw = y1 - y0, x1 - x0
            if bh >= FAST_HP_MIN_HEIGHT * h and bw / max(bh, 1.0) <= FAST_HP_MAX_ASPECT:
                cands.append((bh, b))
        cands.sort(key=lambda t: -t[0])
        boxes_sel = [b for _, b in cands[:FAST_HP_MAX_CANDIDATES]]
        self._read_boxes(frame_bgr, boxes_sel, read)
        return [ln for b in boxes_sel if (ln := read.get(b)) is not None]

    def _identify_by_hp_badge(
        self,
        frame_bgr: np.ndarray,
        boxes: list[Box],
        hp_lines: list[OcrLine],
        read: dict[Box, OcrLine | None],
        result: IdentifyResult,
    ) -> tuple[IdentifyResult | None, str | None]:
        """HP バッジ（大きな数字）とその左の名前行から英語カードを特定する。

        通常パイプラインの HP バッジアンカーと同じ判定を、全行認識せずに行う: 最も大きい数字の
        左にある行だけを読んで名前を決める。まず推定したカード領域を拡大して番号を読み直し
        （番号が見えていれば検索より確実）、読めなければ領域内の行をワザ名として名前・HP・ワザ名で
        検索する。戻り値は (成功した結果 or None, 失敗理由 or None)。`result` は成功時だけ更新する。
        """
        h, w = frame_bgr.shape[:2]
        badges = [
            ln
            for ln in hp_lines
            if is_hp_badge_text(ln.text)
            and (box_rect(ln.box)[3] - box_rect(ln.box)[1]) >= HP_BADGE_MIN_HEIGHT * h
        ]
        if not badges:
            return None, None
        hp = max(badges, key=lambda ln: box_rect(ln.box)[3] - box_rect(ln.box)[1])
        hx0, hy0, hx1, hy1 = box_rect(hp.box)
        hp_h, hp_cy = hy1 - hy0, (hy0 + hy1) / 2
        # 名前候補: HP バッジの左にあり、縦に重なる箱（find_hp_badge_anchor と同じ条件）
        name_boxes = []
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            if x0 >= hx0 or hx0 - x1 >= FAST_HP_NAME_MAX_GAP * w:
                continue
            if abs((y0 + y1) / 2 - hp_cy) >= hp_h or (y1 - y0) < hp_h * 0.3:
                continue
            name_boxes.append(b)
        self._read_boxes(frame_bgr, name_boxes, read)
        anchor = find_hp_badge_anchor(to_geoms(self._lines(read, True), w, h), w / h)
        if anchor is None:
            return None, None
        # 番号が見えている表示（バトル場の詳細表示など）なら、拡大して読んだ番号で確定する
        sub = self._identify_roi(frame_bgr, anchor.roi)
        if sub is not None:
            for key, value in result.timings_ms.items():
                sub.timings_ms.setdefault(f"frame_{key}", value)
            return sub, None
        # カード領域内の行（ワザ名・特性名）を読み、アンカーを作り直して text_lines を埋める
        roi = anchor.roi
        column = []
        for b in boxes:
            x0, y0, x1, y1 = box_rect(b)
            if (
                x0 / w >= roi.rx - 0.05
                and x1 / w <= roi.rx + roi.rw + 0.05
                and roi.ry <= y0 / h <= roi.ry + roi.rh
            ):
                column.append(b)
        # 行は全行認識と同じ読み順（上から下）に並べる。検索に使うワザ名は先頭 2 つまでなので、
        # 順番が違うと別の印刷が先頭に来ることがある
        self._read_boxes(frame_bgr, column, read)
        lines = self._lines(read, True)
        anchor = find_hp_badge_anchor(to_geoms(lines, w, h), w / h) or anchor
        refs, reason = self.resolve_by_search(anchor)
        if not refs:
            return None, reason
        result.ocr_lines = lines
        result.anchor_kind = "hp_badge"
        result.card_box = anchor.roi
        result.recognized = RecognizedCard(
            name_en=anchor.name,
            set_code=None,
            number=None,
            hp=anchor.hp,
            confidence=anchor.confidence,
        )
        result.en_candidates = refs
        return self._finish(result, refs[0], reason), None

    def _hp_badge_rois(self, frame_bgr: np.ndarray, hp_lines: list[OcrLine]) -> list[CardBox]:
        """大きな数字で終わる箱（HP バッジ。名前バーと結合していることもある）からカード領域を推定。

        拡大表示のカードは左上に名前、右上に HP バッジがある。バッジの右端からカード幅ぶん左、
        下にカード高さぶんの矩形を、バッジの大きい順に返す。
        場のカードの HP バッジも候補に入るため、呼び出し側は順に試す。
        """
        h, w = frame_bgr.shape[:2]
        rois: list[CardBox] = []
        for ln in hp_lines:
            if not _TRAILING_HP_RE.search(ln.text):
                continue
            x0, y0, x1, y1 = box_rect(ln.box)
            bh = y1 - y0
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

    # 失敗した HP バッジ領域の記憶。盤面のポケモンの HP バッジは演出のたびに候補になるが、
    # 位置も見た目も変わらないので、同じ場所・同じ見た目なら拡大して読み直さない
    @staticmethod
    def _roi_thumb(frame_bgr: np.ndarray, roi: CardBox) -> np.ndarray | None:
        sub = crop(frame_bgr, roi)
        if sub.size == 0:
            return None
        gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, FAILED_ROI_THUMB_SIZE, interpolation=cv2.INTER_AREA).astype(
            np.float32
        )

    def _failed_roi_difference(self, frame_bgr: np.ndarray, roi: CardBox) -> float | None:
        """同じ場所で最近失敗した領域の記憶との最小差分。記憶が無ければ None。"""
        thumb: np.ndarray | None = None
        now = time.monotonic()
        best: float | None = None
        for cached_roi, cached_thumb, at in self._failed_rois:
            if now - at > FAILED_ROI_TTL_S:
                continue
            if (
                abs(cached_roi.rx - roi.rx) > FAILED_ROI_POS_TOLERANCE
                or abs(cached_roi.ry - roi.ry) > FAILED_ROI_POS_TOLERANCE
                or abs(cached_roi.rw - roi.rw) > FAILED_ROI_POS_TOLERANCE
                or abs(cached_roi.rh - roi.rh) > FAILED_ROI_POS_TOLERANCE
            ):
                continue
            if thumb is None:
                thumb = self._roi_thumb(frame_bgr, roi)
                if thumb is None:
                    return None
            diff = float(np.mean(np.abs(thumb - cached_thumb)))
            if best is None or diff < best:
                best = diff
        return best

    def _remember_failed_roi(self, frame_bgr: np.ndarray, roi: CardBox) -> None:
        thumb = self._roi_thumb(frame_bgr, roi)
        if thumb is not None:
            self._failed_rois.append((roi, thumb, time.monotonic()))

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

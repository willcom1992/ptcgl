"""カードDB の検索と英→日の対応付け。

対応付けの出典は Limitless の「JP. Prints」（docs/spec.md「データソース」参照）。
同一イラスト判定はイラストレーター名の一致で行う。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.limitless import (
    JP_SETS_URL,
    CardPage,
    CardRef,
    PrintEntry,
    build_search_query,
    jp_image_url,
    name_variants,
    normalize_search_text,
    parse_card_page,
    parse_jp_set_index,
    parse_search_results,
    search_url,
)
from ptcgl.data.mapping import MappingRecord, MappingStore
from ptcgl.data.sets import find_set

log = logging.getLogger(__name__)

# Limitless のページ（JP. Prints は新弾で増える）は 7 日で取り直す。画像は不変
PAGE_MAX_AGE_SECONDS = 7 * 24 * 3600
# 日本語セット一覧（発売日）も新弾で増えるだけなので 7 日で取り直す
JP_SETS_MAX_AGE_SECONDS = 7 * 24 * 3600

# 日本語の通常拡張パック（SV1S, SV2a, SV10, M1L, M2a ...）。プロモ・構築済みより優先する
_MAIN_EXPANSION_RE = re.compile(r"^(SV|M)\d+[A-Za-z]?$")


@dataclass(frozen=True)
class JapaneseCard:
    ref: CardRef
    name_ja: str
    set_name: str
    illustrator: str | None
    image_url: str
    image_path: Path | None = None

    @property
    def label(self) -> str:
        name = self.name_ja or "(名称未取得)"
        return f"{name}  {self.set_name or self.ref.set_code} #{self.ref.number}"

    def with_image_path(self, path: Path) -> JapaneseCard:
        return JapaneseCard(
            ref=self.ref,
            name_ja=self.name_ja,
            set_name=self.set_name,
            illustrator=self.illustrator,
            image_url=self.image_url,
            image_path=path,
        )


@dataclass
class MatchResult:
    """英語カード 1 枚に対する照合結果。"""

    en: CardPage
    best: JapaneseCard | None
    same_art: bool
    confidence: float
    candidates: list[JapaneseCard] = field(default_factory=list)
    reason: str = ""
    source: str = "auto"  # "auto" | "manual" | "cache"

    @property
    def should_auto_show(self) -> bool:
        return self.best is not None and self.confidence >= 0.8


def _normalize_artist(name: str | None) -> str:
    return re.sub(r"[\s_\-.]", "", (name or "").lower())


def same_illustrator(a: str | None, b: str | None) -> bool:
    """イラストレーター名が一致するか（空白・記号・大文字小文字を無視）。どちらかが空なら False。"""
    na, nb = _normalize_artist(a), _normalize_artist(b)
    return bool(na) and na == nb


def _to_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None


def rank_candidates(
    entries: tuple[PrintEntry, ...],
    en_release: str | None = None,
    jp_releases: Mapping[str, str] | None = None,
) -> list[PrintEntry]:
    """照合で日本語ページを取りに行く順。

    英語版と日本語セットの発売日（YYYY-MM-DD）が分かる印刷は、英語版より前に出たものを
    近い順に、次に英語版より後に出たものを近い順に並べる。同一絵柄はたいてい英語版の直前の
    日本語弾にある（対戦ログ 2026-11-12: 構築済みに何度も再録されたカードで 18 ページ目まで
    たどり 20 秒かかった）。発売日が分からない印刷はその後ろに、通常拡張パック → その他、
    番号の小さい順で並べる。
    """
    en_day = _to_date(en_release)

    def sort_key(e: PrintEntry) -> tuple[int, int, int, int, int, str]:
        tier = 0 if _MAIN_EXPANSION_RE.match(e.ref.set_code) else 1
        digits = re.match(r"\d+", e.ref.number)
        number = int(digits.group(0)) if digits else 10**6
        jp_day = _to_date((jp_releases or {}).get(e.ref.set_code))
        if en_day is None or jp_day is None:
            return (1, 0, 0, tier, number, e.ref.number)
        days = (en_day - jp_day).days
        return (0, 0 if days >= 0 else 1, abs(days), tier, number, e.ref.number)

    return sorted(entries, key=sort_key)


class CardRepository:
    """英語カード情報から日本語カードを検索する。"""

    def __init__(
        self,
        http: CachedHttpClient,
        images_dir: Path,
        mapping: MappingStore | None = None,
    ) -> None:
        self._http = http
        self._images_dir = images_dir
        self._mapping = mapping
        self._jp_releases: tuple[float, dict[str, str]] | None = None  # (取得時刻, 表)

    # --- Limitless ページ取得 -------------------------------------------------

    def get_card_page(self, ref: CardRef) -> CardPage:
        html = self._http.get_text(ref.page_url, max_age=PAGE_MAX_AGE_SECONDS)
        try:
            return parse_card_page(html, ref_hint=ref)
        except ValueError:
            self._http.invalidate(ref.page_url)  # メンテナンス画面等を保存し続けない
            raise

    def jp_release_dates(self) -> dict[str, str]:
        """日本語セットの発売日（セットコード → YYYY-MM-DD）。一覧ページは 7 日ごとに取り直す。"""
        now = time.monotonic()
        if self._jp_releases is None or now - self._jp_releases[0] > JP_SETS_MAX_AGE_SECONDS:
            html = self._http.get_text(JP_SETS_URL, max_age=JP_SETS_MAX_AGE_SECONDS)
            try:
                self._jp_releases = (now, parse_jp_set_index(html))
            except ValueError:
                self._http.invalidate(JP_SETS_URL)  # メンテナンス画面等を保存し続けない
                raise
        return self._jp_releases[1]

    def _rank_for_matching(
        self, en_ref: CardRef, prints: tuple[PrintEntry, ...]
    ) -> list[PrintEntry]:
        """照合で日本語ページを取りに行く順。候補が 2 件以上のときだけ発売日の表を引く。"""
        if len(prints) < 2:
            return list(prints)
        en_set = find_set(en_ref.set_code)
        if en_set is None or not en_set.release_date:
            return rank_candidates(prints)
        try:
            releases = self.jp_release_dates()
        except Exception as exc:  # 一覧が取れなくても照合は従来の順で続ける
            log.warning("日本語セット一覧の取得に失敗（候補は従来の順で試す）: %s", exc)
            return rank_candidates(prints)
        ranked = rank_candidates(prints, en_set.release_date, releases)
        log.info(
            "日本語候補 %d 件を %s（%s）の発売日に近い順に: %s",
            len(ranked),
            en_ref.set_code,
            en_set.release_date,
            [f"{e.ref.set_code}/{e.ref.number}" for e in ranked[:6]],
        )
        return ranked

    def get_japanese_card(self, ref: CardRef, fetch_page: bool = True) -> JapaneseCard:
        """日本語カード情報を返す。`fetch_page=False` ならページを取得せず URL だけ組み立てる。"""
        if not fetch_page:
            return JapaneseCard(
                ref=ref, name_ja="", set_name="", illustrator=None, image_url=jp_image_url(ref)
            )
        page = self.get_card_page(ref)
        return JapaneseCard(
            ref=ref,
            name_ja=page.name,
            set_name=page.set_name,
            illustrator=page.illustrator,
            image_url=page.image_url or jp_image_url(ref),
        )

    def search_english(
        self, name: str, hp: int | None = None, text_terms: list[str] | None = None
    ) -> list[CardRef]:
        """名前・HP・ワザ名で英語カードを検索する（番号が読めない対戦中の詳細表示用）。

        まず名前と HP だけで検索する（OCR の揺れが少なくキャッシュが効き、1 件に絞れることが多い）。
        複数件のときだけワザ名を加えて絞り込み、0 件なら最も不確かな（末尾の）条件から外して
        再検索する。どの条件でも絞れなければ名前と HP の結果を返す。
        """
        name = normalize_search_text(name)
        refs = self._search(build_search_query(name, hp, None))
        if not refs:
            # 名前に "ex" 等が続けて読まれていると 0 件になる。外した名前で引き直す
            for variant in name_variants(name)[1:]:
                refs = self._search(build_search_query(variant, hp, None))
                if refs:
                    log.info("名前 %r では 0 件。%r で検索して %d 件", name, variant, len(refs))
                    name = variant
                    break
        terms = [normalize_search_text(t) for t in text_terms or []]
        if len(refs) <= 1:
            return refs
        while terms:
            narrowed = self._search(build_search_query(name, hp, terms))
            if narrowed:
                return narrowed
            terms.pop()
        return refs

    def _search(self, query: str) -> list[CardRef]:
        html = self._http.get_text(search_url(query), max_age=PAGE_MAX_AGE_SECONDS)
        refs, _ = parse_search_results(html)
        return refs

    def image_path_for(self, ref: CardRef) -> Path:
        return self._images_dir / ref.lang / f"{ref.set_code}_{ref.number}.png"

    def download_image(self, card: JapaneseCard) -> JapaneseCard:
        path = self._http.get_file(card.image_url, self.image_path_for(card.ref))
        return card.with_image_path(path)

    # --- 照合 -------------------------------------------------------------------

    def find_japanese(self, en_ref: CardRef, use_saved: bool = True) -> MatchResult:
        """英語カードに対応する日本語カードを探す。

        1. 保存済み対応表（自動確定・手動選択）があればそれを使う。
        2. Limitless 英語ページの JP. Prints を候補とし、イラストレーター一致を同一絵柄とする。
        3. 同一絵柄が無ければ別イラスト候補として返し、自動確定しない。
        """
        en_page = self.get_card_page(en_ref)

        if not en_page.jp_prints:
            return MatchResult(
                en=en_page,
                best=None,
                same_art=False,
                confidence=0.0,
                reason="日本語版の印刷が見つかりません",
            )

        if use_saved and self._mapping is not None:
            saved = self._mapping.get(en_ref)
            if saved is not None:
                entries = rank_candidates(en_page.jp_prints)
                try:
                    card = self.get_japanese_card(saved.jp)
                except (
                    Exception
                ) as exc:  # ページ欠落等。保存済みの対応は維持し、画像 URL だけで扱う
                    log.warning("保存済み日本語カードページの取得に失敗: %s (%s)", saved.jp, exc)
                    card = self.get_japanese_card(saved.jp, fetch_page=False)
                others = [
                    self.get_japanese_card(e.ref, fetch_page=False)
                    for e in entries
                    if e.ref != saved.jp
                ]
                return MatchResult(
                    en=en_page,
                    best=card,
                    same_art=saved.same_art,
                    confidence=1.0,
                    candidates=[card, *others],
                    reason="保存済みの対応付け",
                    source="cache" if saved.source == "auto" else "manual",
                )

        entries = self._rank_for_matching(en_ref, en_page.jp_prints)
        en_artist = _normalize_artist(en_page.illustrator)
        fetched: dict[CardRef, JapaneseCard] = {}
        best: JapaneseCard | None = None
        for entry in entries:
            try:
                card = self.get_japanese_card(entry.ref)
            except Exception as exc:  # ページ欠落等。候補から外さず画像 URL だけで扱う
                log.warning("日本語カードページの取得に失敗: %s (%s)", entry.ref, exc)
                card = self.get_japanese_card(entry.ref, fetch_page=False)
            fetched[entry.ref] = card
            if en_artist and _normalize_artist(card.illustrator) == en_artist:
                best = card
                break

        candidates = [
            fetched.get(e.ref) or self.get_japanese_card(e.ref, fetch_page=False) for e in entries
        ]

        if best is not None:
            candidates.remove(best)
            candidates.insert(0, best)
            if self._mapping is not None:
                saved = self._mapping.get(en_ref)
                if saved is None or saved.source != "manual":  # 手動選択は自動結果で上書きしない
                    self._mapping.put(
                        MappingRecord(en=en_ref, jp=best.ref, same_art=True, source="auto")
                    )
            return MatchResult(
                en=en_page,
                best=best,
                same_art=True,
                confidence=0.95,
                candidates=candidates,
                reason=f"イラストレーター一致: {en_page.illustrator}",
            )

        # 同一絵柄なし: 先頭候補を暫定表示用に返すが、自動確定はしない
        return MatchResult(
            en=en_page,
            best=candidates[0],
            same_art=False,
            confidence=0.5,
            candidates=candidates,
            reason="同一イラストの日本語版が見つからないため別イラスト候補を表示",
        )

    def confirm(self, en_ref: CardRef, jp: JapaneseCard, same_art: bool) -> None:
        """ユーザーが候補から選択した結果を保存する。"""
        if self._mapping is not None:
            self._mapping.put(
                MappingRecord(en=en_ref, jp=jp.ref, same_art=same_art, source="manual")
            )

"""カードDB の検索と英→日の対応付け。

対応付けの出典は Limitless の「JP. Prints」（docs/spec.md「データソース」参照）。
同一イラスト判定はイラストレーター名の一致で行う。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ptcgl.data.cache import CachedHttpClient
from ptcgl.data.limitless import (
    CardPage,
    CardRef,
    PrintEntry,
    build_search_query,
    jp_image_url,
    normalize_search_text,
    parse_card_page,
    parse_search_results,
    search_url,
)
from ptcgl.data.mapping import MappingRecord, MappingStore

log = logging.getLogger(__name__)

# Limitless のページ（JP. Prints は新弾で増える）は 7 日で取り直す。画像は不変
PAGE_MAX_AGE_SECONDS = 7 * 24 * 3600

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


def rank_candidates(entries: tuple[PrintEntry, ...]) -> list[PrintEntry]:
    """照合の優先順: 通常拡張パック → その他、同一枠内は番号の小さい順。"""

    def sort_key(e: PrintEntry) -> tuple[int, int, str]:
        tier = 0 if _MAIN_EXPANSION_RE.match(e.ref.set_code) else 1
        digits = re.match(r"\d+", e.ref.number)
        number = int(digits.group(0)) if digits else 10**6
        return (tier, number, e.ref.number)

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

    # --- Limitless ページ取得 -------------------------------------------------

    def get_card_page(self, ref: CardRef) -> CardPage:
        html = self._http.get_text(ref.page_url, max_age=PAGE_MAX_AGE_SECONDS)
        try:
            return parse_card_page(html, ref_hint=ref)
        except ValueError:
            self._http.invalidate(ref.page_url)  # メンテナンス画面等を保存し続けない
            raise

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
        entries = rank_candidates(en_page.jp_prints)

        if not entries:
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

"""Limitless TCG（limitlesstcg.com）のカードページ解析。

Limitless には API が無いため HTML を解析する。英語カードページの「JP. Prints」が
日本語版への対応付け、日本語カードページの og:image が日本語カード画像の出典になる。
セレクタはこのモジュールに集約し、HTML 変更は tests/fixtures/html のテストで検知する。

利用条件: 個人利用・ローカルキャッシュのみ。アクセスは CachedHttpClient の間隔制御に従う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://limitlesstcg.com"
CDN_BASE = "https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com"

# 例: "Pineco - Scarlet & Violet (SVI) #1 – Limitless"
_TITLE_RE = re.compile(
    r"^(?P<name>.+?) - (?P<set_name>.+?) \((?P<set_code>[^)]+)\) #(?P<number>\S+)"
)
_HP_RE = re.compile(r"(\d{2,3})\s*HP")
_PRINT_HREF_RE = re.compile(r"^/cards/(?:(?P<lang>jp|en)/)?(?P<set_code>[^/]+)/(?P<number>[^/?#]+)")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9-]{1,16}")


@dataclass(frozen=True, order=True)
class CardRef:
    """言語・セットコード・番号でカードの印刷を一意に表す。

    セットコードは言語ごとに衝突する（日本語の SVI はバトルアカデミー）ため言語を必ず持つ。
    """

    lang: str  # "en" | "jp"
    set_code: str
    number: str

    def __post_init__(self) -> None:
        # URL とファイル名に使うため、英数字とハイフン以外は受け付けない
        if self.lang not in ("en", "jp"):
            raise ValueError(f"不正な言語: {self.lang!r}")
        if not _SAFE_TOKEN_RE.fullmatch(self.set_code):
            raise ValueError(f"不正なセットコード: {self.set_code!r}")
        if not _SAFE_TOKEN_RE.fullmatch(self.number):
            raise ValueError(f"不正なカード番号: {self.number!r}")

    @property
    def page_url(self) -> str:
        if self.lang == "jp":
            return f"{BASE_URL}/cards/jp/{self.set_code}/{self.number}"
        return f"{BASE_URL}/cards/{self.set_code}/{self.number}"

    @property
    def key(self) -> str:
        return f"{self.lang}:{self.set_code}:{self.number}"

    def __str__(self) -> str:
        return f"{self.set_code} #{self.number} ({self.lang})"


@dataclass(frozen=True)
class PrintEntry:
    """「Int. Prints」「JP. Prints」の 1 行。"""

    ref: CardRef
    set_name: str


@dataclass(frozen=True)
class CardPage:
    """Limitless のカードページから抽出した情報（英語・日本語共通）。"""

    ref: CardRef
    name: str
    set_name: str
    illustrator: str | None
    hp: int | None
    image_url: str | None
    rarity: str | None
    int_prints: tuple[PrintEntry, ...] = field(default_factory=tuple)
    jp_prints: tuple[PrintEntry, ...] = field(default_factory=tuple)


def jp_image_url(ref: CardRef, size: str = "LG") -> str:
    """日本語カード画像 URL を組み立てる（番号はゼロ埋め無し）。"""
    return f"{CDN_BASE}/tpc/{ref.set_code}/{ref.set_code}_{ref.number}_R_JP_{size}.png"


def en_image_url(ref: CardRef, size: str = "LG") -> str:
    """英語カード画像 URL を組み立てる（番号は 3 桁ゼロ埋め）。"""
    number = ref.number.zfill(3) if ref.number.isdigit() else ref.number
    return f"{CDN_BASE}/tpci/{ref.set_code}/{ref.set_code}_{number}_R_EN_{size}.png"


def parse_print_href(href: str, default_lang: str) -> CardRef | None:
    m = _PRINT_HREF_RE.match(href)
    if not m:
        return None
    lang = m.group("lang") or default_lang
    try:
        return CardRef(lang=lang, set_code=m.group("set_code"), number=m.group("number"))
    except ValueError:
        return None


_SEARCH_COUNT_RE = re.compile(r"(\d+)\s+cards?\s+found")
_SEARCH_LINK_RE = re.compile(r"^/cards/(?P<set>[A-Z0-9-]+)/(?P<number>[0-9A-Z]+)$")


def search_url(query: str) -> str:
    from urllib.parse import quote_plus

    return f"{BASE_URL}/cards?q={quote_plus(query)}"


def build_search_query(
    name: str,
    hp: int | None = None,
    text_terms: list[str] | None = None,
    unique: bool = True,
) -> str:
    """名前・HP・ワザ名から Limitless の検索クエリを組み立てる。"""
    # 先頭の "-" や ":" は Limitless の検索演算子になるので落とす
    parts = [name.strip().lstrip("-:").strip()]
    if hp:
        parts.append(f"hp:{hp}")
    for term in text_terms or []:
        term = term.strip().replace(" ", "_")
        if len(term) >= 3:
            parts.append(f"text:{term}")
    if unique:
        parts.append("unique:cards")
    return " ".join(parts)


def parse_search_results(html: str) -> tuple[list[CardRef], int | None]:
    """検索結果ページから英語カードの参照一覧と件数を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    count: int | None = None
    m = _SEARCH_COUNT_RE.search(soup.get_text(" ", strip=True))
    if m:
        count = int(m.group(1))
    refs: list[CardRef] = []
    seen: set[CardRef] = set()
    grid = soup.find("div", class_="card-search-grid") or soup
    for a in grid.find_all("a", href=True):
        lm = _SEARCH_LINK_RE.match(str(a["href"]))
        if not lm:
            continue
        if lm.group("set") in ("jp", "en"):
            continue
        ref = CardRef("en", lm.group("set"), lm.group("number"))
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs, count


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if tag else ""


def _parse_prints_table(
    soup: BeautifulSoup, heading: str, default_lang: str
) -> tuple[PrintEntry, ...]:
    """「Int. Prints」「JP. Prints」見出し以降の行からリンクを集める。"""
    th = soup.find("th", string=lambda s: s is not None and s.strip() == heading)
    if th is None:
        return ()
    entries: list[PrintEntry] = []
    row = th.find_parent("tr")
    while row is not None:
        row = row.find_next_sibling("tr")
        if row is None:
            break
        # 次の見出し行に到達したら終了
        if row.find("th") is not None:
            break
        first_cell = row.find("td")
        if first_cell is None:
            continue
        if "jp-print-toggle" in (first_cell.get("class") or []):
            continue
        link = row.find("a", href=True)
        if link is None:
            continue
        ref = parse_print_href(str(link["href"]), default_lang)
        if ref is None:
            continue
        number_span = link.find("span", class_="prints-table-card-number")
        if number_span is not None:
            number_span.extract()
        entries.append(PrintEntry(ref=ref, set_name=_text(link)))
    return tuple(entries)


def parse_card_page(html: str, ref_hint: CardRef | None = None) -> CardPage:
    """カードページ HTML を解析する。`ref_hint` は URL から分かる場合に渡す。"""
    soup = BeautifulSoup(html, "html.parser")

    title = _text(soup.find("title"))
    m = _TITLE_RE.match(title)
    if not m:
        raise ValueError(f"カードページのタイトルを解析できません: {title!r}")
    name = m.group("name")
    set_name = m.group("set_name")
    set_code = m.group("set_code")
    number = m.group("number")

    lang = ref_hint.lang if ref_hint else "en"
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        parsed = parse_print_href(str(canonical["href"]).replace(BASE_URL, ""), lang)
        if parsed:
            lang = parsed.lang
    ref = CardRef(lang=lang, set_code=set_code, number=number)

    illustrator: str | None = None
    artist_div = soup.find("div", class_="card-text-artist")
    if artist_div is not None:
        link = artist_div.find("a")
        illustrator = _text(link) or None

    hp: int | None = None
    title_p = soup.find("p", class_="card-text-title")
    if title_p is not None:
        hp_match = _HP_RE.search(_text(title_p))
        if hp_match:
            hp = int(hp_match.group(1))

    image_url: str | None = None
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        image_url = str(og["content"]).replace("_SM.png", "_LG.png")

    rarity: str | None = None
    details = soup.find("div", class_="prints-current-details")
    if details is not None:
        spans = details.find_all("span")
        if len(spans) >= 2:
            detail_text = _text(spans[-1])
            if "·" in detail_text:
                rarity = detail_text.split("·", 1)[1].strip() or None

    int_prints = _parse_prints_table(soup, "Int. Prints", "en")
    jp_prints = _parse_prints_table(soup, "JP. Prints", "jp")

    return CardPage(
        ref=ref,
        name=name,
        set_name=set_name,
        illustrator=illustrator,
        hp=hp,
        image_url=image_url,
        rarity=rarity,
        int_prints=int_prints,
        jp_prints=jp_prints,
    )

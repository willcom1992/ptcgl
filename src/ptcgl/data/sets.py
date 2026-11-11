"""英語セット表（PTCGL のセットコード・番号の分母）。

`en_sets.json` は scripts/build_en_sets.py で TCGdex から生成する。パッケージ同梱
データなので、PyInstaller 実行時も importlib.resources で参照できる。

新しいセットが出るたびにアプリを配り直さなくて済むよう、起動時に `refresh_en_sets()` で
このリポジトリの main にある最新の表を取得する（1 日 1 回、`data/cache.py` 経由）。
取得できない・壊れている・同梱版より新しくない場合は同梱版をそのまま使う。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ptcgl.data.cache import CachedHttpClient

log = logging.getLogger(__name__)

REMOTE_SETS_URL = (
    "https://raw.githubusercontent.com/willcom1992/ptcgl/main/src/ptcgl/data/en_sets.json"
)
REMOTE_MAX_AGE = 24 * 60 * 60.0  # 取り直す間隔（秒）
REMOTE_TIMEOUT = 5.0  # 取得中は他の HTTP 取得（カード情報）を待たせるので短くする
_REMOTE_MAX_BYTES = 1_000_000  # 現状は約 10 KB。想定外に大きい応答は採用しない
_MAX_SETS = 1000
_CODE_RE = re.compile(r"[A-Z0-9]{2,5}")
_PREFIX_RE = re.compile(r"[A-Z]{0,3}")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})?")
_UPDATED_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
UPDATED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"  # UTC。文字列のまま大小比較できる


@dataclass(frozen=True)
class EnSet:
    code: str  # PTCGL / Limitless のセットコード（例 "SVI"）
    tcgdex_id: str
    name: str
    official: int  # カード番号の分母（"123/198" の 198）
    total: int
    release_date: str
    number_prefix: str = ""  # Trainer Gallery 等のサブセット番号の接頭辞（"TG01/TG30" の "TG"）


@dataclass(frozen=True)
class SetTable:
    updated: str  # 表を生成した日時（UTC）。同梱版と取得版のどちらが新しいかの判定に使う
    sets: tuple[EnSet, ...]


def _text(row: dict, key: str, default: str | None = None) -> str:
    value = row.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} が文字列ではありません: {value!r}")
    return value


def _count(row: dict, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
        raise ValueError(f"{key} が 0〜999 の整数ではありません: {value!r}")
    return value


def parse_set_table(text: str) -> SetTable:
    """en_sets.json の内容を検証して読み込む。形式が違えば ValueError。"""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"JSON として読めません: {exc!r}") from exc
    if not isinstance(doc, dict):
        raise ValueError("トップレベルがオブジェクトではありません")
    updated, rows = doc.get("updated"), doc.get("sets")
    if not isinstance(updated, str) or not _UPDATED_RE.fullmatch(updated):
        raise ValueError(f"updated が不正です: {updated!r}")
    if not isinstance(rows, list) or not 0 < len(rows) <= _MAX_SETS:
        raise ValueError("sets が空、または多すぎます")
    sets: list[EnSet] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError("セットの行がオブジェクトではありません")
        s = EnSet(
            code=_text(r, "code"),
            tcgdex_id=_text(r, "tcgdex_id"),
            name=_text(r, "name"),
            official=_count(r, "official"),
            total=_count(r, "total"),
            release_date=_text(r, "release_date", ""),
            number_prefix=_text(r, "number_prefix", ""),
        )
        # コードは Limitless の URL に入るので、想定した文字だけを通す
        if not _CODE_RE.fullmatch(s.code) or not _PREFIX_RE.fullmatch(s.number_prefix):
            raise ValueError(f"セットコードが不正です: {s.code!r} {s.number_prefix!r}")
        if not _DATE_RE.fullmatch(s.release_date):
            raise ValueError(f"発売日が不正です: {s.code} {s.release_date!r}")
        if s.tcgdex_id in seen:
            raise ValueError(f"セットが重複しています: {s.tcgdex_id!r}")
        seen.add(s.tcgdex_id)
        sets.append(s)
    return SetTable(updated, tuple(sets))


def _load_bundled() -> SetTable:
    text = resources.files("ptcgl.data").joinpath("en_sets.json").read_text(encoding="utf-8")
    return parse_set_table(text)


# 現在有効な表。差し替えは参照の代入だけなので、認識中のワーカースレッドからは
# 常に新旧どちらか一方の完全な表が見える。ロックは代入の競合を防ぐためのもの
_active: SetTable | None = None
_active_lock = threading.Lock()


def _table() -> SetTable:
    global _active
    current = _active
    if current is None:
        with _active_lock:
            if _active is None:
                _active = _load_bundled()
            current = _active
    return current


def load_en_sets() -> tuple[EnSet, ...]:
    return _table().sets


def _parse_remote(data: bytes, current: SetTable) -> SetTable:
    """取得した表を検証する。採用できない内容なら ValueError。"""
    if len(data) > _REMOTE_MAX_BYTES:
        raise ValueError(f"大きすぎます（{len(data)} バイト）")
    remote = parse_set_table(data.decode("utf-8"))  # UnicodeDecodeError も ValueError
    limit = (datetime.now(timezone.utc) + timedelta(days=1)).strftime(UPDATED_FORMAT)
    if remote.updated > limit:
        raise ValueError(f"updated が未来の日時です: {remote.updated}")
    if len(remote.sets) < len(current.sets) // 2:
        # セットは増える一方。大きく減っていたら生成の失敗とみなす
        raise ValueError(f"セット数が少なすぎます（{len(remote.sets)} 件）")
    return remote


def refresh_en_sets(http: CachedHttpClient, max_age: float = REMOTE_MAX_AGE) -> bool:
    """最新のセット表を取得して差し替える。差し替えたら True。

    ネットワークに出るのは `max_age` に 1 回だけで、失敗しても例外は投げない
    （前回取得した表、それも無ければ同梱版で動き続ける）。ブロックするので UI スレッドから呼ばない。
    """
    global _active
    current = _table()
    try:
        # 起動のたびに呼ぶので、つながらないときは粘らず 1 回であきらめる。
        # 不正な内容（公衆無線 LAN のログイン画面など）で前回の取得分を上書きしないよう、
        # 保存前に検証する
        data = http.get_bytes(
            REMOTE_SETS_URL,
            max_age=max_age,
            stale_if_error=True,
            max_retries=0,
            timeout=REMOTE_TIMEOUT,
            validate=lambda content: _parse_remote(content, current),
        )
        remote = _parse_remote(data, current)
    except (httpx.HTTPError, OSError) as exc:
        log.info("セット表を更新できません（手元の表を使います）: %s", exc)
        return False
    except ValueError as exc:
        log.warning("取得したセット表が不正なため使いません: %s", exc)
        http.invalidate(REMOTE_SETS_URL)  # 以前に保存した内容が今の検証に通らない場合は捨てる
        return False
    if remote.updated <= current.updated:
        # 同梱版と同じ、またはアプリ更新前の古いキャッシュ・開発中に手元の表を先に更新した場合
        log.debug("セット表は最新です（手元 %s / 取得 %s）", current.updated, remote.updated)
        return False
    with _active_lock:
        _active = remote
    log.info(
        "セット表を更新しました: %d → %d セット（%s 版）",
        len(current.sets),
        len(remote.sets),
        remote.updated,
    )
    return True


def find_set(code: str) -> EnSet | None:
    code = code.upper()
    for s in load_en_sets():
        if s.code == code and not s.number_prefix:
            return s
    return None


def sets_by_official_total(total: int, number_prefix: str = "") -> list[EnSet]:
    """番号の分母が一致するセットを新しい順に返す（衝突することがある）。"""
    prefix = number_prefix.upper()
    matches = [s for s in load_en_sets() if s.official == total and s.number_prefix == prefix]
    matches.sort(key=lambda s: s.release_date, reverse=True)
    return matches


def known_codes() -> frozenset[str]:
    return frozenset(s.code for s in load_en_sets())

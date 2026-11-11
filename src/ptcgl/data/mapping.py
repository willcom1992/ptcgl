"""英語カード → 日本語カードの対応付けを SQLite に保存する。"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ptcgl.data.limitless import CardRef

_SCHEMA = """
CREATE TABLE IF NOT EXISTS en_jp_map (
    en_set    TEXT NOT NULL,
    en_number TEXT NOT NULL,
    jp_set    TEXT NOT NULL,
    jp_number TEXT NOT NULL,
    same_art  INTEGER NOT NULL,
    source    TEXT NOT NULL,
    updated   REAL NOT NULL,
    PRIMARY KEY (en_set, en_number)
);
"""


@dataclass(frozen=True)
class MappingRecord:
    en: CardRef
    jp: CardRef
    same_art: bool
    source: str  # "auto" | "manual"


class MappingStore:
    """確定した対応付け（自動・手動）の永続化。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()  # UI スレッドと認識ワーカーの両方から使われる
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def get(self, en: CardRef) -> MappingRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT jp_set, jp_number, same_art, source FROM en_jp_map "
                "WHERE en_set=? AND en_number=?",
                (en.set_code, en.number),
            ).fetchone()
        if row is None:
            return None
        jp = CardRef(lang="jp", set_code=row[0], number=row[1])
        return MappingRecord(en=en, jp=jp, same_art=bool(row[2]), source=row[3])

    def put(self, record: MappingRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO en_jp_map VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.en.set_code,
                    record.en.number,
                    record.jp.set_code,
                    record.jp.number,
                    int(record.same_art),
                    record.source,
                    time.time(),
                ),
            )
            self._conn.commit()

    def delete(self, en: CardRef) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM en_jp_map WHERE en_set=? AND en_number=?", (en.set_code, en.number)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

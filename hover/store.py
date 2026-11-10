"""A personal glossary of everything looked up, exportable to Anki."""

import csv
import sqlite3
import threading
import time
from pathlib import Path

MAX_TERM_CHARS = 80  # longer items are passages, not glossary terms
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
SCHEMA = """CREATE TABLE IF NOT EXISTS lookups (
    term TEXT NOT NULL, mode TEXT NOT NULL, answer TEXT NOT NULL, source TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    PRIMARY KEY (term, mode))"""


def inert(cell: str) -> str:
    """Stop spreadsheet apps from treating a copied "=cmd|..." term as a formula if the CSV is opened in Excel."""
    return f"'{cell}" if cell[:1] in FORMULA_STARTS else cell


class Glossary:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # lookups finish on worker threads
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(SCHEMA)

    def add(self, term: str, mode: str, answer: str, source: str) -> int:
        """Record a lookup and return how many times this term has been looked up in this mode (0 if not a term)."""
        term = term.strip()
        if not term or len(term) > MAX_TERM_CHARS or not answer:
            return 0
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO lookups VALUES (?, ?, ?, ?, 1, ?, ?) ON CONFLICT (term, mode) DO UPDATE SET "
                "answer = excluded.answer, source = excluded.source, count = count + 1, last_seen = excluded.last_seen",
                (term, mode, answer, source, now, now),
            )
            return self._db.execute("SELECT count FROM lookups WHERE term = ? AND mode = ?", (term, mode)).fetchone()[0]

    def rows(self) -> list[tuple[str, str, str, str, int]]:
        with self._lock:
            return self._db.execute(
                "SELECT term, mode, answer, source, count FROM lookups ORDER BY last_seen DESC"
            ).fetchall()

    def export_anki(self, path: Path) -> int:
        """Write an Anki-importable CSV (front = term, back = answer + source). Returns the number of cards."""
        rows = self.rows()
        with path.open("w", newline="", encoding="utf-8") as file:
            file.write("#separator:Comma\n#html:false\n#columns:Front,Back,Tags\n")
            writer = csv.writer(file)
            for term, mode, answer, source, _ in rows:
                writer.writerow([inert(term), inert(f"{answer}\n\n{source}".strip()), f"hover {mode}"])
        return len(rows)

    def close(self) -> None:
        self._db.close()

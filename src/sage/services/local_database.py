"""Persistent local SQLite index for conversational retrieval."""

from __future__ import annotations

import ntpath
import os
import sqlite3
import time
from pathlib import Path

from ..models import ScanResult
from .retrieval import assess_path

_APPROVED_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".csv", ".log"
}
_SENSITIVE_SEGMENTS = {"appdata", "credentials", "secrets", "private", "token", "password"}
_REPARSE_ATTRIBUTE = 0x400


class DriveIndex:
    """Stores scan metadata and optional safe text excerpts for local retrieval."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        max_content_bytes: int = 128 * 1024,
        max_chunks_per_file: int = 8,
    ) -> None:
        self.db_path = db_path or self.default_path()
        self.max_content_bytes = max_content_bytes
        self.max_chunks_per_file = max_chunks_per_file
        self._ready = False

    @staticmethod
    def default_path() -> str:
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            root = str(Path.home() / ".sage")
        path = Path(root) / "SAGE" / "index.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def ensure_schema(self) -> None:
        if self._ready:
            return
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    parent_path TEXT,
                    name TEXT NOT NULL,
                    extension TEXT,
                    is_directory INTEGER NOT NULL,
                    logical_bytes INTEGER,
                    allocated_bytes INTEGER,
                    modified_time INTEGER,
                    scan_drive TEXT,
                    indexed_at INTEGER NOT NULL,
                    content_indexed INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_path);
                CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
                CREATE INDEX IF NOT EXISTS idx_files_drive ON files(scan_drive);

                CREATE TABLE IF NOT EXISTS content_chunks (
                    path TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    snippet TEXT NOT NULL,
                    PRIMARY KEY (path, chunk_index)
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(path, snippet);
                """
            )
        self._ready = True

    @staticmethod
    def _safe_for_content_index(path: str, extension: str, is_reparse: bool) -> bool:
        if is_reparse or extension not in _APPROVED_TEXT_EXTENSIONS:
            return False
        parts = {part.casefold() for part in ntpath.normpath(path).split("\\") if part}
        return not bool(parts & _SENSITIVE_SEGMENTS)

    def _replace_content(self, conn: sqlite3.Connection, path: str, text: str) -> None:
        conn.execute("DELETE FROM content_chunks WHERE path = ?", (path,))
        conn.execute("DELETE FROM content_fts WHERE path = ?", (path,))
        chunk_size = 500
        for index, start in enumerate(range(0, len(text), chunk_size)):
            if index >= self.max_chunks_per_file:
                break
            snippet = text[start : start + chunk_size].strip()
            if not snippet:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO content_chunks(path, chunk_index, snippet) VALUES(?, ?, ?)",
                (path, index, snippet),
            )
            conn.execute("INSERT INTO content_fts(path, snippet) VALUES(?, ?)", (path, snippet))

    def _read_text_excerpt(self, path: str) -> str | None:
        try:
            size = os.path.getsize(path)
            if size <= 0 or size > self.max_content_bytes:
                return None
            with open(path, "rb") as stream:
                raw = stream.read(self.max_content_bytes + 1)
            if len(raw) > self.max_content_bytes or b"\x00" in raw:
                return None
            return raw.decode("utf-8", errors="ignore")
        except OSError:
            return None

    def update_scan(self, result: ScanResult) -> None:
        """Incrementally upsert metadata and index safe local text excerpts."""
        self.ensure_schema()
        now = int(time.time())
        with self._connect() as conn:
            for record in result.records.values():
                path = result.path_for(record.frn)
                if not path:
                    continue
                parent = ntpath.dirname(path.rstrip("\\")) if path.rstrip("\\") != result.volume.drive.rstrip("\\") else None
                extension = ntpath.splitext(record.name)[1].casefold()
                is_reparse = bool(record.attributes & _REPARSE_ATTRIBUTE)
                conn.execute(
                    """
                    INSERT INTO files(path, parent_path, name, extension, is_directory, logical_bytes, allocated_bytes, modified_time, scan_drive, indexed_at, content_indexed)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT content_indexed FROM files WHERE path = ?), 0))
                    ON CONFLICT(path) DO UPDATE SET
                        parent_path=excluded.parent_path,
                        name=excluded.name,
                        extension=excluded.extension,
                        is_directory=excluded.is_directory,
                        logical_bytes=excluded.logical_bytes,
                        allocated_bytes=excluded.allocated_bytes,
                        modified_time=excluded.modified_time,
                        scan_drive=excluded.scan_drive,
                        indexed_at=excluded.indexed_at
                    """,
                    (
                        path,
                        parent,
                        record.name,
                        extension,
                        1 if record.is_directory else 0,
                        int(record.logical_size or 0),
                        int(record.allocated_size or 0),
                        int(record.modified or 0),
                        result.volume.drive.upper(),
                        now,
                        path,
                    ),
                )
                if record.is_directory:
                    continue
                if not self._safe_for_content_index(path, extension, is_reparse):
                    continue
                text = self._read_text_excerpt(path)
                if not text:
                    continue
                self._replace_content(conn, path, text)
                conn.execute("UPDATE files SET content_indexed=1 WHERE path = ?", (path,))

    def search_names(self, query: str, *, scope: str | None = None, limit: int = 10) -> list[dict]:
        self.ensure_schema()
        term = query.strip()
        if not term:
            return []
        where = "(name LIKE ? OR path LIKE ?)"
        params: list[object] = [f"%{term}%", f"%{term}%"]
        if scope:
            where += " AND path LIKE ?"
            params.append(ntpath.normpath(scope).rstrip("\\") + "%")
        params.append(max(1, min(limit, 50)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT path, name, is_directory, logical_bytes, allocated_bytes, modified_time
                FROM files
                WHERE {where}
                ORDER BY allocated_bytes DESC, path ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_content(self, query: str, *, scope: str | None = None, limit: int = 10) -> list[dict]:
        self.ensure_schema()
        term = query.strip()
        if not term:
            return []
        with self._connect() as conn:
            try:
                if scope:
                    rows = conn.execute(
                        """
                        SELECT c.path, c.chunk_index, c.snippet
                        FROM content_chunks c
                        JOIN content_fts f ON f.path = c.path AND f.snippet = c.snippet
                        WHERE f.snippet MATCH ? AND c.path LIKE ?
                        ORDER BY c.path, c.chunk_index
                        LIMIT ?
                        """,
                        (term, ntpath.normpath(scope).rstrip("\\") + "%", max(1, min(limit, 50))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT c.path, c.chunk_index, c.snippet
                        FROM content_chunks c
                        JOIN content_fts f ON f.path = c.path AND f.snippet = c.snippet
                        WHERE f.snippet MATCH ?
                        ORDER BY c.path, c.chunk_index
                        LIMIT ?
                        """,
                        (term, max(1, min(limit, 50))),
                    ).fetchall()
            except sqlite3.OperationalError:
                if scope:
                    rows = conn.execute(
                        """
                        SELECT path, chunk_index, snippet
                        FROM content_chunks
                        WHERE snippet LIKE ? AND path LIKE ?
                        ORDER BY path, chunk_index
                        LIMIT ?
                        """,
                        (f"%{term}%", ntpath.normpath(scope).rstrip("\\") + "%", max(1, min(limit, 50))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT path, chunk_index, snippet
                        FROM content_chunks
                        WHERE snippet LIKE ?
                        ORDER BY path, chunk_index
                        LIMIT ?
                        """,
                        (f"%{term}%", max(1, min(limit, 50))),
                    ).fetchall()
        return [dict(row) for row in rows]

    def list_children(self, path: str, *, limit: int = 25) -> list[dict]:
        self.ensure_schema()
        normalized = ntpath.normpath(path).rstrip("\\")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT path, name, is_directory, logical_bytes, allocated_bytes, modified_time
                FROM files
                WHERE parent_path = ?
                ORDER BY is_directory DESC, allocated_bytes DESC, name ASC
                LIMIT ?
                """,
                (normalized, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def summarize_folder(self, path: str) -> dict:
        self.ensure_schema()
        normalized = ntpath.normpath(path).rstrip("\\")
        prefix = normalized + "\\%"
        with self._connect() as conn:
            direct = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN is_directory = 1 THEN 1 ELSE 0 END) AS direct_folders,
                    SUM(CASE WHEN is_directory = 0 THEN 1 ELSE 0 END) AS direct_files
                FROM files
                WHERE parent_path = ?
                """,
                (normalized,),
            ).fetchone()
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_items,
                    SUM(CASE WHEN is_directory = 0 THEN allocated_bytes ELSE 0 END) AS total_allocated
                FROM files
                WHERE path = ? OR path LIKE ?
                """,
                (normalized, prefix),
            ).fetchone()
        return {
            "path": path,
            "direct_folders": int((direct["direct_folders"] or 0) if direct else 0),
            "direct_files": int((direct["direct_files"] or 0) if direct else 0),
            "total_items": int((totals["total_items"] or 0) if totals else 0),
            "total_allocated": int((totals["total_allocated"] or 0) if totals else 0),
        }

    def find_large_files(self, *, scope: str | None = None, limit: int = 10) -> list[dict]:
        self.ensure_schema()
        where = "is_directory = 0"
        params: list[object] = []
        if scope:
            where += " AND path LIKE ?"
            params.append(ntpath.normpath(scope).rstrip("\\") + "%")
        params.append(max(1, min(limit, 50)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT path, name, logical_bytes, allocated_bytes, modified_time
                FROM files
                WHERE {where}
                ORDER BY allocated_bytes DESC, path ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def retrieve_context(self, query: str, *, scope: str | None = None, limit: int = 8) -> dict:
        names = self.search_names(query, scope=scope, limit=limit)
        content = self.search_content(query, scope=scope, limit=limit)
        citations = [
            {"path": item["path"], "source": "name_search"}
            for item in names
        ] + [
            {"path": item["path"], "source": "content_search", "snippet": item.get("snippet", "")[:180]}
            for item in content
        ]
        return {
            "items": names,
            "content_matches": content,
            "citations": citations[: max(1, min(limit, 20))],
        }

    def impact_analysis(self, path: str, action: str) -> dict:
        risk = assess_path(path)
        return {
            "path": path,
            "action": action,
            "risk": risk.get("risk", "unknown"),
            "guidance": risk.get("guidance", "Unknown impact."),
            "read_only": True,
        }

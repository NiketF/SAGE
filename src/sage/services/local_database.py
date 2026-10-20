"""Persistent local SQLite index for conversational retrieval."""

from __future__ import annotations

import ntpath
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..models import ScanResult
from .retrieval import assess_path, ScanEvidence

_APPROVED_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".xml", ".html", ".csv", ".log"
}
_SENSITIVE_SEGMENTS = {"appdata", "credentials", "secrets", "private", "token", "tokens", "password", "passwords", ".ssh", ".aws", ".azure"}


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
        self.include_paths=()
        self.exclude_paths=()

    @staticmethod
    def default_path() -> str:
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            root = str(Path.home() / ".sage")
        path = Path(root) / "SAGE" / "index.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=0.5)
        conn.row_factory = sqlite3.Row
        conn.create_function('casefold',1,lambda value:str(value or '').casefold(),deterministic=True)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
            if conn.execute('PRAGMA user_version').fetchone()[0] < 2:
                # Reuse chunk row IDs so replacing one file does not scan the FTS table.
                conn.execute('DELETE FROM content_fts')
                conn.execute('INSERT INTO content_fts(rowid,path,snippet) SELECT rowid,path,snippet FROM content_chunks')
                conn.execute('PRAGMA user_version=2')
        self._ready = True

    def record_fact(self, path, field, value, answer, *, source, scan_time=None):
        """Persist and read back a measurement without blocking the file-index writer."""
        # The index writes a scan in one transaction. A small SQLite sidecar keeps
        # queried analytics independent of that writer; the existing index is unchanged.
        path_db=Path(self.db_path)
        conn=sqlite3.connect(str(path_db.with_name(path_db.stem+'-facts'+path_db.suffix)),timeout=.5)
        try:
            with conn:
                conn.execute('CREATE TABLE IF NOT EXISTS facts (path TEXT COLLATE NOCASE, field TEXT, value_json TEXT, '
                             'answer TEXT, source TEXT, scan_time TEXT, measured_at INTEGER, PRIMARY KEY(path,field))')
                conn.execute('INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?)',
                             (path,field,json.dumps(value),answer,source,scan_time,int(time.time())))
                row=conn.execute('SELECT value_json,answer,source,scan_time,measured_at FROM facts WHERE path=? AND field=?',
                                 (path,field)).fetchone()
            return {'path':path,'field':field,'value':json.loads(row[0]),'answer':row[1],
                    'source':row[2],'scan_time':row[3],'measured_at':row[4]}
        finally:conn.close()

    @staticmethod
    def _safe_for_content_index(path: str, extension: str, is_reparse: bool) -> bool:
        if is_reparse or extension not in _APPROVED_TEXT_EXTENSIONS:
            return False
        parts = {part.casefold() for part in ntpath.normpath(path).split("\\") if part}
        parts.add(ntpath.splitext(ntpath.basename(path))[0].casefold())
        return not bool(parts & _SENSITIVE_SEGMENTS)

    def _replace_content(self, conn: sqlite3.Connection, path: str, text: str) -> None:
        for row in conn.execute('SELECT rowid FROM content_chunks WHERE path=?', (path,)).fetchall():
            conn.execute('DELETE FROM content_fts WHERE rowid=?', (row[0],))
        conn.execute("DELETE FROM content_chunks WHERE path = ?", (path,))
        chunk_size = 500
        for index, start in enumerate(range(0, len(text), chunk_size)):
            if index >= self.max_chunks_per_file:
                break
            snippet = text[start : start + chunk_size].strip()
            if not snippet:
                continue
            cursor = conn.execute(
                "INSERT OR REPLACE INTO content_chunks(path, chunk_index, snippet) VALUES(?, ?, ?)",
                (path, index, snippet),
            )
            conn.execute("INSERT INTO content_fts(rowid, path, snippet) VALUES(?, ?, ?)", (cursor.lastrowid, path, snippet))

    def update_scan(self, result: ScanResult, *, index_content: bool = True, cancelled=None, progress=None,
                    include_paths: tuple = (), exclude_paths: tuple = (), content_seconds: float = 2) -> None:
        """Incrementally upsert metadata and index safe local text excerpts."""
        self.ensure_schema()
        self.include_paths=include_paths
        self.exclude_paths=exclude_paths
        now = time.time_ns()
        evidence = ScanEvidence(result)
        with self._connect() as conn:
            for count, record in enumerate(result.records.values()):
                if cancelled and cancelled():
                    raise InterruptedError('Index update cancelled.')
                path = result.path_for(record.frn)
                if not path:
                    continue
                parent = ntpath.dirname(path.rstrip("\\")) if path.rstrip("\\") != result.volume.drive.rstrip("\\") else None
                extension = ntpath.splitext(record.name)[1].casefold()
                previous = conn.execute('SELECT logical_bytes,modified_time FROM files WHERE path=?', (path,)).fetchone()
                if previous and (not record.modified or previous['logical_bytes'] != record.logical_size or previous['modified_time'] != record.modified):
                    self._replace_content(conn, path, '')
                    conn.execute('UPDATE files SET content_indexed=0 WHERE path=?', (path,))
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
                        result.directory_sizes.get(record.frn, (None,None))[0] if record.is_directory else (record.logical_size if record.size_ready else None),
                        result.directory_sizes.get(record.frn, (None,None))[1] if record.is_directory else (record.allocated_size if record.size_ready else None),
                        int(record.modified or 0),
                        result.volume.drive.upper(),
                        now,
                        path,
                    ),
                )
                if progress and count % 1000 == 0:
                    progress(count, len(result.records))
            stale = conn.execute('SELECT path FROM files WHERE scan_drive=? AND indexed_at<>?', (result.volume.drive.upper(),now)).fetchall()
            for row in stale:
                if cancelled and cancelled():raise InterruptedError('Index update cancelled.')
                self._replace_content(conn,row['path'],'')
            conn.execute('DELETE FROM files WHERE scan_drive=? AND indexed_at<>?', (result.volume.drive.upper(),now))
        if not index_content:
            return
        deadline = time.monotonic() + content_seconds
        # ponytail: bounded local text excerpts, not a whole-drive content audit.
        # Further content work is requested explicitly through search_content.
        with self._connect() as conn:
            for record in result.records.values():
                if time.monotonic() >= deadline or cancelled and cancelled():
                    break
                path = result.path_for(record.frn)
                extension = ntpath.splitext(record.name)[1].casefold()
                if record.is_directory or not path or not self._safe_for_content_index(path,extension,bool(record.attributes & (0x400|0x1000|0x400000))):
                    continue
                if include_paths and not any(evidence.within(path,p) for p in include_paths):
                    continue
                if any(evidence.within(path,p) for p in exclude_paths):
                    continue
                row = conn.execute('SELECT content_indexed FROM files WHERE path=?',(path,)).fetchone()
                if row and row[0]:
                    continue
                excerpt = evidence._read_excerpt(record,limit=self.max_content_bytes)
                if excerpt.get('status') == 'text':
                    self._replace_content(conn,path,excerpt['text'])
                    conn.execute('UPDATE files SET content_indexed=1 WHERE path=?',(path,))

    def search_names(self, query: str, *, scope: str | None = None, limit: int = 10) -> list[dict]:
        self.ensure_schema()
        term = query.strip()
        if not term:
            return []
        where = "(name LIKE ? OR path LIKE ?)"
        params: list[object] = [f"%{term}%", f"%{term}%"]
        if scope:
            where += " AND instr(casefold(path), ?) = 1"
            params.append(ntpath.normpath(scope).rstrip("\\").casefold() + "\\")
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

    def content_candidates(self,scope: str,limit: int=64) -> list[str]:
        self.ensure_schema()
        extensions=sorted(_APPROVED_TEXT_EXTENSIONS)
        prefix=ntpath.normpath(scope).rstrip('\\').casefold()+'\\'
        with self._connect() as conn:
            rows=conn.execute('SELECT path FROM files WHERE is_directory=0 AND logical_bytes BETWEEN 1 AND ? '
                'AND instr(casefold(path),?)=1 AND extension IN ('+','.join('?' for _ in extensions)+') '
                'ORDER BY content_indexed,logical_bytes,path LIMIT ?',
                [self.max_content_bytes,prefix,*extensions,limit]).fetchall()
        return [row[0] for row in rows]

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
                        JOIN content_fts f ON f.rowid = c.rowid
                        WHERE f.snippet MATCH ? AND instr(casefold(c.path), ?) = 1
                        ORDER BY c.path, c.chunk_index
                        LIMIT ?
                        """,
                        (term, ntpath.normpath(scope).rstrip("\\").casefold() + "\\", max(1, min(limit, 50))),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT c.path, c.chunk_index, c.snippet
                        FROM content_chunks c
                        JOIN content_fts f ON f.rowid = c.rowid
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
                        WHERE snippet LIKE ? AND instr(casefold(path), ?) = 1
                        ORDER BY path, chunk_index
                        LIMIT ?
                        """,
                        (f"%{term}%", ntpath.normpath(scope).rstrip("\\").casefold() + "\\", max(1, min(limit, 50))),
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
            where += " AND instr(casefold(path), ?) = 1"
            params.append(ntpath.normpath(scope).rstrip("\\").casefold() + "\\")
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

    def search_metadata(self, *, scope: str, query: str = '', kind: str = 'any', extension: str = '',
                        minimum_bytes: int = 0, modified_before: int = 0, modified_after: int = 0,
                        sort: str = 'largest', limit: int = 8) -> list[dict]:
        self.ensure_schema()
        # instr avoids LIKE wildcard names and enforces folder boundaries.
        prefix = ntpath.normpath(scope).rstrip('\\').casefold() + '\\'
        where, args = ['instr(casefold(path),?)=1','casefold(path)<>?'], [prefix,ntpath.normpath(scope).casefold()]
        for clause, value in [('instr(casefold(name),?)>0',query.casefold()),
                              ('extension=?',extension.casefold())]:
            if value:
                where.append(clause); args.append(value)
        if kind in {'file','folder'}:
            where.append('is_directory=?'); args.append(int(kind=='folder'))
        if minimum_bytes:
            where.append('allocated_bytes>=?'); args.append(minimum_bytes)
        if modified_before:
            where.append('modified_time>0 AND modified_time<?'); args.append(modified_before)
        if modified_after:
            where.append('modified_time>=?'); args.append(modified_after)
        order = {'largest':'allocated_bytes DESC','recent':'modified_time DESC','oldest':'modified_time ASC','name':'name ASC'}[sort]
        if sort in {'recent','oldest'}:
            where.append('modified_time>0')
        args.append(limit)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                'SELECT path,name,is_directory,logical_bytes,allocated_bytes,modified_time FROM files WHERE '
                + ' AND '.join(where) + ' ORDER BY ' + order + ',path LIMIT ?', args)]

    def possible_duplicates(self, scope: str, limit: int = 5) -> dict:
        self.ensure_schema()
        prefix = ntpath.normpath(scope).rstrip('\\').casefold() + '\\'
        groups=[]
        with self._connect() as conn:
            rows=conn.execute('SELECT logical_bytes,COUNT(*) AS n FROM files WHERE is_directory=0 AND logical_bytes>0 '
                'AND instr(casefold(path),?)=1 GROUP BY logical_bytes HAVING COUNT(*)>1 ORDER BY logical_bytes DESC LIMIT ?', (prefix,limit)).fetchall()
            for row in rows:
                matches=conn.execute('SELECT path FROM files WHERE is_directory=0 AND logical_bytes=? AND instr(casefold(path),?)=1 LIMIT 8',
                                     (row['logical_bytes'],prefix)).fetchall()
                groups.append({'logical_bytes':row['logical_bytes'],'count':row['n'],'paths':[r[0] for r in matches]})
        return {'groups':groups,'content_verified':False,'basis':'Same measured logical size only. Contents may differ; no deletion recommendation.'}

"""Optional read-only adapter for a local CodeGraph index."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from .models import CodeReference


_OWNER_KINDS = (
    "function",
    "method",
    "class",
    "struct",
    "interface",
    "trait",
    "protocol",
    "route",
    "component",
)


@dataclass(frozen=True)
class CodeGraphSymbol:
    id: str
    kind: str
    qualified_name: str


class CodeGraphProvider:
    """Resolve source locations against CodeGraph without owning its lifecycle."""

    def __init__(self, database: Path):
        self.database = database
        self._db: sqlite3.Connection | None = None

    def __enter__(self) -> CodeGraphProvider:
        uri = f"{self.database.resolve().as_uri()}?mode=ro"
        self._db = sqlite3.connect(uri, uri=True)
        self._db.row_factory = sqlite3.Row
        return self

    def __exit__(self, *_exc_info) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def symbol_at(self, file_path: str, line: int) -> CodeGraphSymbol | None:
        """Return the narrowest symbol containing, or immediately after, a line."""
        if not self.database.is_file():
            return None

        placeholders = ",".join("?" for _ in _OWNER_KINDS)
        temporary_db: sqlite3.Connection | None = None
        try:
            if self._db is None:
                uri = f"{self.database.resolve().as_uri()}?mode=ro"
                temporary_db = sqlite3.connect(uri, uri=True)
                temporary_db.row_factory = sqlite3.Row
            db = self._db or temporary_db
            assert db is not None
            row = db.execute(
                f"""
                SELECT id, kind, qualified_name
                FROM nodes
                WHERE file_path = ?
                  AND kind IN ({placeholders})
                  AND (
                    (start_line <= ? AND end_line >= ?)
                    OR (start_line > ? AND start_line <= ?)
                  )
                ORDER BY
                  CASE WHEN start_line <= ? THEN 0 ELSE 1 END,
                  (end_line - start_line) ASC,
                  start_line ASC
                LIMIT 1
                """,
                (
                    file_path,
                    *_OWNER_KINDS,
                    line,
                    line,
                    line,
                    line + 2,
                    line,
                ),
            ).fetchone()
        except sqlite3.Error:
            if self._db is not None:
                raise
            return None
        finally:
            if temporary_db is not None:
                temporary_db.close()

        if not row:
            return None
        return CodeGraphSymbol(
            id=row["id"],
            kind=row["kind"],
            qualified_name=row["qualified_name"],
        )

    def file_is_current(self, file_path: str, source_file: Path) -> bool:
        """Return whether CodeGraph indexed the current on-disk file version."""
        if self._db is None or not source_file.is_file():
            return False
        row = self._db.execute(
            "SELECT modified_at FROM files WHERE path = ?",
            (file_path,),
        ).fetchone()
        if not row:
            return False
        return int(row["modified_at"]) == int(source_file.stat().st_mtime * 1000)


def enrich_code_references(
    root: Path,
    refs: list[CodeReference],
    database: str,
    *,
    warn: bool = True,
) -> list[CodeReference]:
    """Attach CodeGraph symbol identity where available; preserve file fallback."""
    configured_path = Path(database).expanduser()
    db_path = configured_path if configured_path.is_absolute() else root / configured_path
    if not db_path.is_file():
        if not warn:
            return refs
        print(
            f"warning: CodeGraph index not found at {db_path}; using file-level code traces",
            file=sys.stderr,
        )
        return refs

    try:
        with CodeGraphProvider(db_path) as provider:
            current_files: dict[str, bool] = {}
            stale_files: list[str] = []
            for ref in refs:
                rel_path = Path(ref.rel_path).as_posix()
                if rel_path not in current_files:
                    current_files[rel_path] = provider.file_is_current(
                        rel_path,
                        ref.code_file,
                    )
                    if not current_files[rel_path]:
                        stale_files.append(rel_path)
                if not current_files[rel_path]:
                    continue
                symbol = provider.symbol_at(rel_path, ref.line_number)
                if not symbol:
                    continue
                ref.provider_id = symbol.id
                ref.provider_kind = symbol.kind
                ref.provider_title = symbol.qualified_name
            if stale_files and warn:
                preview = ", ".join(stale_files[:3])
                suffix = "" if len(stale_files) <= 3 else f", ... (+{len(stale_files) - 3})"
                print(
                    f"warning: CodeGraph has no current index for {len(stale_files)} traced "
                    f"file(s): {preview}{suffix}; using file-level code traces",
                    file=sys.stderr,
                )
    except sqlite3.Error as exc:
        if not warn:
            return refs
        print(
            f"warning: cannot read CodeGraph index at {db_path}: {exc}; "
            "using file-level code traces",
            file=sys.stderr,
        )
    return refs

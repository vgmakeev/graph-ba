"""SQLite storage and import helpers for Graph BA."""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional, List, Tuple

DB_PATH = Path.cwd() / "reports" / "graph.db"
SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    line_number INTEGER NOT NULL DEFAULT 0,
    defined     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS edges (
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '',
    source_file TEXT NOT NULL DEFAULT '',
    line_number INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, target_id, source_file, line_number)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type);

CREATE TABLE IF NOT EXISTS semantic_clusters (
    cluster_name TEXT NOT NULL,
    artifact_id  TEXT NOT NULL,
    PRIMARY KEY (cluster_name, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_clusters_artifact ON semantic_clusters(artifact_id);

CREATE TABLE IF NOT EXISTS file_paths (
    filename    TEXT PRIMARY KEY,
    full_path   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scanned_files (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL
);

-- FTS5 virtual table for full-text search over artifacts
CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
    id, type, title, source_file,
    content=artifacts,
    content_rowid=rowid
);

-- FTS5 for edge context search
CREATE VIRTUAL TABLE IF NOT EXISTS edges_fts USING fts5(
    source_id, target_id, context,
    tokenize='unicode61'
);

-- FTS5 for semantic cluster search
CREATE VIRTUAL TABLE IF NOT EXISTS clusters_fts USING fts5(
    cluster_name, artifact_id,
    tokenize='unicode61'
);

-- Triggers to keep FTS in sync with artifacts
CREATE TRIGGER IF NOT EXISTS artifacts_ai AFTER INSERT ON artifacts BEGIN
    INSERT INTO artifacts_fts(rowid, id, type, title, source_file)
    VALUES (new.rowid, new.id, new.type, new.title, new.source_file);
END;
CREATE TRIGGER IF NOT EXISTS artifacts_ad AFTER DELETE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, id, type, title, source_file)
    VALUES ('delete', old.rowid, old.id, old.type, old.title, old.source_file);
END;
CREATE TRIGGER IF NOT EXISTS artifacts_au AFTER UPDATE ON artifacts BEGIN
    INSERT INTO artifacts_fts(artifacts_fts, rowid, id, type, title, source_file)
    VALUES ('delete', old.rowid, old.id, old.type, old.title, old.source_file);
    INSERT INTO artifacts_fts(rowid, id, type, title, source_file)
    VALUES (new.rowid, new.id, new.type, new.title, new.source_file);
END;
"""


def _drop_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TRIGGER IF EXISTS artifacts_ai;
        DROP TRIGGER IF EXISTS artifacts_ad;
        DROP TRIGGER IF EXISTS artifacts_au;
        DROP TABLE IF EXISTS artifacts_fts;
        DROP TABLE IF EXISTS edges_fts;
        DROP TABLE IF EXISTS clusters_fts;
        DROP TABLE IF EXISTS scanned_files;
        DROP TABLE IF EXISTS semantic_clusters;
        DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS artifacts;
        DROP TABLE IF EXISTS file_paths;
        DROP TABLE IF EXISTS meta;
    """)


def get_db(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    has_schema = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
    ).fetchone()
    if has_schema and version != SCHEMA_VERSION:
        print("schema changed — rebuilding graph", file=sys.stderr)
        _drop_schema(conn)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


# ── Import ────────────────────────────────────────────────────────

def _scan_file_mtimes(root: Path, config) -> dict[str, float]:
    files: dict[str, float] = {}
    for d in config.scan_dirs:
        p = root / d
        if not p.exists():
            continue
        for f in p.rglob("*.md"):
            if f.is_file():
                files[str(f.resolve())] = f.stat().st_mtime
    return files


def _stored_file_mtimes(db: sqlite3.Connection) -> Optional[dict[str, float]]:
    try:
        rows = db.execute("SELECT path, mtime FROM scanned_files").fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    return {r["path"]: float(r["mtime"]) for r in rows}


def graph_is_stale(db: sqlite3.Connection, root: Path, config) -> bool:
    """Compare current source mtimes with the last import snapshot."""
    current = _scan_file_mtimes(root.resolve(), config)
    stored = _stored_file_mtimes(db)
    return stored != current


def do_import(root: Path, db: sqlite3.Connection, quiet: bool = False,
              force: bool = False) -> bool:
    """Import graph by running traceability scan and loading into SQLite."""
    from graph_ba import traceability as t
    from graph_ba.config import load_config

    root = root.resolve()
    config = load_config(root)
    current_files = _scan_file_mtimes(root, config)
    has_data = db.execute("SELECT count(*) FROM artifacts").fetchone()[0] > 0
    if not force and has_data and _stored_file_mtimes(db) == current_files:
        if not quiet:
            n_nodes = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
            n_edges = db.execute("SELECT count(*) FROM edges").fetchone()[0]
            n_clusters = db.execute(
                "SELECT count(DISTINCT cluster_name) FROM semantic_clusters"
            ).fetchone()[0]
            n_code = db.execute(
                "SELECT count(DISTINCT source_id) FROM edges WHERE source_id LIKE 'CODE:%'"
            ).fetchone()[0]
            n_test = db.execute(
                "SELECT count(DISTINCT source_id) FROM edges WHERE source_id LIKE 'TEST:%'"
            ).fetchone()[0]
            n_ui = db.execute(
                "SELECT count(DISTINCT source_id) FROM edges WHERE source_id LIKE 'UI:%'"
            ).fetchone()[0]
            print(f"Imported: {n_nodes} artifacts, {n_edges} edges, "
                  f"{n_clusters} semantic clusters, {n_code} code files, "
                  f"{n_test} test files, {n_ui} ui trace files "
                  "(up to date, no changes)")
            db_path = db.execute("PRAGMA database_list").fetchone()[2]
            print(f"DB: {db_path}")
        return False

    registry = t.scan_definitions(root, config)
    references = t.scan_references(root, registry, config)
    index_xrefs = t.scan_index_cross_refs(root, config)
    code_refs = t.scan_code_references(root, config)
    test_refs = t.scan_test_references(root, config)
    ui_refs = t.scan_ui_references(root, config)
    G = t.build_graph(registry, references, config, index_xrefs, code_refs, test_refs,
                      ui_refs)

    # Clear existing data
    db.executescript("""
        DELETE FROM edges_fts;
        DELETE FROM clusters_fts;
        DELETE FROM semantic_clusters;
        DELETE FROM edges;
        DELETE FROM artifacts;
        DELETE FROM file_paths;
        DELETE FROM scanned_files;
    """)

    # Insert artifacts
    for n, d in G.nodes(data=True):
        art = registry.get(n)
        db.execute(
            "INSERT OR REPLACE INTO artifacts (id, type, title, source_file, line_number, defined) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (n, d.get("type", "UNKNOWN"), d.get("title", ""),
             d.get("source_file", ""), art.line_number if art else 0,
             1 if d.get("defined", False) else 0)
        )

    # Insert edges
    for u, v, d in G.edges(data=True):
        db.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, context, source_file, line_number) "
            "VALUES (?, ?, ?, ?, ?)",
            (u, v, d.get("context", ""), d.get("source_file", ""), d.get("line", 0))
        )

    # Insert semantic clusters from config
    for cluster_name, ids in config.clusters.items():
        for aid in ids:
            db.execute(
                "INSERT OR IGNORE INTO semantic_clusters (cluster_name, artifact_id) "
                "VALUES (?, ?)", (cluster_name, aid)
            )

    # Build filename → full_path mapping
    file_map: dict = {}
    for art in registry.values():
        file_map[art.source_file.name] = str(art.source_file)
    for ref in references:
        file_map[ref.source_file.name] = str(ref.source_file)
    for cref in code_refs:
        file_map[cref.code_file.name] = str(cref.code_file)
    for tref in test_refs:
        file_map[tref.code_file.name] = str(tref.code_file)
    for uref in ui_refs:
        file_map[uref.code_file.name] = str(uref.code_file)
    for fname, fpath in file_map.items():
        db.execute("INSERT OR IGNORE INTO file_paths (filename, full_path) VALUES (?, ?)",
                   (fname, fpath))

    # Populate FTS for edges and clusters
    db.execute("INSERT INTO edges_fts(source_id, target_id, context) "
               "SELECT source_id, target_id, context FROM edges")
    db.execute("INSERT INTO clusters_fts(cluster_name, artifact_id) "
               "SELECT cluster_name, artifact_id FROM semantic_clusters")

    for fpath, mtime in current_files.items():
        db.execute("INSERT OR REPLACE INTO scanned_files (path, mtime) VALUES (?, ?)",
                   (fpath, mtime))

    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('import_time', ?)",
               (str(time.time()),))

    db.commit()

    if quiet:
        return

    n_nodes = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    n_edges = db.execute("SELECT count(*) FROM edges").fetchone()[0]
    n_clusters = db.execute("SELECT count(DISTINCT cluster_name) FROM semantic_clusters").fetchone()[0]
    n_code = db.execute(
        "SELECT count(DISTINCT source_id) FROM edges WHERE source_id LIKE 'CODE:%'"
    ).fetchone()[0]
    n_test = db.execute(
        "SELECT count(DISTINCT source_id) FROM edges WHERE source_id LIKE 'TEST:%'"
    ).fetchone()[0]
    n_ui = db.execute(
        "SELECT count(DISTINCT source_id) FROM edges WHERE source_id LIKE 'UI:%'"
    ).fetchone()[0]
    db_path = db.execute("PRAGMA database_list").fetchone()[2]
    print(f"Imported: {n_nodes} artifacts, {n_edges} edges, "
          f"{n_clusters} semantic clusters, {n_code} code files, "
          f"{n_test} test files, {n_ui} ui trace files")
    print(f"DB: {db_path}")
    return True


# ── Query helpers ─────────────────────────────────────────────────
def _fts_query(q: str) -> str:
    """Auto-add wildcard suffix to each token for prefix matching.
    'order delivery' -> 'order* delivery*'
    Passes through if user already uses FTS5 syntax (*, OR, AND, quotes).
    """
    if any(c in q for c in ('*', '"', 'OR', 'AND', 'NOT', 'NEAR')):
        return q
    tokens = q.strip().split()
    result = []
    for t in tokens:
        if not t:
            continue
        result.append(t + "*")
    return " ".join(result)


def _load_nx(db: sqlite3.Connection):
    import networkx as nx
    G = nx.DiGraph()
    for r in db.execute("SELECT * FROM artifacts").fetchall():
        G.add_node(r["id"], type=r["type"], title=r["title"],
                   source_file=r["source_file"], defined=bool(r["defined"]))
    for r in db.execute("SELECT * FROM edges").fetchall():
        G.add_edge(r["source_id"], r["target_id"],
                   context=r["context"], source_file=r["source_file"])
    return G



# ── Review command (validate + context combined) ─────────────────
def _resolve_file(db: sqlite3.Connection, filename: str) -> Optional[str]:
    """Resolve a filename to its full path using the file_paths table."""
    row = db.execute("SELECT full_path FROM file_paths WHERE filename = ?",
                     (filename,)).fetchone()
    if row:
        return row["full_path"]
    # Fallback: try partial match
    row = db.execute("SELECT full_path FROM file_paths WHERE filename LIKE ?",
                     (f"%{filename}%",)).fetchone()
    return row["full_path"] if row else None


def _read_snippet(filepath: str, center_line: int, radius: int = 4) -> Optional[str]:
    """Read a snippet of a file around a given line number."""
    p = Path(filepath)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    start = max(0, center_line - radius - 1)
    end = min(len(lines), center_line + radius)
    result = []
    for i in range(start, end):
        marker = "→" if i == center_line - 1 else " "
        result.append(f"  {marker} {i+1:4d}│ {lines[i]}")
    return "\n".join(result)


# ── Numeric extraction for validation ────────────────────────────
class _FileCache:
    """Lazy file-content cache to avoid re-reading files across checks."""
    def __init__(self):
        self._cache: dict = {}

    def get_lines(self, path: str) -> Optional[List[str]]:
        if path not in self._cache:
            try:
                self._cache[path] = Path(path).read_text(encoding="utf-8").splitlines()
            except Exception:
                self._cache[path] = None
        return self._cache[path]

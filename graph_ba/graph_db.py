"""
Graph BA — SQLite + FTS5 store for the artifact traceability graph.

Imports the graph from traceability scan, stores in SQLite with full-text
search, and provides CLI commands for agent-friendly querying.

Usage:
    graph-ba import              # scan & populate DB
    graph-ba search "кухня"      # FTS5 search
    graph-ba node BP-03           # node details + neighbors
    graph-ba neighbors BP-03      # in/out edges
    graph-ba path F-04 M09        # shortest path
    graph-ba impact BR.19         # cascade analysis
    graph-ba hubs                 # most connected nodes
    graph-ba orphans              # weakly connected nodes
    graph-ba stats                # summary
    graph-ba cluster "кухня"      # semantic cluster lookup
    graph-ba sql "SELECT ..."     # raw SQL
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import click

DB_PATH = Path.cwd() / "reports" / "graph.db"

# ── Schema ────────────────────────────────────────────────────────

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


def get_db(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ── Import ────────────────────────────────────────────────────────

def do_import(root: Path, db: sqlite3.Connection):
    """Import graph by running traceability scan and loading into SQLite."""
    # Import traceability module functions
    from graph_ba import traceability as t

    root = root.resolve()
    registry = t.scan_definitions(root)
    references = t.scan_references(root, registry)
    index_xrefs = t.scan_index_cross_refs(root)
    G = t.build_graph(registry, references, index_xrefs)

    # Clear existing data
    db.executescript("""
        DELETE FROM edges_fts;
        DELETE FROM clusters_fts;
        DELETE FROM semantic_clusters;
        DELETE FROM edges;
        DELETE FROM artifacts;
        DELETE FROM file_paths;
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

    # Insert semantic clusters
    for cluster_name, ids in t.SEMANTIC_CLUSTERS.items():
        for aid in ids:
            db.execute(
                "INSERT OR IGNORE INTO semantic_clusters (cluster_name, artifact_id) "
                "VALUES (?, ?)", (cluster_name, aid)
            )

    # Build filename → full_path mapping from registry and references
    file_map: Dict[str, str] = {}
    for art in registry.values():
        file_map[art.source_file.name] = str(art.source_file)
    for ref in references:
        file_map[ref.source_file.name] = str(ref.source_file)
    for fname, fpath in file_map.items():
        db.execute("INSERT OR IGNORE INTO file_paths (filename, full_path) VALUES (?, ?)",
                   (fname, fpath))

    # Populate FTS for edges and clusters
    db.execute("INSERT INTO edges_fts(source_id, target_id, context) "
               "SELECT source_id, target_id, context FROM edges")
    db.execute("INSERT INTO clusters_fts(cluster_name, artifact_id) "
               "SELECT cluster_name, artifact_id FROM semantic_clusters")

    db.commit()

    n_nodes = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    n_edges = db.execute("SELECT count(*) FROM edges").fetchone()[0]
    n_clusters = db.execute("SELECT count(DISTINCT cluster_name) FROM semantic_clusters").fetchone()[0]
    print(f"Imported: {n_nodes} artifacts, {n_edges} edges, {n_clusters} semantic clusters")
    print(f"DB: {DB_PATH}")


# ── Query helpers ─────────────────────────────────────────────────

def _fts_query(q: str) -> str:
    """Auto-add wildcard suffix to each token for prefix matching.
    'кухня доставка' -> 'кухн* доставк*'
    Strips 1-2 trailing Cyrillic chars for crude stemming, then adds *.
    Passes through if user already uses FTS5 syntax (*, OR, AND, quotes).
    """
    if any(c in q for c in ('*', '"', 'OR', 'AND', 'NOT', 'NEAR')):
        return q
    tokens = q.strip().split()
    result = []
    for t in tokens:
        if not t:
            continue
        # Crude Russian stemming: strip 1-2 trailing Cyrillic chars if word is long enough
        if len(t) >= 4 and t[-1].lower() in "аеёиоуыэюяьъйнмтсвк":
            stem = t[:-1]
            if len(stem) >= 4 and stem[-1].lower() in "аеёиоуыэюяьъйнмтсвк":
                stem = stem[:-1]
            result.append(stem + "*")
        else:
            result.append(t + "*")
    return " ".join(result)


def fmt_table(rows: list, headers: list) -> str:
    """Format rows as a compact aligned table."""
    if not rows:
        return "(пусто)"
    widths = [len(h) for h in headers]
    str_rows = []
    for r in rows:
        sr = [str(c) if c is not None else "" for c in r]
        str_rows.append(sr)
        for i, c in enumerate(sr):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
    sep = "  "
    lines = [sep.join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append(sep.join("─" * w for w in widths))
    for sr in str_rows:
        lines.append(sep.join(sr[i].ljust(widths[i]) if i < len(widths) else "" for i in range(len(headers))))
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────

@click.group()
@click.option("--db", type=click.Path(path_type=Path), default=None,
              help=f"Path to SQLite DB (default: {DB_PATH})")
@click.pass_context
def cli(ctx, db):
    """Graph BA — query the artifact traceability graph."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


def _conn(ctx) -> sqlite3.Connection:
    return get_db(ctx.obj.get("db_path"))


@cli.command("import")
@click.option("--root", type=click.Path(exists=True, path_type=Path),
              default=".", help="Project root")
@click.pass_context
def cmd_import(ctx, root):
    """Scan artifacts and populate the SQLite DB."""
    db = _conn(ctx)
    do_import(root, db)
    db.close()


@cli.command()
@click.argument("query")
@click.option("-n", "--limit", default=20, help="Max results")
@click.pass_context
def search(ctx, query, limit):
    """Full-text search across artifact titles and IDs."""
    db = _conn(ctx)
    fq = _fts_query(query)

    # Search artifacts
    rows = db.execute(
        "SELECT a.id, a.type, a.title, a.source_file "
        "FROM artifacts_fts f JOIN artifacts a ON f.rowid = a.rowid "
        "WHERE artifacts_fts MATCH ? ORDER BY rank LIMIT ?",
        (fq, limit)
    ).fetchall()
    if rows:
        print(f"── Артефакты ({len(rows)}) ──")
        print(fmt_table(
            [(r["id"], r["type"], r["title"][:60], r["source_file"]) for r in rows],
            ["ID", "Тип", "Название", "Файл"]
        ))
    else:
        print("Артефакты: не найдено")

    # Search clusters
    cl_rows = db.execute(
        "SELECT DISTINCT cluster_name FROM clusters_fts "
        "WHERE clusters_fts MATCH ? LIMIT ?",
        (fq, limit)
    ).fetchall()
    if cl_rows:
        print(f"\n── Кластеры ({len(cl_rows)}) ──")
        for r in cl_rows:
            print(f"  • {r['cluster_name']}")

    # Search edge contexts
    e_rows = db.execute(
        "SELECT source_id, target_id, context FROM edges_fts "
        "WHERE edges_fts MATCH ? LIMIT ?",
        (fq, limit)
    ).fetchall()
    if e_rows:
        print(f"\n── Связи ({len(e_rows)}) ──")
        print(fmt_table(
            [(r["source_id"], r["target_id"], r["context"][:60]) for r in e_rows],
            ["Из", "В", "Контекст"]
        ))
    db.close()


@cli.command()
@click.argument("node_id")
@click.pass_context
def node(ctx, node_id):
    """Show node details and immediate neighbors."""
    db = _conn(ctx)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (node_id,)).fetchone()
    if not row:
        # Try case-insensitive / partial match
        rows = db.execute(
            "SELECT * FROM artifacts WHERE id LIKE ? LIMIT 5",
            (f"%{node_id}%",)
        ).fetchall()
        if rows:
            print(f"Не найден '{node_id}'. Похожие:")
            for r in rows:
                print(f"  {r['id']} ({r['type']}) — {r['title'][:50]}")
        else:
            print(f"Артефакт '{node_id}' не найден")
        db.close()
        return

    print(f"ID:     {row['id']}")
    print(f"Тип:    {row['type']}")
    print(f"Файл:   {row['source_file']}:{row['line_number']}")
    print(f"Defined: {'да' if row['defined'] else 'НЕТ (dangling)'}")
    print(f"Название: {row['title']}")

    # Clusters
    clusters = db.execute(
        "SELECT cluster_name FROM semantic_clusters WHERE artifact_id = ?",
        (node_id,)
    ).fetchall()
    if clusters:
        print(f"Кластеры: {', '.join(r['cluster_name'] for r in clusters)}")

    # Out-edges
    out = db.execute(
        "SELECT e.target_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? ORDER BY a.type, e.target_id",
        (node_id,)
    ).fetchall()
    print(f"\n→ Исходящие ({len(out)}):")
    if out:
        print(fmt_table(
            [(r["target_id"], r["type"] or "?",
              f"{r['source_file']}:{r['line_number']}" if r["line_number"] else "",
              r["title"][:40] if r["title"] else "") for r in out],
            ["ID", "Тип", "Где ссылка", "Название"]
        ))

    # In-edges
    inc = db.execute(
        "SELECT e.source_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? ORDER BY a.type, e.source_id",
        (node_id,)
    ).fetchall()
    print(f"\n← Входящие ({len(inc)}):")
    if inc:
        print(fmt_table(
            [(r["source_id"], r["type"] or "?",
              f"{r['source_file']}:{r['line_number']}" if r["line_number"] else "",
              r["title"][:40] if r["title"] else "") for r in inc],
            ["ID", "Тип", "Где ссылка", "Название"]
        ))
    db.close()


@cli.command()
@click.argument("node_id")
@click.option("--type", "filter_type", default=None, help="Filter by artifact type")
@click.option("--direction", type=click.Choice(["in", "out", "both"]), default="both")
@click.pass_context
def neighbors(ctx, node_id, filter_type, direction):
    """List neighbors of a node, optionally filtered by type and direction."""
    db = _conn(ctx)
    results = []
    if direction in ("out", "both"):
        q = ("SELECT 'out' as dir, e.target_id as neighbor, a.type, a.title "
             "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
             "WHERE e.source_id = ?")
        params: list = [node_id]
        if filter_type:
            q += " AND a.type = ?"
            params.append(filter_type)
        results.extend(db.execute(q, params).fetchall())

    if direction in ("in", "both"):
        q = ("SELECT 'in' as dir, e.source_id as neighbor, a.type, a.title "
             "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
             "WHERE e.target_id = ?")
        params = [node_id]
        if filter_type:
            q += " AND a.type = ?"
            params.append(filter_type)
        results.extend(db.execute(q, params).fetchall())

    print(f"Соседи {node_id} ({len(results)}):")
    if results:
        print(fmt_table(
            [(r["dir"], r["neighbor"], r["type"] or "?", r["title"][:50] if r["title"] else "") for r in results],
            ["Напр", "ID", "Тип", "Название"]
        ))
    db.close()


@cli.command()
@click.argument("from_id")
@click.argument("to_id")
@click.pass_context
def path(ctx, from_id, to_id):
    """Find shortest path between two artifacts."""
    import networkx as nx

    db = _conn(ctx)
    G = _load_nx(db)
    db.close()

    if from_id not in G:
        print(f"Узел '{from_id}' не найден")
        return
    if to_id not in G:
        print(f"Узел '{to_id}' не найден")
        return

    # Try directed first, then undirected
    for label, graph in [("направленный", G), ("ненаправленный", G.to_undirected())]:
        try:
            p = nx.shortest_path(graph, from_id, to_id)
            print(f"Кратчайший путь ({label}, {len(p)-1} шагов):")
            for i, nid in enumerate(p):
                data = G.nodes.get(nid, {})
                arrow = "  →  " if i < len(p) - 1 else ""
                print(f"  [{data.get('type','?')}] {nid} — {data.get('title','')[:50]}{arrow}")
            return
        except nx.NetworkXNoPath:
            continue

    print(f"Путь между {from_id} и {to_id} не существует")


@cli.command()
@click.argument("node_id")
@click.option("--depth", default=10, help="Max traversal depth")
@click.pass_context
def impact(ctx, node_id, depth):
    """Cascade impact analysis: what does changing this artifact affect?"""
    import networkx as nx

    db = _conn(ctx)
    G = _load_nx(db)
    db.close()

    if node_id not in G:
        print(f"Узел '{node_id}' не найден")
        return

    # BFS from node, follow outgoing edges
    reachable = nx.descendants(G, node_id)
    if not reachable:
        print(f"{node_id}: нет каскадного влияния (нет исходящих путей)")
        return

    # Group by type
    by_type: dict = {}
    for nid in reachable:
        t = G.nodes[nid].get("type", "?")
        by_type.setdefault(t, []).append(nid)

    print(f"Каскадное влияние {node_id}: {len(reachable)} артефактов")
    print()
    for t in sorted(by_type):
        ids = sorted(by_type[t])
        print(f"  [{t}] ({len(ids)}): {', '.join(ids[:15])}")
        if len(ids) > 15:
            print(f"         ... и ещё {len(ids)-15}")

    # Also show reverse: what affects this node?
    ancestors = nx.ancestors(G, node_id)
    if ancestors:
        print(f"\nОбратное влияние (что затрагивает {node_id}): {len(ancestors)} артефактов")
        by_type2: dict = {}
        for nid in ancestors:
            t = G.nodes[nid].get("type", "?")
            by_type2.setdefault(t, []).append(nid)
        for t in sorted(by_type2):
            ids = sorted(by_type2[t])
            print(f"  [{t}] ({len(ids)}): {', '.join(ids[:15])}")
            if len(ids) > 15:
                print(f"         ... и ещё {len(ids)-15}")


@cli.command()
@click.option("-n", "--limit", default=20, help="Max results")
@click.option("--type", "filter_type", default=None, help="Filter by type")
@click.pass_context
def hubs(ctx, limit, filter_type):
    """Show most connected nodes (hubs)."""
    db = _conn(ctx)
    q = """
        SELECT a.id, a.type, a.title,
            (SELECT count(*) FROM edges WHERE source_id = a.id) as out_deg,
            (SELECT count(*) FROM edges WHERE target_id = a.id) as in_deg,
            (SELECT count(*) FROM edges WHERE source_id = a.id)
            + (SELECT count(*) FROM edges WHERE target_id = a.id) as total_deg
        FROM artifacts a
    """
    params: list = []
    if filter_type:
        q += " WHERE a.type = ?"
        params.append(filter_type)
    q += " ORDER BY total_deg DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(q, params).fetchall()
    print(f"Хабы (top-{limit}):")
    print(fmt_table(
        [(r["id"], r["type"], r["in_deg"], r["out_deg"], r["total_deg"],
          r["title"][:40]) for r in rows],
        ["ID", "Тип", "In", "Out", "Total", "Название"]
    ))
    db.close()


@cli.command()
@click.option("--max-degree", default=1, help="Max total degree to consider orphan")
@click.option("--type", "filter_type", default=None, help="Filter by type")
@click.pass_context
def orphans(ctx, max_degree, filter_type):
    """Show poorly connected (orphan) artifacts."""
    db = _conn(ctx)
    q = """
        SELECT a.id, a.type, a.title,
            (SELECT count(*) FROM edges WHERE source_id = a.id)
            + (SELECT count(*) FROM edges WHERE target_id = a.id) as total_deg
        FROM artifacts a
        WHERE (SELECT count(*) FROM edges WHERE source_id = a.id)
            + (SELECT count(*) FROM edges WHERE target_id = a.id) <= ?
    """
    params: list = [max_degree]
    if filter_type:
        q += " AND a.type = ?"
        params.append(filter_type)
    q += " ORDER BY a.type, a.id"

    rows = db.execute(q, params).fetchall()
    if not rows:
        print(f"Нет артефактов с degree ≤ {max_degree}")
        db.close()
        return

    # Group by type
    current_type = None
    for r in rows:
        if r["type"] != current_type:
            current_type = r["type"]
            count = sum(1 for x in rows if x["type"] == current_type)
            print(f"\n[{current_type}] ({count}):")
        print(f"  {r['id']:12s} deg={r['total_deg']}  {r['title'][:55]}")
    print(f"\nИтого: {len(rows)} артефактов с degree ≤ {max_degree}")
    db.close()


@cli.command()
@click.pass_context
def stats(ctx):
    """Show summary statistics."""
    db = _conn(ctx)
    n_arts = db.execute("SELECT count(*) as c FROM artifacts").fetchone()["c"]
    n_edges = db.execute("SELECT count(*) as c FROM edges").fetchone()["c"]
    n_defined = db.execute("SELECT count(*) as c FROM artifacts WHERE defined = 1").fetchone()["c"]
    n_dangling = n_arts - n_defined

    print(f"Артефактов: {n_arts} ({n_defined} defined, {n_dangling} dangling)")
    print(f"Связей:     {n_edges}")

    # By type
    rows = db.execute("""
        SELECT type, count(*) as cnt,
            sum(CASE WHEN defined = 1 THEN 1 ELSE 0 END) as def_cnt
        FROM artifacts GROUP BY type ORDER BY cnt DESC
    """).fetchall()
    print("\nПо типам:")
    print(fmt_table(
        [(r["type"], r["cnt"], r["def_cnt"]) for r in rows],
        ["Тип", "Всего", "Defined"]
    ))

    # Edge density by type pair
    print("\nТоп связей (тип → тип):")
    rows = db.execute("""
        SELECT a1.type as src_type, a2.type as tgt_type, count(*) as cnt
        FROM edges e
        JOIN artifacts a1 ON e.source_id = a1.id
        JOIN artifacts a2 ON e.target_id = a2.id
        GROUP BY a1.type, a2.type
        ORDER BY cnt DESC LIMIT 15
    """).fetchall()
    print(fmt_table(
        [(r["src_type"], r["tgt_type"], r["cnt"]) for r in rows],
        ["Из типа", "В тип", "Кол-во"]
    ))

    # Clusters
    n_cl = db.execute("SELECT count(DISTINCT cluster_name) as c FROM semantic_clusters").fetchone()["c"]
    print(f"\nСемантических кластеров: {n_cl}")
    db.close()


@cli.command()
@click.argument("term")
@click.pass_context
def cluster(ctx, term):
    """Show a semantic cluster by name (partial match)."""
    db = _conn(ctx)
    rows = db.execute(
        "SELECT DISTINCT cluster_name FROM semantic_clusters "
        "WHERE cluster_name LIKE ? ORDER BY cluster_name",
        (f"%{term}%",)
    ).fetchall()

    if not rows:
        print(f"Кластер '{term}' не найден")
        db.close()
        return

    for cl in rows:
        name = cl["cluster_name"]
        members = db.execute(
            "SELECT sc.artifact_id, a.type, a.title "
            "FROM semantic_clusters sc "
            "LEFT JOIN artifacts a ON sc.artifact_id = a.id "
            "WHERE sc.cluster_name = ? ORDER BY a.type, sc.artifact_id",
            (name,)
        ).fetchall()
        print(f"── {name} ({len(members)}) ──")
        print(fmt_table(
            [(r["artifact_id"], r["type"] or "?", r["title"][:55] if r["title"] else "—") for r in members],
            ["ID", "Тип", "Название"]
        ))
        print()
    db.close()


@cli.command("sql")
@click.argument("query")
@click.pass_context
def raw_sql(ctx, query):
    """Execute raw SQL query."""
    db = _conn(ctx)
    try:
        rows = db.execute(query).fetchall()
        if not rows:
            print("(пусто)")
            db.close()
            return
        headers = rows[0].keys()
        print(fmt_table(
            [tuple(r) for r in rows],
            list(headers)
        ))
    except sqlite3.Error as e:
        print(f"SQL error: {e}", file=sys.stderr)
    db.close()


@cli.command()
@click.argument("from_type")
@click.argument("to_type")
@click.pass_context
def gaps(ctx, from_type, to_type):
    """Find artifacts of FROM_TYPE that have no edges to artifacts of TO_TYPE."""
    db = _conn(ctx)
    rows = db.execute("""
        SELECT a.id, a.title
        FROM artifacts a
        WHERE a.type = ?
          AND a.defined = 1
          AND NOT EXISTS (
              SELECT 1 FROM edges e
              JOIN artifacts a2 ON e.target_id = a2.id
              WHERE e.source_id = a.id AND a2.type = ?
          )
          AND NOT EXISTS (
              SELECT 1 FROM edges e
              JOIN artifacts a2 ON e.source_id = a2.id
              WHERE e.target_id = a.id AND a2.type = ?
          )
        ORDER BY a.id
    """, (from_type, to_type, to_type)).fetchall()

    if not rows:
        print(f"Все {from_type} связаны с {to_type}")
    else:
        print(f"{from_type} без связей с {to_type} ({len(rows)}):")
        for r in rows:
            print(f"  {r['id']:12s} {r['title'][:60]}")
    db.close()


@cli.command()
@click.pass_context
def coverage(ctx):
    """Show cross-layer coverage matrix."""
    db = _conn(ctx)
    # Expected cross-layer links
    pairs = [
        ("F", "M"), ("F", "BF"), ("F", "BR_REQ"),
        ("BF", "BR_REQ"), ("BR_RULE", "BR_REQ"), ("BR_RULE", "BP"),
        ("BP", "F"), ("BP", "VAD"), ("BP", "BD"),
        ("ST", "BR_REQ"),
    ]
    print("Матрица покрытия межслойных связей:")
    print()
    for src_type, tgt_type in pairs:
        total = db.execute(
            "SELECT count(*) as c FROM artifacts WHERE type = ? AND defined = 1",
            (src_type,)
        ).fetchone()["c"]
        linked = db.execute("""
            SELECT count(DISTINCT a.id) as c
            FROM artifacts a
            WHERE a.type = ? AND a.defined = 1
              AND (EXISTS (
                  SELECT 1 FROM edges e JOIN artifacts a2 ON e.target_id = a2.id
                  WHERE e.source_id = a.id AND a2.type = ?
              ) OR EXISTS (
                  SELECT 1 FROM edges e JOIN artifacts a2 ON e.source_id = a2.id
                  WHERE e.target_id = a.id AND a2.type = ?
              ))
        """, (src_type, tgt_type, tgt_type)).fetchone()["c"]

        pct = (linked / total * 100) if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        status = "OK" if pct >= 90 else "WARN" if pct >= 50 else "GAP"
        print(f"  {src_type:8s} ↔ {tgt_type:8s}  {linked:3d}/{total:<3d}  {bar}  {pct:5.1f}%  [{status}]")
    db.close()


# ── NetworkX loader (for path/impact commands) ────────────────────

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


@cli.command()
@click.option("-o", "--output", type=click.Path(path_type=Path),
              default=None, help="Output HTML path (default: reports/traceability.html)")
@click.option("--no-file-nodes", is_flag=True, help="Exclude FILE nodes")
@click.option("--no-transitive", is_flag=True, help="Remove transitive edges")
@click.pass_context
def render(ctx, output, no_file_nodes, no_transitive):
    """Render interactive HTML visualization from the DB."""
    from graph_ba import traceability as t

    db = _conn(ctx)
    G = _load_nx(db)
    db.close()

    H = t._filter_graph(G, no_file_nodes, no_transitive, verbose=True)
    out = output or (Path.cwd() / "reports" / "traceability.html")
    t.export_html(H, out)


@cli.command()
@click.argument("start_id")
@click.option("--depth", default=3, help="Max traversal depth")
@click.option("--direction", type=click.Choice(["out", "in", "both"]), default="out",
              help="Traverse outgoing, incoming, or both edges")
@click.option("--type", "filter_type", default=None, help="Only show nodes of this type")
@click.option("--no-file", is_flag=True, help="Skip FILE nodes")
@click.pass_context
def walk(ctx, start_id, depth, direction, filter_type, no_file):
    """BFS walk from a node — shows the reachability tree."""
    db = _conn(ctx)
    G = _load_nx(db)
    db.close()

    if start_id not in G:
        print(f"Узел '{start_id}' не найден")
        return

    # BFS with depth tracking
    visited = {start_id: 0}
    queue = [(start_id, 0)]
    tree: dict = {}  # node -> (parent, depth)
    tree[start_id] = (None, 0)

    while queue:
        node, d = queue.pop(0)
        if d >= depth:
            continue

        if direction in ("out", "both"):
            for succ in G.successors(node):
                if succ not in visited:
                    if no_file and G.nodes[succ].get("type") == "FILE":
                        continue
                    if filter_type and G.nodes[succ].get("type") != filter_type:
                        continue
                    visited[succ] = d + 1
                    tree[succ] = (node, d + 1)
                    queue.append((succ, d + 1))

        if direction in ("in", "both"):
            for pred in G.predecessors(node):
                if pred not in visited:
                    if no_file and G.nodes[pred].get("type") == "FILE":
                        continue
                    if filter_type and G.nodes[pred].get("type") != filter_type:
                        continue
                    visited[pred] = d + 1
                    tree[pred] = (node, d + 1)
                    queue.append((pred, d + 1))

    # Print as indented tree
    start_data = G.nodes[start_id]
    print(f"[{start_data.get('type','?')}] {start_id} — {start_data.get('title','')[:55]}")

    # Group by depth, then sort
    by_depth: dict = {}
    for nid, (parent, d) in tree.items():
        if nid == start_id:
            continue
        by_depth.setdefault(d, []).append((nid, parent))

    for d in sorted(by_depth):
        for nid, parent in sorted(by_depth[d]):
            data = G.nodes.get(nid, {})
            indent = "  " * d
            typ = data.get("type", "?")
            title = data.get("title", "")[:50]
            print(f"{indent}├─ [{typ}] {nid} — {title}")

    total = len(visited) - 1
    if total:
        print(f"\nОбход: {total} узлов за {max(visited.values())} шагов")
    else:
        print("\nНет достижимых узлов")


# ── Review command (validate + context combined) ─────────────────

@cli.command()
@click.argument("node_id_or_file")
@click.option("--lines", default=200, type=int, help="Max lines per artifact in --semantic mode (default 200)")
@click.option("--nums", is_flag=True, help="Enable numeric conflict detection")
@click.option("--semantic", is_flag=True, help="Full text of each linked artifact for semantic validation")
@click.option("--types", default=None, help="Comma-separated artifact types to include in --semantic (e.g. ST,BR_REQ,BR_RULE,BP)")
@click.pass_context
def review(ctx, node_id_or_file, lines, nums, semantic, types):
    """Full review: validate + context in one call.

    Accepts artifact ID (e.g. F-01) or file path (e.g. PRD/F-01_....md).
    Use --semantic to get full text of all linked artifacts for deep validation.
    Use --lines N to limit each artifact to N lines in semantic mode.
    """
    db = _conn(ctx)

    # Resolve: accept ID or file path
    explicit_file = None  # file path passed by user (show as main document)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (node_id_or_file,)).fetchone()
    if not row:
        explicit_file = node_id_or_file
        basename = Path(node_id_or_file).name

        # When a file path is given, prefer the FILE: node
        # (it has all outgoing refs from the document, not just the feature-list entry)
        file_id = f"FILE:{basename}"
        row = db.execute("SELECT * FROM artifacts WHERE id = ?",
                         (file_id,)).fetchone()

        # Fallback: extract artifact ID from filename (e.g. F-01_xxx.md → F-01)
        if not row:
            fname_stem = Path(node_id_or_file).stem
            id_match = re.match(r'^([A-ZА-Я]{1,3}-?\d{1,2}(?:\.\d+)*)', fname_stem)
            if id_match:
                candidate = id_match.group(1)
                row = db.execute("SELECT * FROM artifacts WHERE id = ?",
                                 (candidate,)).fetchone()

        # Fallback: find non-FILE artifact referencing this file
        if not row:
            row = db.execute(
                "SELECT * FROM artifacts WHERE type != 'FILE' "
                "AND (source_file = ? OR source_file LIKE ?)",
                (node_id_or_file, f"%{basename}")
            ).fetchone()
    if not row:
        print(f"Артефакт '{node_id_or_file}' не найден (ни как ID, ни как файл)")
        db.close()
        return
    node_id = row["id"]

    # Resolve explicit_file to full path (user may pass relative path)
    if explicit_file:
        p = Path(explicit_file)
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.exists():
            explicit_file = str(p)
        else:
            # Try via file_paths table
            explicit_file = _resolve_file(db, Path(explicit_file).name) or None

    # ── Part 1: Validate ──
    print(f"{'═' * 70}")
    print(f"  REVIEW: {node_id} — {row['title']}")
    print(f"  Тип: {row['type']}  |  Файл: {row['source_file']}:{row['line_number']}")
    print(f"{'═' * 70}")

    issues: List[Tuple[str, str, str]] = []
    fname = row["source_file"]
    full_path = _resolve_file(db, fname)

    if full_path and Path(full_path).exists():
        try:
            content = Path(full_path).read_text(encoding="utf-8")
        except Exception:
            content = ""

        atype = row["type"]
        if atype in _REQUIRED_SECTIONS:
            for section in _REQUIRED_SECTIONS[atype]:
                if section.lower() not in content.lower():
                    issues.append(("STRUCT", node_id, f"Отсутствует секция '{section}'"))

        if nums and content:
            num_vals = _extract_numbers(content)
            _check_numeric_conflicts(db, node_id, fname, full_path, num_vals, issues)

        _check_bidirectional(db, node_id, atype, issues)
        _check_empty_links(db, node_id, issues)

    # Coverage: missing cross-layer links
    _check_layer_gaps(db, node_id, row["type"], issues)

    if issues:
        print(f"\n┌─ Проблемы ({len(issues)}) ─────────────────────────────────")
        for sev, _, msg in issues:
            print(f"│ [{sev:6s}] {msg}")
        print(f"└{'─' * 55}")
    else:
        print("\n✓ Проблем не найдено")

    # ── Part 2: Context ──

    # Clusters
    clusters = db.execute(
        "SELECT cluster_name FROM semantic_clusters WHERE artifact_id = ?",
        (node_id,)
    ).fetchall()
    if clusters:
        print(f"\nКластеры: {', '.join(r['cluster_name'] for r in clusters)}")

    # Collect edges (skip FILE)
    out_edges = db.execute(
        "SELECT e.target_id as ref_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND COALESCE(a.type,'') != 'FILE' "
        "ORDER BY a.type, e.target_id",
        (node_id,)
    ).fetchall()
    in_edges = db.execute(
        "SELECT e.source_id as ref_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? AND COALESCE(a.type,'') != 'FILE' "
        "ORDER BY a.type, e.source_id",
        (node_id,)
    ).fetchall()

    if semantic:
        # ── Semantic mode: only linked artifacts (PRD itself is read separately) ──

        # Deduplicate linked artifacts — include FILE node edges if user passed a file
        seen_ids: set = set()
        linked_ids: list = []
        all_edges = list(out_edges) + list(in_edges)
        if explicit_file:
            file_node_id = f"FILE:{Path(explicit_file).name}"
            file_edges = db.execute(
                "SELECT e.target_id as ref_id, a.type, a.title "
                "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
                "WHERE e.source_id = ? AND COALESCE(a.type,'') != 'FILE' "
                "ORDER BY a.type, e.target_id",
                (file_node_id,)
            ).fetchall()
            all_edges.extend(file_edges)
        for r in all_edges:
            rid = r["ref_id"]
            if rid not in seen_ids:
                seen_ids.add(rid)
                linked_ids.append(rid)

        print(f"\n{'═' * 70}")
        print(f"  СВЯЗАННЫЕ АРТЕФАКТЫ ({len(linked_ids)})")
        print(f"{'═' * 70}")

        for rid in linked_ids:
            art = db.execute("SELECT * FROM artifacts WHERE id = ?", (rid,)).fetchone()
            if not art or not art["source_file"]:
                print(f"\n  ▸ {rid} — определение не найдено в БД")
                continue
            def_path = _resolve_file(db, art["source_file"])
            if not def_path:
                print(f"\n  ▸ {rid} — файл {art['source_file']} не найден")
                continue
            section = _read_artifact_section(def_path, art["line_number"] or 1,
                                               max_lines=lines)
            if section:
                print(f"\n{'─' * 70}")
                print(f"  {rid}: {art['title'] or ''}")
                print(f"  Файл: {art['source_file']}:{art['line_number']}")
                print(f"{'─' * 70}")
                print(section)
    else:
        # ── Normal mode: first 30 lines + edge snippets ──
        if full_path and Path(full_path).exists():
            try:
                own_lines = Path(full_path).read_text(encoding="utf-8").splitlines()[:30]
                print(f"\n── Содержание {fname} (первые {len(own_lines)} строк) ──")
                for i, line in enumerate(own_lines, 1):
                    print(f"  {i:4d}│ {line}")
            except Exception:
                pass

        if out_edges:
            print(f"\n── Исходящие ссылки ({len(out_edges)}) ──")
            for r in out_edges:
                _print_edge_context(db, "→", r["ref_id"], r["type"],
                                    r["title"], r["source_file"],
                                    r["line_number"], r["context"], 4)
        if in_edges:
            print(f"\n── Входящие ссылки ({len(in_edges)}) ──")
            for r in in_edges:
                _print_edge_context(db, "←", r["ref_id"], r["type"],
                                    r["title"], r["source_file"],
                                    r["line_number"], r["context"], 4)

    db.close()


def _check_layer_gaps(db, aid, atype, issues):
    """Check if this artifact has expected cross-layer links."""
    expected = {
        "F": [("BF", "бизнес-функции"), ("BR_REQ", "бизнес-требования"), ("M", "модули")],
        "BP": [("F", "фичи"), ("VAD", "цепочки ценности"), ("BD", "решения")],
        "BR_RULE": [("BR_REQ", "бизнес-требования"), ("BP", "бизнес-процессы")],
        "ST": [("BR_REQ", "бизнес-требования")],
    }
    pairs = expected.get(atype, [])
    for target_type, label in pairs:
        linked = db.execute(
            "SELECT 1 FROM edges e JOIN artifacts a ON e.target_id = a.id "
            "WHERE e.source_id = ? AND a.type = ? "
            "UNION SELECT 1 FROM edges e JOIN artifacts a ON e.source_id = a.id "
            "WHERE e.target_id = ? AND a.type = ?",
            (aid, target_type, aid, target_type)
        ).fetchone()
        if not linked:
            issues.append(("GAP", aid, f"Нет связей с {target_type} ({label})"))


# ── File / section reading helpers ────────────────────────────────

def _read_artifact_section(filepath: str, start_line: int,
                           max_lines: int = 200) -> Optional[str]:
    """Read from an artifact's definition line to the next same-or-higher-level heading.

    For heading-based artifacts (# / ## / ###): reads until the next heading
    of equal or higher level.
    For table-row artifacts (|...): reads the table header + this row + 2 lines.
    Falls back to max_lines if no boundary found.
    """
    p = Path(filepath)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    if start_line < 1 or start_line > len(lines):
        return None

    idx = start_line - 1
    first = lines[idx]

    # Determine heading level (0 if not a heading)
    heading_level = 0
    for ch in first:
        if ch == '#':
            heading_level += 1
        else:
            break

    # Table row: show header row + separator + this row + a couple more
    if first.lstrip().startswith('|') and heading_level == 0:
        # Walk back to find table header
        tbl_start = idx
        for i in range(idx - 1, max(idx - 5, -1), -1):
            if lines[i].lstrip().startswith('|'):
                tbl_start = i
            else:
                break
        tbl_end = min(len(lines), idx + 3)
        result = []
        for i in range(tbl_start, tbl_end):
            marker = "→" if i == idx else " "
            result.append(f"  {marker}{i+1:4d}│ {lines[i]}")
        return "\n".join(result)

    # Heading or plain text: read until next heading of same/higher level
    result = []
    for i in range(idx, min(len(lines), idx + max_lines)):
        line = lines[i]
        if i > idx and heading_level > 0:
            lvl = 0
            for ch in line:
                if ch == '#':
                    lvl += 1
                else:
                    break
            if lvl > 0 and lvl <= heading_level:
                break
        result.append(f"  {i+1:4d}│ {line}")

    return "\n".join(result)



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

import re

_NUM_PATTERNS = [
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(мин\w*|минут\w*)'), "мин"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(сек\w*)'), "сек"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(час\w*)'), "час"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*₽'), "₽"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(УЕТ)'), "УЕТ"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*%'), "%"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(заказ\w*|заказ)'), "заказ"),
    (re.compile(r'(\d+(?:[.,]\d+)?)\s*(повар\w*|курьер\w*)'), "персонал"),
]


def _extract_numbers(text: str) -> List[Tuple[str, str, str]]:
    """Extract numeric values with units from text. Returns [(value, unit, context)]."""
    results = []
    for line in text.splitlines():
        for pattern, unit_label in _NUM_PATTERNS:
            for m in pattern.finditer(line):
                val = m.group(1)
                ctx = line.strip()[:80]
                results.append((val, unit_label, ctx))
    return results



def _print_edge_context(db, arrow, ref_id, ref_type, ref_title,
                        source_file, line_number, edge_context, radius):
    """Print a single edge with its source file snippet."""
    ref_title = ref_title or ""
    print(f"\n  {arrow} [{ref_type or '?'}] {ref_id} — {ref_title[:55]}")
    print(f"    Ссылка в: {source_file}:{line_number}")
    if edge_context:
        print(f"    Контекст: {edge_context[:70]}")

    if line_number and source_file:
        full_path = _resolve_file(db, source_file)
        if full_path:
            snippet = _read_snippet(full_path, line_number, radius)
            if snippet:
                print(snippet)


# ── Validation helpers (used by review) ─────────────────────────

_REQUIRED_SECTIONS = {
    "BR_RULE": ["Описание правила", "Входные параметры", "Выход"],
    "BD": ["Контекст", "решение"],
    "F": ["Goal", "Scope", "Traceability"],
}



def _check_numeric_conflicts(db, aid, fname, full_path,
                              nums: List[Tuple[str, str, str]],
                              issues: List[Tuple[str, str, str]]):
    """Check if numeric values in this artifact conflict with directly connected artifacts.

    Only compares numbers that share the same unit AND have overlapping context words
    (to avoid false positives like "таймер 30 мин" vs "доставка 10 мин").
    Only checks direct neighbors, not FILE nodes.
    """
    if not nums:
        return

    # Get directly connected artifact files (skip FILE nodes)
    connected = db.execute(
        "SELECT DISTINCT a.id as ref_id, a.source_file "
        "FROM edges e JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND a.type != 'FILE' "
        "UNION "
        "SELECT DISTINCT a.id as ref_id, a.source_file "
        "FROM edges e JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? AND a.type != 'FILE'",
        (aid, aid)
    ).fetchall()

    # Extract nums from connected artifacts
    all_nums: List[Tuple[str, str, str, str]] = []  # (value, unit, context_words, artifact_id)
    for val, unit, ctx_line in nums:
        words = _context_keywords(ctx_line)
        all_nums.append((val, unit, words, aid))

    for conn in connected:
        ref_path = _resolve_file(db, conn["source_file"])
        if not ref_path or not Path(ref_path).exists():
            continue
        try:
            ref_content = Path(ref_path).read_text(encoding="utf-8")
        except Exception:
            continue
        for val, unit, ctx_line in _extract_numbers(ref_content):
            words = _context_keywords(ctx_line)
            all_nums.append((val, unit, words, conn["ref_id"]))

    # Group by unit, then check for conflicts only among entries with overlapping context
    by_unit: dict = {}
    for val, unit, words, src in all_nums:
        by_unit.setdefault(unit, []).append((val, words, src))

    for unit, entries in by_unit.items():
        # Compare pairs: only flag if different values AND shared context words
        seen_conflicts: set = set()
        for i, (v1, w1, s1) in enumerate(entries):
            if s1 != aid:
                continue  # only check from the artifact being validated
            for j, (v2, w2, s2) in enumerate(entries):
                if s2 == aid or v1 == v2:
                    continue
                overlap = w1 & w2
                if len(overlap) >= 2:  # at least 2 shared context words
                    key = (min(v1, v2), max(v1, v2), unit, frozenset({s1, s2}))
                    if key not in seen_conflicts:
                        seen_conflicts.add(key)
                        shared = ", ".join(sorted(overlap)[:3])
                        issues.append(("NUM", aid,
                                       f"{s1}: {v1} {unit} vs {s2}: {v2} {unit}"
                                       f" (общий контекст: {shared})"))


_STOP_WORDS = frozenset("в на из по с к у о а и или не для при до за от".split())


def _context_keywords(line: str) -> frozenset:
    """Extract meaningful keywords from a context line for matching."""
    words = set()
    for w in re.findall(r'[а-яёА-ЯЁa-zA-Z_]{4,}', line.lower()):
        if w not in _STOP_WORDS:
            words.add(w[:6])  # truncate for crude stemming
    return frozenset(words)


def _check_bidirectional(db, aid, atype, issues):
    """Check for one-way links that should be bidirectional."""
    # Expected bidirectional pairs
    expected_pairs = {
        "BR_RULE": ["BP", "BR_REQ"],
        "BP": ["BD", "VAD"],
        "F": ["BF", "BR_REQ"],
    }
    expected = expected_pairs.get(atype, [])
    if not expected:
        return

    # Outgoing targets
    out_targets = db.execute(
        "SELECT DISTINCT e.target_id, a.type FROM edges e "
        "JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ?", (aid,)
    ).fetchall()

    for target_row in out_targets:
        tid = target_row["target_id"]
        ttype = target_row["type"]
        if ttype not in expected:
            continue
        # Check if reverse link exists
        rev = db.execute(
            "SELECT 1 FROM edges WHERE source_id = ? AND target_id = ?",
            (tid, aid)
        ).fetchone()
        if not rev:
            issues.append(("REF", aid,
                           f"{aid}→{tid} есть, но {tid}→{aid} отсутствует"))


def _check_empty_links(db, aid, issues):
    """Find edges with no meaningful context."""
    empties = db.execute(
        "SELECT target_id, source_file, line_number FROM edges "
        "WHERE source_id = ? AND (context IS NULL OR context = '')",
        (aid,)
    ).fetchall()
    for e in empties:
        if e["line_number"] and e["line_number"] > 0:
            # Has line number — check if there's actual text around it
            full_path = _resolve_file(db, e["source_file"])
            if full_path:
                snippet = _read_snippet(full_path, e["line_number"], 1)
                if snippet and len(snippet.strip()) < 20:
                    issues.append(("EMPTY", aid,
                                   f"→{e['target_id']} в {e['source_file']}:{e['line_number']}"
                                   " — голая ссылка без контекста"))



if __name__ == "__main__":
    cli()

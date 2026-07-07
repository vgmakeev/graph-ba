"""MCP stdio server for structured Graph BA access."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from graph_ba.audit import run_anomalies, run_audit, run_coverage
from graph_ba.config import load_config
from graph_ba.db import _fts_query, _load_nx, do_import, get_db, graph_is_stale
from graph_ba.review import run_review

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - exercised only without extra
    raise SystemExit(
        "graph-ba-mcp requires the optional dependency: graph-ba[mcp]"
    ) from exc


mcp = FastMCP("graph-ba")


def _db_path(root: Path, db_path: Optional[str]) -> Path:
    return Path(db_path) if db_path else root / "reports" / "graph.db"


def _open(root: str = ".", db_path: Optional[str] = None):
    project_root = Path(root).resolve()
    db = get_db(_db_path(project_root, db_path))
    config = load_config(project_root)
    n = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    if n == 0 or graph_is_stale(db, project_root, config):
        do_import(project_root, db, quiet=True)
    return project_root, config, db


@mcp.tool()
def ba_search(query: str, root: str = ".", db_path: Optional[str] = None,
              limit: int = 20) -> dict:
    """Full-text search across artifact IDs, titles, clusters and edge contexts."""
    _, _, db = _open(root, db_path)
    fq = _fts_query(query)
    artifacts = db.execute(
        "SELECT a.id, a.type, a.origin, a.title, a.source_file "
        "FROM artifacts_fts f JOIN artifacts a ON f.rowid = a.rowid "
        "WHERE artifacts_fts MATCH ? ORDER BY rank LIMIT ?",
        (fq, limit)
    ).fetchall()
    clusters = db.execute(
        "SELECT DISTINCT cluster_name FROM clusters_fts "
        "WHERE clusters_fts MATCH ? LIMIT ?",
        (fq, limit)
    ).fetchall()
    edges = db.execute(
        "SELECT source_id, target_id, relation_type, context FROM edges_fts "
        "WHERE edges_fts MATCH ? LIMIT ?",
        (fq, limit)
    ).fetchall()
    db.close()
    return {
        "artifacts": [dict(r) for r in artifacts],
        "clusters": [dict(r) for r in clusters],
        "edges": [dict(r) for r in edges],
    }


@mcp.tool()
def ba_schema(root: str = ".", db_path: Optional[str] = None) -> dict:
    """Return configured artifact types, origin enum and relation type enum."""
    _, config, db = _open(root, db_path)
    origins = db.execute(
        "SELECT id, label, description FROM artifact_origins ORDER BY id"
    ).fetchall()
    relations = db.execute(
        "SELECT id, label, description, direction FROM relation_types ORDER BY id"
    ).fetchall()
    db.close()
    return {
        "types": [
            {
                "id": tid,
                "label": tdef.label,
                "origin": tdef.origin,
                "restrict_to": tdef.restrict_to or [],
            }
            for tid, tdef in config.types.items()
        ],
        "origins": [dict(r) for r in origins],
        "relation_types": [dict(r) for r in relations],
        "coverage": [
            {"source": c.source, "target": c.target, "label": c.label}
            for c in config.coverage_pairs
        ],
    }


@mcp.tool()
def ba_node(node_id: str, root: str = ".", db_path: Optional[str] = None) -> dict:
    """Return artifact details and immediate neighbors."""
    _, _, db = _open(root, db_path)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (node_id,)).fetchone()
    if not row:
        db.close()
        return {"error": f"Artifact '{node_id}' not found", "id": node_id}
    clusters = db.execute(
        "SELECT cluster_name FROM semantic_clusters WHERE artifact_id = ?",
        (node_id,)
    ).fetchall()
    outgoing = db.execute(
        "SELECT e.target_id, a.type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? ORDER BY a.type, e.target_id",
        (node_id,)
    ).fetchall()
    incoming = db.execute(
        "SELECT e.source_id, a.type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? ORDER BY a.type, e.source_id",
        (node_id,)
    ).fetchall()
    db.close()
    return {
        "id": row["id"],
        "type": row["type"],
        "origin": row["origin"],
        "title": row["title"],
        "source_file": row["source_file"],
        "line_number": row["line_number"],
        "defined": bool(row["defined"]),
        "clusters": [r["cluster_name"] for r in clusters],
        "outgoing": [dict(r) for r in outgoing],
        "incoming": [dict(r) for r in incoming],
    }


@mcp.tool()
def ba_review(node_id_or_file: str, root: str = ".",
              db_path: Optional[str] = None, semantic: bool = False,
              lines: int = 10, nums: bool = False,
              types: Optional[str] = None) -> dict:
    """Run structured artifact review."""
    project_root, config, db = _open(root, db_path)
    data = run_review(db, project_root, config, node_id_or_file,
                      semantic=semantic, lines=lines, nums=nums, types=types)
    db.close()
    return data


@mcp.tool()
def ba_impact(node_id: str, root: str = ".", db_path: Optional[str] = None) -> dict:
    """Return descendants and ancestors for an artifact."""
    import networkx as nx

    _, _, db = _open(root, db_path)
    G = _load_nx(db)
    db.close()
    if node_id not in G:
        return {"error": f"Node '{node_id}' not found", "node": node_id}
    descendants = {}
    for nid in nx.descendants(G, node_id):
        t = G.nodes[nid].get("type", "?")
        descendants.setdefault(t, []).append(nid)
    ancestors = {}
    for nid in nx.ancestors(G, node_id):
        t = G.nodes[nid].get("type", "?")
        ancestors.setdefault(t, []).append(nid)
    return {
        "node": node_id,
        "type": G.nodes[node_id].get("type", "?"),
        "descendants": {"total": sum(len(v) for v in descendants.values()),
                        "by_type": {t: sorted(ids) for t, ids in descendants.items()}},
        "ancestors": {"total": sum(len(v) for v in ancestors.values()),
                      "by_type": {t: sorted(ids) for t, ids in ancestors.items()}},
    }


@mcp.tool()
def ba_coverage(root: str = ".", db_path: Optional[str] = None) -> dict:
    """Return cross-layer, code, test and UI coverage."""
    _, config, db = _open(root, db_path)
    data = run_coverage(db, config)
    db.close()
    return data


@mcp.tool()
def ba_anomalies(root: str = ".", db_path: Optional[str] = None,
                 min_component: int = 2) -> dict:
    """Return graph anomaly report."""
    _, _, db = _open(root, db_path)
    data = run_anomalies(db, min_component)
    db.close()
    return data


@mcp.tool()
def ba_audit(root: str = ".", db_path: Optional[str] = None,
             top: int = 30) -> dict:
    """Return global audit report."""
    project_root, config, db = _open(root, db_path)
    data = run_audit(db, project_root, config, top)
    db.close()
    return data


@mcp.tool()
def ba_path(from_id: str, to_id: str, root: str = ".",
            db_path: Optional[str] = None) -> dict:
    """Return shortest directed or undirected path between two artifacts."""
    import networkx as nx

    _, _, db = _open(root, db_path)
    G = _load_nx(db)
    db.close()
    if from_id not in G:
        return {"error": f"Node '{from_id}' not found", "from": from_id}
    if to_id not in G:
        return {"error": f"Node '{to_id}' not found", "to": to_id}
    for label, graph in [("directed", G), ("undirected", G.to_undirected())]:
        try:
            path = nx.shortest_path(graph, from_id, to_id)
            return {"mode": label, "steps": len(path) - 1, "path": path}
        except nx.NetworkXNoPath:
            continue
    return {"mode": None, "steps": None, "path": []}


@mcp.tool()
def ba_sql(query: str, root: str = ".", db_path: Optional[str] = None) -> dict:
    """Run a read-only SQL query against the graph cache."""
    _, _, db = _open(root, db_path)
    db.execute("PRAGMA query_only=ON")
    try:
        rows = db.execute(query).fetchall()
    except sqlite3.Error as exc:
        db.close()
        return {"error": str(exc)}
    db.close()
    return {"rows": [dict(r) for r in rows]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

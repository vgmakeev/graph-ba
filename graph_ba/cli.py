"""Click command line interface for graph-ba."""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import sys
import shutil
from pathlib import Path
from typing import Any, List, Tuple
from datetime import date

import click

from graph_ba import __version__
from graph_ba.audit import (
    _issue_fingerprints,
    run_anomalies,
    run_audit,
    run_coverage,
)
from graph_ba.db import (
    _FileCache,
    _fts_query,
    _load_nx,
    _read_snippet,
    _resolve_file,
    do_import,
    get_db,
    graph_is_stale,
)
from graph_ba.lint import do_lint, _lint_todo_markers
from graph_ba.review import (
    _artifact_section_text,
    _check_bidirectional,
    _check_empty_links,
    _check_layer_gaps,
    _check_numeric_conflicts,
    _extract_numbers,
    _print_edge_context,
    _read_artifact_section,
    run_review,
    run_validate,
)

def _is_meta_node(node_id: str) -> bool:
    """Check if node is a meta-node (FILE:, CODE:, TEST: or UI:) rather than a BA artifact."""
    return (node_id.startswith("FILE:") or node_id.startswith("CODE:")
            or node_id.startswith("TEST:") or node_id.startswith("UI:"))


def _json_out(ctx, data):
    """Print data as JSON if --json flag is set, return True if printed."""
    if ctx.obj.get("json"):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return True
    return False


def _csv_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}

# ── Schema ────────────────────────────────────────────────────────
def fmt_table(rows: list, headers: list) -> str:
    """Format rows as a compact aligned table."""
    if not rows:
        return "(empty)"
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
@click.version_option(__version__, prog_name="graph-ba")
@click.option("--db", type=click.Path(path_type=Path), default=None,
              help=f"Path to SQLite DB (default: reports/graph.db)")
@click.option("--root", type=click.Path(exists=True, path_type=Path),
              default=".", help="Project root directory")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-auto-import", is_flag=True,
              help="Do not rebuild the graph automatically when it is empty or stale")
@click.pass_context
def cli(ctx, db, root, json_output, no_auto_import):
    """graph-ba — query the artifact traceability graph."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db
    ctx.obj["root"] = str(Path(root).resolve())
    ctx.obj["json"] = json_output
    ctx.obj["no_auto_import"] = no_auto_import


def _conn(ctx) -> sqlite3.Connection:
    return get_db(ctx.obj.get("db_path"))


def _auto_import(ctx, db: sqlite3.Connection, reason: str) -> bool:
    """Rebuild the graph in place when the config is available."""
    from graph_ba.config import load_config
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        load_config(root)
    except Exception:
        return False
    click.echo(f"auto-import: graph {reason} — rebuilding", err=True)
    do_import(root, db, quiet=True)
    return True


def _require_graph(ctx, db: sqlite3.Connection) -> None:
    """Keep read commands honest: rebuild or fail on an empty/stale graph.

    An empty DB makes every read command return a clean result, which reads
    as "no issues" when the real problem is that import was never run. By
    default the graph is rebuilt automatically (import is cheap); with
    --no-auto-import the old fail/warn behavior applies.
    """
    auto = not ctx.obj.get("no_auto_import")

    n = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    if n == 0:
        if auto and _auto_import(ctx, db, "was empty"):
            n = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        if n == 0:
            db_path = db.execute("PRAGMA database_list").fetchone()[2]
            raise click.ClickException(
                f"Graph is empty (db: {db_path}). Run `graph-ba import` first.")
        return

    row = db.execute("SELECT value FROM meta WHERE key = 'import_time'").fetchone()
    if not row:
        if auto and _auto_import(ctx, db, "predates staleness tracking"):
            return
        click.echo("warning: DB predates staleness tracking — "
                   "re-run `graph-ba import` to enable it", err=True)
        return
    import_time = float(row["value"])

    try:
        from graph_ba.config import load_config
        root = Path(ctx.obj.get("root", ".")).resolve()
        config = load_config(root)
        if graph_is_stale(db, root, config):
            if auto and _auto_import(ctx, db, "is stale"):
                return
            click.echo("warning: graph is stale — sources modified "
                       "after last import; run `graph-ba import`", err=True)
            return
    except Exception:
        pass


@cli.command("import")
@click.option("--force", is_flag=True, help="Force full rebuild even if files are unchanged")
@click.pass_context
def cmd_import(ctx, force):
    """Scan artifacts and populate the SQLite DB."""
    root = Path(ctx.obj.get("root", "."))
    db = _conn(ctx)
    do_import(root, db, force=force)
    db.close()


@cli.command("init")
@click.pass_context
def cmd_init(ctx):
    """Create a template graph-ba.toml in the project root."""
    from graph_ba.config import CONFIG_FILENAME
    root = Path(ctx.obj.get("root", "."))
    config_path = root / CONFIG_FILENAME
    if config_path.exists():
        print(f"Config already exists: {config_path}")
        return

    template = '''\
# graph-ba.toml — project configuration for graph-ba
# Defines artifact types, scan rules, and cross-reference patterns.

[scan]
# Directories to scan for .md files (relative to project root)
dirs = ["docs"]

# ID normalization rules
[normalize]
# Character replacements (e.g. Cyrillic → Latin)
char_map = {}
# Zero-padding rules: { pattern = "regex with group(1)=number", format = "python format string" }
zero_pad = []

# Range expansion pattern (for references like REQ.1.1–REQ.1.5)
range_pattern = '((?:REQ|FUNC)\\.\\d+\\.)(\\d+)\\s*[–\\-]\\s*(?:(?:REQ|FUNC)\\.\\d+\\.)(\\d+)'

# ── Artifact origin enum ──
# graph-ba ships defaults: human, derived, canonical, evidence,
# implementation, container, unknown. Projects can override labels or add
# stricter provenance classes.
# [origins.human]
# label = "Human primary source"
# description = "Client, stakeholder, refined meeting or human dictation input."
#
# [origins.reviewed_derived]
# label = "Reviewed derived artifact"
# description = "Agent or analyst output reviewed by a human analyst."

# ── Edge relation enum ──
# graph-ba emits MENTIONS, INDEX, CODE_TRACE, TEST_EVIDENCE and UI_TRACE.
# It also suggests semantic relation names for agent-first traceability:
# DERIVES_FROM, NORMALIZES, IMPLEMENTS, VERIFIES, RENDERS, CONFLICTS_WITH,
# SUPERSEDES, TRACE_GAP. Projects can add their own relation types.
# [relations.NORMALIZES]
# label = "Normalizes"
# description = "Canonical AC normalizes raw source material."
# direction = "canonical_to_source"

# ── Artifact types ──
# Each type needs:
#   ref = regex to find references in text (group 1 = full ID)
#   classify = regex to classify an ID string (used with fullmatch)
#   label = human-readable name
#   origin = optional provenance class (human, derived, evidence, implementation, ...)
#   restrict_to = optional list of files/dirs where this pattern is allowed

[types.REQ]
label = "Requirements"
origin = "derived"
ref = '(?<![A-Za-z])(REQ-\\d{2,4})(?!\\d)'
classify = 'REQ-\\d{2,4}'

[types.FEAT]
label = "Features"
origin = "derived"
ref = '(?<![A-Za-z])(FEAT-\\d{2,4})(?!\\d)'
classify = 'FEAT-\\d{2,4}'

# ── Definition scan rules ──
# type = artifact type ID
# file = relative path (supports * glob patterns)
# mode = "heading" (match heading lines) or "table" (match table rows)
# pattern = regex (group 1 = ID, group 2 = title)

[[definitions]]
type = "REQ"
file = "docs/requirements.md"
mode = "table"
pattern = '^\\|\\s*(REQ-\\d{2,4})\\s*\\|'

[[definitions]]
type = "FEAT"
file = "docs/features.md"
mode = "heading"
pattern = '^##\\s+(FEAT-\\d{2,4})\\s*[—–\\-]\\s*(.*)'

# ── Index tables (extract cross-refs from table rows) ──
# file = path to index file
# first_col = regex matching the source ID in the first column

# [[index_tables]]
# file = "docs/features.md"
# first_col = '^\\|\\s*(FEAT-\\d{2,4})\\s*\\|'

# ── Coverage expectations ──
# source/target = type IDs, label = display name

# [[coverage]]
# source = "FEAT"
# target = "REQ"
# label = "FEAT → REQ"

# ── Review validation ──
[review]
# Required sections in artifacts of a given type
# required_sections = { "FEAT" = ["Goal", "Scope"] }

# Expected bidirectional links
# expected_bidir = { "FEAT" = ["REQ"] }

# Expected cross-layer links for review
# [[review.expected_cross_layer.FEAT]]
# type = "REQ"
# label = "requirements"

# ── Semantic clusters ──
[clusters]
# "Topic Name" = ["ID-01", "ID-02"]

# ── Code traceability ──
# Scan source files for @trace comments referencing artifacts.
# Example: // @trace: REQ-01, FEAT-02
# [code]
# dirs = ["src"]
# extensions = ["ts", "tsx", "py", "go"]
# marker = "@trace"
# coverage_types = ["FEAT", "REQ"]

# ── Test traceability ──
# Test files become TEST: nodes; any artifact ID found in a test file
# counts as test evidence (no marker needed).
# [tests]
# dirs = ["tests"]
# extensions = ["py", "ts", "tsx", "js", "dart"]
# coverage_types = ["REQ"]

# ── UI traceability ──
# UI trace sidecars (e.g. trace.json mapping data-testid -> AC IDs) become
# UI: nodes; any artifact ID found in them counts as a UI link.
# [ui]
# files = ["app/src/features/*/api/trace.json"]
# coverage_types = ["REQ"]
'''
    config_path.write_text(template, encoding="utf-8")
    print(f"Created template config: {config_path}")
    print("Edit it to match your project's artifact naming conventions.")


@cli.command()
@click.argument("query")
@click.option("-n", "--limit", default=20, help="Max results")
@click.pass_context
def search(ctx, query, limit):
    """Full-text search across artifact titles and IDs."""
    db = _conn(ctx)
    _require_graph(ctx, db)
    fq = _fts_query(query)

    # Search artifacts
    rows = db.execute(
        "SELECT a.id, a.type, a.origin, a.title, a.source_file "
        "FROM artifacts_fts f JOIN artifacts a ON f.rowid = a.rowid "
        "WHERE artifacts_fts MATCH ? ORDER BY rank LIMIT ?",
        (fq, limit)
    ).fetchall()

    # Search clusters
    cl_rows = db.execute(
        "SELECT DISTINCT cluster_name FROM clusters_fts "
        "WHERE clusters_fts MATCH ? LIMIT ?",
        (fq, limit)
    ).fetchall()

    # Search edge contexts
    e_rows = db.execute(
        "SELECT source_id, target_id, relation_type, context FROM edges_fts "
        "WHERE edges_fts MATCH ? LIMIT ?",
        (fq, limit)
    ).fetchall()
    db.close()

    if _json_out(ctx, {
        "artifacts": [dict(r) for r in rows],
        "clusters": [dict(r) for r in cl_rows],
        "edges": [dict(r) for r in e_rows],
    }):
        return

    if rows:
        print(f"── Artifacts ({len(rows)}) ──")
        print(fmt_table(
            [(r["id"], r["type"], r["origin"], r["title"][:60], r["source_file"])
             for r in rows],
            ["ID", "Type", "Origin", "Title", "File"]
        ))
    else:
        print("Artifacts: not found")

    if cl_rows:
        print(f"\n── Clusters ({len(cl_rows)}) ──")
        for r in cl_rows:
            print(f"  • {r['cluster_name']}")

    if e_rows:
        print(f"\n── Edges ({len(e_rows)}) ──")
        print(fmt_table(
            [(r["source_id"], r["target_id"], r["relation_type"], r["context"][:60])
             for r in e_rows],
            ["From", "To", "Relation", "Context"]
        ))


@cli.command()
@click.argument("node_id")
@click.pass_context
def node(ctx, node_id):
    """Show node details and immediate neighbors."""
    db = _conn(ctx)
    _require_graph(ctx, db)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (node_id,)).fetchone()
    if not row:
        # Try case-insensitive / partial match
        rows = db.execute(
            "SELECT * FROM artifacts WHERE id LIKE ? LIMIT 5",
            (f"%{node_id}%",)
        ).fetchall()
        if rows:
            print(f"Not found '{node_id}'. Similar:")
            for r in rows:
                print(f"  {r['id']} ({r['type']}) — {r['title'][:50]}")
        else:
            print(f"Artifact '{node_id}' not found")
        db.close()
        return

    # Clusters
    clusters = db.execute(
        "SELECT cluster_name FROM semantic_clusters WHERE artifact_id = ?",
        (node_id,)
    ).fetchall()

    # Out-edges
    out = db.execute(
        "SELECT e.target_id, a.type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? ORDER BY a.type, e.target_id",
        (node_id,)
    ).fetchall()

    # In-edges
    inc = db.execute(
        "SELECT e.source_id, a.type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? ORDER BY a.type, e.source_id",
        (node_id,)
    ).fetchall()

    if _json_out(ctx, {
        "id": row["id"], "type": row["type"], "origin": row["origin"],
        "title": row["title"],
        "source_file": row["source_file"], "line_number": row["line_number"],
        "defined": bool(row["defined"]),
        "clusters": [r["cluster_name"] for r in clusters],
        "outgoing": [dict(r) for r in out],
        "incoming": [dict(r) for r in inc],
    }):
        db.close()
        return

    print(f"ID:      {row['id']}")
    print(f"Type:    {row['type']}")
    if row["origin"]:
        print(f"Origin:  {row['origin']}")
    print(f"File:    {row['source_file']}:{row['line_number']}")
    print(f"Defined: {'yes' if row['defined'] else 'NO (dangling)'}")
    print(f"Title:   {row['title']}")

    if clusters:
        print(f"Clusters: {', '.join(r['cluster_name'] for r in clusters)}")

    print(f"\n→ Outgoing ({len(out)}):")
    if out:
        print(fmt_table(
            [(r["target_id"], r["type"] or "?",
              r["relation_type"],
              f"{r['source_file']}:{r['line_number']}" if r["line_number"] else "",
              r["title"][:40] if r["title"] else "") for r in out],
            ["ID", "Type", "Relation", "Ref location", "Title"]
        ))

    print(f"\n← Incoming ({len(inc)}):")
    if inc:
        print(fmt_table(
            [(r["source_id"], r["type"] or "?",
              r["relation_type"],
              f"{r['source_file']}:{r['line_number']}" if r["line_number"] else "",
              r["title"][:40] if r["title"] else "") for r in inc],
            ["ID", "Type", "Relation", "Ref location", "Title"]
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
    _require_graph(ctx, db)
    G = _load_nx(db)
    db.close()

    if from_id not in G:
        print(f"Node '{from_id}' not found")
        return
    if to_id not in G:
        print(f"Node '{to_id}' not found")
        return

    # Try directed first, then undirected
    for label, graph in [("directed", G), ("undirected", G.to_undirected())]:
        try:
            p = nx.shortest_path(graph, from_id, to_id)
            print(f"Shortest path ({label}, {len(p)-1} steps):")
            for i, nid in enumerate(p):
                data = G.nodes.get(nid, {})
                arrow = "  →  " if i < len(p) - 1 else ""
                print(f"  [{data.get('type','?')}] {nid} — {data.get('title','')[:50]}{arrow}")
            return
        except nx.NetworkXNoPath:
            continue

    print(f"No path between {from_id} and {to_id}")


@cli.command()
@click.argument("node_id")
@click.option("--depth", default=10, help="Max traversal depth")
@click.pass_context
def impact(ctx, node_id, depth):
    """Cascade impact analysis: what does changing this artifact affect?"""
    import networkx as nx

    db = _conn(ctx)
    _require_graph(ctx, db)
    G = _load_nx(db)
    db.close()

    if node_id not in G:
        if _json_out(ctx, {"error": f"Node '{node_id}' not found", "node": node_id}):
            return
        print(f"Node '{node_id}' not found")
        return

    # BFS from node, follow outgoing edges
    reachable = nx.descendants(G, node_id)

    # Group descendants by type
    descendants_by_type: dict = {}
    for nid in reachable:
        t = G.nodes[nid].get("type", "?")
        descendants_by_type.setdefault(t, []).append(nid)

    # Reverse: what affects this node?
    ancestors = nx.ancestors(G, node_id)
    ancestors_by_type: dict = {}
    for nid in ancestors:
        t = G.nodes[nid].get("type", "?")
        ancestors_by_type.setdefault(t, []).append(nid)

    # JSON output
    if _json_out(ctx, {
        "node": node_id,
        "type": G.nodes[node_id].get("type", "?"),
        "descendants": {
            "total": len(reachable),
            "by_type": {t: sorted(ids) for t, ids in sorted(descendants_by_type.items())},
        },
        "ancestors": {
            "total": len(ancestors),
            "by_type": {t: sorted(ids) for t, ids in sorted(ancestors_by_type.items())},
        },
    }):
        return

    # Text output
    if not reachable:
        print(f"{node_id}: no cascade impact (no outgoing paths)")
        return

    print(f"Cascade impact {node_id}: {len(reachable)} artifacts")
    print()
    for t in sorted(descendants_by_type):
        ids = sorted(descendants_by_type[t])
        print(f"  [{t}] ({len(ids)}): {', '.join(ids[:15])}")
        if len(ids) > 15:
            print(f"         ... and {len(ids)-15} more")

    if ancestors:
        print(f"\nReverse impact (what affects {node_id}): {len(ancestors)} artifacts")
        for t in sorted(ancestors_by_type):
            ids = sorted(ancestors_by_type[t])
            print(f"  [{t}] ({len(ids)}): {', '.join(ids[:15])}")
            if len(ids) > 15:
                print(f"         ... and {len(ids)-15} more")






@cli.command("sql")
@click.argument("query")
@click.pass_context
def raw_sql(ctx, query):
    """Execute raw SQL query."""
    db = _conn(ctx)
    try:
        rows = db.execute(query).fetchall()
        if not rows:
            print("(empty)")
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


@cli.command("admin-component-traces")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write generated mini-admin component trace map to this file")
@click.option("--format", "output_format",
              type=click.Choice(["json", "ts"]), default="json",
              help="Output format")
@click.pass_context
def admin_component_traces(ctx, out_path, output_format):
    """Export graph-ba generated mini-admin component overlay traces."""
    from graph_ba.config import load_config
    from graph_ba.traceability import export_mini_admin_component_trace_map

    root = Path(ctx.obj.get("root", ".")).resolve()
    config = load_config(root)
    data = export_mini_admin_component_trace_map(root, config)

    if output_format == "ts":
        content = (
            "/* eslint-disable */\n"
            "// Generated by graph-ba admin-component-traces. Do not edit by hand.\n"
            "import type { AdminComponentTraceMap } from \"@mini/admin/data-source-inspector\";\n\n"
            "export const adminComponentTracesByScreen = "
            + json.dumps(data, ensure_ascii=False, indent=2)
            + " satisfies Record<string, AdminComponentTraceMap>;\n"
        )
    else:
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"wrote {out_path}")
        return
    print(content, end="")



@cli.command()
@click.pass_context
def coverage(ctx):
    """Show cross-layer coverage matrix."""
    from graph_ba.config import load_config
    root = Path(ctx.obj.get("root", ".")).resolve()
    config = load_config(root)

    db = _conn(ctx)
    _require_graph(ctx, db)
    has_any = (config.coverage_pairs or (config.code and config.code.coverage_types)
               or (config.tests and config.tests.coverage_types)
               or (config.ui and config.ui.coverage_types))
    if not has_any:
        print("No coverage pairs defined in graph-ba.toml [coverage]")
        db.close()
        return
    data = run_coverage(db, config)
    db.close()

    if _json_out(ctx, data):
        return

    if data["pairs"]:
        print("Cross-layer coverage matrix:")
        print()
        for r in data["pairs"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(f"  {r['source']:8s} ↔ {r['target']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                  f"{bar}  {r['pct']:5.1f}%  [{r['status']}]")

    if data["code_coverage"]:
        print("\nCode reference coverage:")
        print()
        for r in data["code_coverage"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(f"  CODE → {r['type']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                  f"{bar}  {r['pct']:5.1f}%  [{r['status']}]")

    if data["test_coverage"]:
        print("\nTest coverage:")
        print()
        for r in data["test_coverage"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(f"  TEST → {r['type']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                  f"{bar}  {r['pct']:5.1f}%  [{r['status']}]")

    if data["ui_coverage"]:
        print("\nUI trace coverage:")
        print()
        for r in data["ui_coverage"]:
            bar = "█" * int(r["pct"] / 5) + "░" * (20 - int(r["pct"] / 5))
            print(f"  UI   → {r['type']:8s}  {r['linked']:3d}/{r['total']:<3d}  "
                  f"{bar}  {r['pct']:5.1f}%  [{r['status']}]")


@cli.command("matrix")
@click.option("--source-type", default=None,
              help="Comma-separated source artifact types, e.g. TEST,CODE,UIC")
@click.option("--target-type", default=None,
              help="Comma-separated target artifact types, e.g. AC,RULE")
@click.option("--relation", "relation_filter", default=None,
              help="Comma-separated relation types, e.g. TEST_EVIDENCE,IMPLEMENTS")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write sparse matrix JSON to this file")
@click.pass_context
def matrix(ctx, source_type, target_type, relation_filter, out_path):
    """Export sparse artifact relationship matrix as JSON."""
    db = _conn(ctx)
    _require_graph(ctx, db)

    source_types = _csv_filter(source_type)
    target_types = _csv_filter(target_type)
    relations = _csv_filter(relation_filter)

    query = (
        "SELECT e.source_id, s.type AS source_type, s.title AS source_title, "
        "e.target_id, t.type AS target_type, t.title AS target_title, "
        "e.relation_type, e.context, e.source_file, e.line_number "
        "FROM edges e "
        "LEFT JOIN artifacts s ON e.source_id = s.id "
        "LEFT JOIN artifacts t ON e.target_id = t.id "
        "WHERE 1 = 1"
    )
    params: list[str] = []
    if source_types:
        query += f" AND s.type IN ({','.join('?' for _ in source_types)})"
        params.extend(sorted(source_types))
    if target_types:
        query += f" AND t.type IN ({','.join('?' for _ in target_types)})"
        params.extend(sorted(target_types))
    if relations:
        query += f" AND e.relation_type IN ({','.join('?' for _ in relations)})"
        params.extend(sorted(relations))
    query += " ORDER BY e.source_id, e.target_id, e.relation_type"

    rows = [dict(row) for row in db.execute(query, params).fetchall()]
    db.close()

    nodes: dict[str, dict[str, object]] = {}
    entries: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        source_id = str(row["source_id"])
        target_id = str(row["target_id"])
        relation_type = str(row["relation_type"])
        nodes[source_id] = {
            "id": source_id,
            "type": row.get("source_type") or "UNKNOWN",
            "title": row.get("source_title") or "",
        }
        nodes[target_id] = {
            "id": target_id,
            "type": row.get("target_type") or "UNKNOWN",
            "title": row.get("target_title") or "",
        }
        key = (source_id, target_id, relation_type)
        entry = entries.setdefault(
            key,
            {
                "source": source_id,
                "target": target_id,
                "relation_type": relation_type,
                "source_type": row.get("source_type") or "UNKNOWN",
                "target_type": row.get("target_type") or "UNKNOWN",
                "count": 0,
                "evidence": [],
            },
        )
        entry["count"] = int(entry["count"]) + 1
        evidence = entry["evidence"]
        if isinstance(evidence, list):
            evidence.append(
                {
                    "source_file": row.get("source_file") or "",
                    "line_number": row.get("line_number") or 0,
                    "context": row.get("context") or "",
                }
            )

    payload = {
        "schema": "graph-ba.sparse-matrix.v1",
        "filters": {
            "source_type": sorted(source_types),
            "target_type": sorted(target_types),
            "relation": sorted(relations),
        },
        "nodes": sorted(nodes.values(), key=lambda item: str(item["id"])),
        "matrix": {
            "rows": sorted({entry["source"] for entry in entries.values()}),
            "columns": sorted({entry["target"] for entry in entries.values()}),
            "entries": sorted(
                entries.values(),
                key=lambda item: (
                    str(item["source"]),
                    str(item["target"]),
                    str(item["relation_type"]),
                ),
            ),
        },
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote sparse matrix JSON: {out_path}")
            return
    print(text)


@cli.command("artifact-state")
@click.argument("artifact_id", required=False, default=None)
@click.option("--snapshot", "snapshot_path", type=click.Path(path_type=Path), default=None,
              help="Accepted fingerprint snapshot JSON to compare against")
@click.option("--write-snapshot", "write_snapshot_path", type=click.Path(path_type=Path), default=None,
              help="Write current fingerprints as accepted snapshot JSON")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write artifact state JSON to this file")
@click.pass_context
def artifact_state(ctx, artifact_id, snapshot_path, write_snapshot_path, out_path):
    """Compute lifecycle, fingerprints and implementation/evidence state."""
    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    payload = _artifact_state_payload(db, root, artifact_id, snapshot_path)
    db.close()

    if write_snapshot_path:
        snapshot = {
            "schema": "graph-ba.fingerprint-snapshot.v1",
            "artifacts": {
                item["id"]: {
                    "lifecycle": item["lifecycle"],
                    "fingerprints": item["fingerprints"],
                }
                for item in payload["artifacts"]
            },
        }
        write_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        write_snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        payload["snapshot_written"] = str(write_snapshot_path)

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote artifact state JSON: {out_path}")
            if write_snapshot_path:
                print(f"Wrote accepted fingerprint snapshot: {write_snapshot_path}")
            return
    print(text)


def _artifact_state_payload(
    db: sqlite3.Connection,
    root: Path,
    artifact_id: str | None,
    snapshot_path: Path | None,
) -> dict[str, Any]:
    snapshot = _load_fingerprint_snapshot(snapshot_path)
    lifecycle_overrides = _graph_native_lifecycle_map(root)
    change_states = _graph_native_change_state_map(root)
    rows = _artifact_rows(db, artifact_id)
    artifacts = [
        _artifact_state_item(db, row, root, lifecycle_overrides, change_states, snapshot)
        for row in rows
    ]
    return {
        "schema": "graph-ba.artifact-state.v1",
        "snapshot_path": str(snapshot_path) if snapshot_path else "",
        "snapshot_loaded": bool(snapshot),
        "artifacts": artifacts,
    }


def _artifact_rows(db: sqlite3.Connection, artifact_id: str | None) -> list[sqlite3.Row]:
    if artifact_id:
        rows = db.execute(
            "SELECT * FROM artifacts WHERE id = ? ORDER BY id",
            (artifact_id,),
        ).fetchall()
        if not rows:
            raise click.ClickException(f"Artifact not found: {artifact_id}")
        return rows
    return db.execute("SELECT * FROM artifacts ORDER BY id").fetchall()


def _artifact_state_item(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    root: Path,
    lifecycle_overrides: dict[str, str],
    change_states: dict[str, dict[str, str]],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    artifact_id = row["id"]
    lifecycle = _artifact_lifecycle(row, lifecycle_overrides)
    fingerprints = _artifact_fingerprints(db, row)
    active_changes = _active_changes_for_artifact(db, artifact_id, change_states)
    implemented = _artifact_has_implementation(db, row)
    verified = _artifact_has_evidence(db, artifact_id)
    baseline = snapshot.get("artifacts", {}).get(artifact_id, {}) if snapshot else {}
    baseline_fingerprints = baseline.get("fingerprints", {}) if isinstance(baseline, dict) else {}
    stale_reasons = _stale_reasons(fingerprints, baseline_fingerprints)
    return {
        "id": artifact_id,
        "type": row["type"],
        "origin": row["origin"],
        "title": row["title"],
        "source_file": row["source_file"],
        "line_number": row["line_number"],
        "lifecycle": lifecycle,
        "computed": {
            "implemented": implemented,
            "verified": verified,
            "changing": bool(active_changes),
            "stale": bool(stale_reasons),
            "unimplemented": lifecycle in {"accepted", "planned"} and not implemented,
            "unverified": lifecycle in {"accepted", "planned"} and not verified,
        },
        "active_changes": active_changes,
        "fingerprints": fingerprints,
        "baseline": {
            "present": bool(baseline_fingerprints),
            "stale_reasons": stale_reasons,
        },
    }


def _artifact_lifecycle(row: sqlite3.Row, overrides: dict[str, str]) -> str:
    artifact_id = row["id"]
    if artifact_id in overrides:
        return overrides[artifact_id]
    if row["type"] == "CHG":
        return overrides.get(artifact_id, "draft")
    if row["origin"] in {"canonical", "implementation", "evidence"}:
        return "accepted"
    return "draft" if int(row["defined"] or 0) else "unknown"


def _artifact_fingerprints(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, str]:
    artifact_id = row["id"]
    content = _artifact_content(db, row)
    links = _edge_tuples(db, "WHERE source_id = ?", (artifact_id,))
    observed = _edge_tuples(
        db,
        "WHERE (source_id = ? OR target_id = ?) "
        "AND relation_type IN ('IMPLEMENTS', 'RENDERS', 'DEPENDS_ON')",
        (artifact_id, artifact_id),
    )
    evidence = _edge_tuples(
        db,
        "WHERE (source_id = ? OR target_id = ?) "
        "AND relation_type IN ('TEST_EVIDENCE', 'VERIFIES')",
        (artifact_id, artifact_id),
    )
    return {
        "content": _sha256_text(content),
        "links": _sha256_json(links),
        "observed": _sha256_json(observed),
        "evidence": _sha256_json(evidence),
    }


def _artifact_content(db: sqlite3.Connection, row: sqlite3.Row) -> str:
    full_path = _resolve_file(db, row["source_file"]) if row["source_file"] else None
    if not full_path:
        return f"{row['id']}\n{row['type']}\n{row['title']}"
    path = Path(full_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return f"{row['id']}\n{row['type']}\n{row['title']}"
    line_number = int(row["line_number"] or 0)
    if 1 <= line_number <= len(lines) and lines[line_number - 1].lstrip().startswith(":::artifact"):
        collected = []
        for line in lines[line_number - 1:]:
            collected.append(line)
            if line.strip() == ":::":
                break
        return "\n".join(collected)
    if line_number > 0:
        return _artifact_section_text(str(path), line_number)
    return "\n".join(lines)


def _edge_tuples(
    db: sqlite3.Connection,
    where_clause: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT source_id, target_id, relation_type, context "
        f"FROM edges {where_clause} "
        "ORDER BY source_id, target_id, relation_type, context",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _artifact_has_implementation(db: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if row["origin"] == "implementation":
        return True
    artifact_id = row["id"]
    found = db.execute(
        "SELECT 1 FROM edges e JOIN artifacts s ON e.source_id = s.id "
        "WHERE e.target_id = ? "
        "AND e.relation_type IN ('IMPLEMENTS', 'RENDERS') "
        "AND (s.origin = 'implementation' OR s.type IN ("
        "'CRUDL_RESOURCE','CUSTOM_METHOD','INTEGRATION_ACTION',"
        "'INTEGRATION_CONNECTION','INTEGRATION_TRIGGER','REACT_UI'"
        ")) LIMIT 1",
        (artifact_id,),
    ).fetchone()
    return bool(found)


def _artifact_has_evidence(db: sqlite3.Connection, artifact_id: str) -> bool:
    found = db.execute(
        "SELECT 1 FROM edges e JOIN artifacts s ON e.source_id = s.id "
        "WHERE e.target_id = ? "
        "AND e.relation_type IN ('TEST_EVIDENCE', 'VERIFIES') "
        "AND (s.origin = 'evidence' OR s.type IN ('TEST', 'EVD', 'UI')) LIMIT 1",
        (artifact_id,),
    ).fetchone()
    return bool(found)


def _active_changes_for_artifact(
    db: sqlite3.Connection,
    artifact_id: str,
    change_states: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    rows = db.execute(
        "SELECT e.source_id, a.title FROM edges e "
        "JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? AND a.type = 'CHG' "
        "AND e.relation_type IN ('CONTAINS', 'DEPENDS_ON') "
        "ORDER BY e.source_id",
        (artifact_id,),
    ).fetchall()
    active = []
    for row in rows:
        state = change_states.get(row["source_id"], {}).get("state", "draft")
        if state in {"accepted", "archived"}:
            continue
        active.append({
            "id": row["source_id"],
            "title": row["title"] or "",
            "state": state,
            "mode": change_states.get(row["source_id"], {}).get("mode", ""),
        })
    return active


def _stale_reasons(current: dict[str, str], baseline: dict[str, str]) -> list[str]:
    if not baseline:
        return []
    return [
        key
        for key in ("content", "links", "observed", "evidence")
        if baseline.get(key) and baseline.get(key) != current.get(key)
    ]


def _load_fingerprint_snapshot(snapshot_path: Path | None) -> dict[str, Any]:
    if not snapshot_path or not snapshot_path.exists():
        return {}
    try:
        return json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise click.ClickException(f"Cannot read snapshot {snapshot_path}: {exc}") from exc


def _graph_native_lifecycle_map(root: Path) -> dict[str, str]:
    try:
        from graph_ba.config import load_config, normalize_id
        from graph_ba.traceability import _graph_native_artifact_files, _parse_graph_native_attrs

        config = load_config(root)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for filepath in _graph_native_artifact_files(root, config):
        try:
            lines = filepath.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            marker = re.match(r"^\s*:::artifact\s+(.+?)\s*$", line)
            if not marker:
                continue
            attrs = _parse_graph_native_attrs(marker.group(1))
            if attrs.get("id") and attrs.get("state"):
                result[normalize_id(attrs["id"], config)] = attrs["state"]
    result.update({key: value.get("state", "") for key, value in _graph_native_change_state_map(root).items()})
    return {key: value for key, value in result.items() if value}


def _graph_native_change_state_map(root: Path) -> dict[str, dict[str, str]]:
    try:
        from graph_ba.config import load_config, normalize_id
        from graph_ba.traceability import _graph_native_change_files, _read_graph_native_change

        config = load_config(root)
    except Exception:
        return {}
    result: dict[str, dict[str, str]] = {}
    for filepath in _graph_native_change_files(root, config):
        change = _read_graph_native_change(filepath)
        change_id = change.get("id")
        if not isinstance(change_id, str) or not change_id:
            continue
        result[normalize_id(change_id, config)] = {
            "state": str(change.get("state") or "draft"),
            "mode": str(change.get("mode") or ""),
        }
    return result


def _sha256_text(value: str) -> str:
    normalized = "\n".join(line.rstrip() for line in value.splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@cli.group("change")
@click.pass_context
def change_group(ctx):
    """Manage graph-native change requests."""


@change_group.command("create")
@click.argument("change_id")
@click.option("--title", default="", help="Human-readable change title")
@click.option("--state", default="draft", type=click.Choice(["draft", "planned", "accepted", "archived"]))
@click.option("--mode", default="dev", type=click.Choice(["explore", "dev", "review", "release"]))
@click.option("--scope", "scope_items", multiple=True, help="Scoped artifact ID; can be repeated")
@click.pass_context
def change_create(ctx, change_id, title, state, mode, scope_items):
    """Create `.graphba/changes/<change-id>/`."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    path = _change_path(root, change_id)
    if path.exists():
        raise click.ClickException(f"Change already exists: {path}")
    (path / "compiled").mkdir(parents=True)
    (path / "evidence").mkdir()
    (path / "archive").mkdir()
    lines = [
        f"id: {change_id}",
        f"title: {title or change_id}",
        f"state: {state}",
        f"mode: {mode}",
        "scope:",
    ]
    lines.extend(f"  - {item}" for item in scope_items)
    (path / "change.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "source.md").write_text(
        "# Change source\n\n"
        "<!-- Add graph-native :::artifact blocks here. -->\n",
        encoding="utf-8",
    )
    print(f"Created graph-ba change: {path}")


@change_group.command("show")
@click.argument("change_id")
@click.pass_context
def change_show(ctx, change_id):
    """Show change metadata, scope and computed state."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    data = _change_payload(db, root, change_id)
    db.close()
    if _json_out(ctx, data):
        return
    meta = data["change"]
    print(f"{meta['id']} — {meta.get('title') or ''}")
    print(f"state={meta.get('state') or 'draft'} mode={meta.get('mode') or ''}")
    print(f"scope: {len(data['scope'])}")
    for item in data["scope"]:
        flags = item["computed"]
        print(
            f"  {item['id']} [{item['type']}] "
            f"implemented={flags['implemented']} verified={flags['verified']} stale={flags['stale']}"
        )


@change_group.command("accept")
@click.argument("change_id")
@click.option("--snapshot", "snapshot_path", type=click.Path(path_type=Path), default=None,
              help="Project accepted fingerprint snapshot path")
@click.pass_context
def change_accept(ctx, change_id, snapshot_path):
    """Write accepted delta/snapshot for a change and optionally update project snapshot."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    data = _change_payload(db, root, change_id)
    db.close()
    change_dir = _change_path(root, change_id)
    archive_dir = change_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    accepted_delta = {
        "schema": "graph-ba.accepted-delta.v1",
        "change": data["change"],
        "scope": [item["id"] for item in data["scope"]],
    }
    accepted_snapshot = {
        "schema": "graph-ba.fingerprint-snapshot.v1",
        "artifacts": {
            item["id"]: {
                "lifecycle": item["lifecycle"],
                "fingerprints": item["fingerprints"],
            }
            for item in data["scope"]
        },
    }
    (archive_dir / "accepted-delta.json").write_text(
        json.dumps(accepted_delta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (archive_dir / "accepted-snapshot.json").write_text(
        json.dumps(accepted_snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if snapshot_path:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        current = _load_fingerprint_snapshot(snapshot_path)
        artifacts = dict(current.get("artifacts", {}))
        artifacts.update(accepted_snapshot["artifacts"])
        snapshot_path.write_text(
            json.dumps(
                {"schema": "graph-ba.fingerprint-snapshot.v1", "artifacts": artifacts},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    _rewrite_change_state(change_dir / "change.yaml", "accepted")
    print(f"Accepted graph-ba change: {change_id}")


@change_group.command("archive")
@click.argument("change_id")
@click.pass_context
def change_archive(ctx, change_id):
    """Move a change under `.graphba/changes/archive/YYYY-MM-DD-<id>/`."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    src = _change_path(root, change_id)
    if not src.exists():
        raise click.ClickException(f"Change not found: {src}")
    dst = root / ".graphba" / "changes" / "archive" / f"{date.today().isoformat()}-{change_id}"
    if dst.exists():
        raise click.ClickException(f"Archive target already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _rewrite_change_state(src / "change.yaml", "archived")
    shutil.move(str(src), str(dst))
    print(f"Archived graph-ba change: {dst}")


@cli.command("gate")
@click.argument("target_id")
@click.option("--mode", default=None, type=click.Choice(["explore", "dev", "review", "release"]),
              help="Gate strictness; defaults to change.yaml mode or dev")
@click.option("--snapshot", "snapshot_path", type=click.Path(path_type=Path), default=None,
              help="Accepted fingerprint snapshot for stale checks")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write gate JSON to this file")
@click.pass_context
def gate(ctx, target_id, mode, snapshot_path, out_path):
    """Evaluate change/release readiness from graph facts."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    result = _gate_payload(db, root, target_id, mode, snapshot_path)
    db.close()
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote gate JSON: {out_path}")
            if not result["pass"]:
                raise click.ClickException(f"Gate failed: {result['verdict']}")
            return
    print(text)
    if not result["pass"]:
        raise click.ClickException(f"Gate failed: {result['verdict']}")


@cli.command("pack")
@click.argument("target_id")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write pack markdown to this file")
@click.option("--format", "output_format", type=click.Choice(["md", "json"]), default="md")
@click.pass_context
def pack(ctx, target_id, out_path, output_format):
    """Compile an agent pack for a change, screen family, screen or artifact."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    payload = _pack_payload(db, root, target_id)
    db.close()
    if output_format == "json":
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        text = _render_pack_markdown(payload)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text.rstrip() + "\n", encoding="utf-8")
        if not ctx.obj.get("json"):
            print(f"Wrote graph-ba pack: {out_path}")
            return
    print(text)


def _change_path(root: Path, change_id: str) -> Path:
    return root / ".graphba" / "changes" / change_id


def _change_payload(db: sqlite3.Connection, root: Path, change_id: str) -> dict[str, Any]:
    change_states = _graph_native_change_state_map(root)
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Change not found in graph: {change_id}")
    scope_rows = _scope_rows(db, change_id)
    state_items = [
        _artifact_state_item(
            db,
            item,
            root,
            _graph_native_lifecycle_map(root),
            change_states,
            {},
        )
        for item in scope_rows
    ]
    return {
        "schema": "graph-ba.change.v1",
        "change": {
            "id": change_id,
            "title": row["title"],
            "state": change_states.get(change_id, {}).get("state", "draft"),
            "mode": change_states.get(change_id, {}).get("mode", ""),
        },
        "scope": state_items,
    }


def _scope_rows(db: sqlite3.Connection, change_id: str) -> list[sqlite3.Row]:
    ids = _scope_ids(db, change_id)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        return db.execute(
            f"SELECT * FROM artifacts WHERE id IN ({placeholders}) ORDER BY type, id",
            tuple(sorted(ids)),
        ).fetchall()
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    return [row] if row else []


def _scope_ids(db: sqlite3.Connection, root_id: str) -> set[str]:
    scope_relations = _semantic_scope_relations()
    incoming_scope_relations = _incoming_scope_relations()
    result: set[str] = set()
    seen = {root_id}
    queue = [root_id]
    while queue:
        current = queue.pop(0)
        outgoing_rows = db.execute(
            "SELECT target_id FROM edges WHERE source_id = ? "
            f"AND relation_type IN ({','.join('?' for _ in scope_relations)}) "
            "ORDER BY target_id",
            (current, *sorted(scope_relations)),
        ).fetchall()
        incoming_rows = db.execute(
            "SELECT source_id AS target_id FROM edges WHERE target_id = ? "
            f"AND relation_type IN ({','.join('?' for _ in incoming_scope_relations)}) "
            "ORDER BY source_id",
            (current, *sorted(incoming_scope_relations)),
        ).fetchall()
        for row in outgoing_rows:
            target_id = row["target_id"]
            if target_id in seen:
                continue
            seen.add(target_id)
            result.add(target_id)
            queue.append(target_id)
        for row in incoming_rows:
            target_id = row["target_id"]
            if target_id in seen:
                continue
            seen.add(target_id)
            result.add(target_id)
    return result


def _semantic_scope_relations() -> set[str]:
    return {
        "CONTAINS",
        "DEPENDS_ON",
        "TRACES_TO",
        "NORMALIZES",
        "IMPLEMENTS",
        "VERIFIES",
        "RENDERS",
        "TEST_EVIDENCE",
        "CODE_TRACE",
        "UI_TRACE",
    }


def _incoming_scope_relations() -> set[str]:
    return {
        "CODE_TRACE",
        "IMPLEMENTS",
        "RENDERS",
        "TEST_EVIDENCE",
        "UI_TRACE",
        "VERIFIES",
    }


def _direct_scope_rows(db: sqlite3.Connection, change_id: str) -> list[sqlite3.Row]:
    rows = db.execute(
        "SELECT DISTINCT a.* FROM edges e JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? AND e.relation_type IN ('CONTAINS', 'DEPENDS_ON') "
        "ORDER BY a.id",
        (change_id,),
    ).fetchall()
    if rows:
        return rows
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (change_id,)).fetchone()
    return [row] if row else []


def _gate_payload(
    db: sqlite3.Connection,
    root: Path,
    target_id: str,
    mode: str | None,
    snapshot_path: Path | None,
) -> dict[str, Any]:
    change_states = _graph_native_change_state_map(root)
    selected_mode = mode or change_states.get(target_id, {}).get("mode") or "dev"
    snapshot = _load_fingerprint_snapshot(snapshot_path)
    rows = _scope_rows(db, target_id)
    states = [
        _artifact_state_item(
            db,
            row,
            root,
            _graph_native_lifecycle_map(root),
            change_states,
            snapshot,
        )
        for row in rows
    ]
    findings = _gate_findings(states, selected_mode, bool(snapshot))
    fail_count = sum(1 for item in findings if item["severity"] == "fail")
    warn_count = sum(1 for item in findings if item["severity"] == "warn")
    return {
        "schema": "graph-ba.gate.v1",
        "target": target_id,
        "mode": selected_mode,
        "pass": fail_count == 0,
        "verdict": "PASS" if fail_count == 0 else "FAIL",
        "summary": {"fail": fail_count, "warn": warn_count, "scope": len(states)},
        "findings": findings,
        "scope": [
            {
                "id": item["id"],
                "type": item["type"],
                "lifecycle": item["lifecycle"],
                "computed": item["computed"],
            }
            for item in states
        ],
    }


def _gate_findings(states: list[dict[str, Any]], mode: str, snapshot_loaded: bool) -> list[dict[str, Any]]:
    if mode == "explore":
        return []
    findings: list[dict[str, Any]] = []
    strict = mode in {"review", "release"}
    release = mode == "release"
    contract_types = {"AC", "RULE", "DER", "STATE", "EVT", "ENT", "MTH"}
    for item in states:
        computed = item["computed"]
        if item["type"] in contract_types and not computed["implemented"]:
            findings.append(_gate_finding(item, "unimplemented", "fail" if strict else "warn"))
        if item["type"] == "AC" and not computed["verified"]:
            findings.append(_gate_finding(item, "unverified", "fail" if strict else "warn"))
        if release and computed["stale"]:
            findings.append(_gate_finding(item, "stale", "fail"))
    if release and not snapshot_loaded:
        findings.append({
            "artifact": "",
            "type": "",
            "code": "missing_snapshot",
            "severity": "fail",
            "message": "release gate requires accepted fingerprint snapshot",
        })
    return findings


def _gate_finding(item: dict[str, Any], code: str, severity: str) -> dict[str, str]:
    return {
        "artifact": item["id"],
        "type": item["type"],
        "code": code,
        "severity": severity,
        "message": f"{item['id']} {code.replace('_', ' ')}",
    }


def _pack_payload(db: sqlite3.Connection, root: Path, target_id: str) -> dict[str, Any]:
    row = db.execute("SELECT * FROM artifacts WHERE id = ?", (target_id,)).fetchone()
    if not row:
        raise click.ClickException(f"Artifact not found: {target_id}")
    if row["type"] == "CHG":
        rows = [row, *_scope_rows(db, target_id)]
    else:
        related_ids = _scope_ids(db, target_id)
        related_ids.add(target_id)
        if related_ids:
            semantic_relations = _semantic_scope_relations()
            placeholders = ",".join("?" for _ in related_ids)
            relation_placeholders = ",".join("?" for _ in semantic_relations)
            adjacent = db.execute(
                "SELECT source_id, target_id FROM edges "
                f"WHERE relation_type IN ({relation_placeholders}) "
                f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))",
                (*sorted(semantic_relations), *tuple(sorted(related_ids)), *tuple(sorted(related_ids))),
            ).fetchall()
            for edge in adjacent:
                related_ids.add(edge["source_id"])
                related_ids.add(edge["target_id"])
        placeholders = ",".join("?" for _ in related_ids)
        rows = db.execute(
            f"SELECT * FROM artifacts WHERE id IN ({placeholders}) ORDER BY type, id",
            tuple(sorted(related_ids)),
        ).fetchall()
    artifacts = [
        {
            "id": item["id"],
            "type": item["type"],
            "title": item["title"],
            "source_file": item["source_file"],
            "line_number": item["line_number"],
            "content": _artifact_content(db, item),
        }
        for item in rows
    ]
    ids = [item["id"] for item in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        semantic_relations = _semantic_scope_relations()
        relation_placeholders = ",".join("?" for _ in semantic_relations)
        edge_rows = db.execute(
            "SELECT source_id, target_id, relation_type, context, source_file, line_number "
            f"FROM edges WHERE relation_type IN ({relation_placeholders}) "
            f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders})) "
            "ORDER BY source_id, target_id, relation_type",
            (*sorted(semantic_relations), *ids, *ids),
        ).fetchall()
    else:
        edge_rows = []
    return {
        "schema": "graph-ba.pack.v1",
        "target": target_id,
        "artifacts": artifacts,
        "edges": [dict(edge) for edge in edge_rows],
    }


def _render_pack_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# graph-ba pack: {payload['target']}", ""]
    lines.append("## Artifacts")
    for item in payload["artifacts"]:
        content = _pack_markdown_content(str(item["type"]), str(item["content"]))
        lines.extend([
            "",
            f"### {item['id']} [{item['type']}]",
            f"Title: {item['title']}",
            f"Source: {item['source_file']}:{item['line_number']}",
            "",
            "```",
            content,
            "```",
        ])
    lines.extend(["", "## Edges", ""])
    for edge in payload["edges"]:
        lines.append(
            f"- `{edge['source_id']}` --{edge['relation_type']}--> `{edge['target_id']}`"
        )
    return "\n".join(lines)


def _pack_markdown_content(artifact_type: str, content: str) -> str:
    content = content.strip()
    limit = 1200 if artifact_type in {"TEST", "CODE", "REACT_COMPONENT"} else 3500
    if len(content) <= limit:
        return content
    return (
        content[:limit].rstrip()
        + f"\n\n[graph-ba pack truncated {len(content) - limit} chars; use source file for full content]"
    )


def _rewrite_change_state(path: Path, state: str) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("state:"):
            lines[index] = f"state: {state}"
            replaced = True
            break
    if not replaced:
        lines.append(f"state: {state}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Code references CLI ──────────────────────────────────────────

@cli.command("code-refs")
@click.option("--by-artifact", is_flag=True, help="Group by artifact instead of by file")
@click.option("--type", "art_type", default=None, help="Filter to artifact type (e.g. F, BR_REQ)")
@click.pass_context
def code_refs(ctx, by_artifact, art_type):
    """Show code-to-artifact traceability links."""
    db = _conn(ctx)
    _require_graph(ctx, db)

    query = (
        "SELECT e.source_id as code_node, e.target_id as artifact_id, "
        "a.type as art_type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e "
        "LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id LIKE 'CODE:%'"
    )
    params: list = []
    if art_type:
        query += " AND a.type = ?"
        params.append(art_type)
    query += " ORDER BY e.source_id, e.line_number"

    rows = db.execute(query, params).fetchall()
    db.close()

    if not rows:
        print("No code references found. Add @trace comments to source files "
              "and configure [code] in graph-ba.toml.")
        return

    if _json_out(ctx, {"code_refs": [dict(r) for r in rows]}):
        return

    if by_artifact:
        groups: dict = {}
        for r in rows:
            groups.setdefault(r["artifact_id"], []).append(r)

        print(f"Code references by artifact ({len(groups)} artifacts):\n")
        for aid in sorted(groups):
            refs = groups[aid]
            art_type_str = refs[0]["art_type"] or "?"
            title = refs[0]["title"] or ""
            print(f"  [{art_type_str}] {aid} — {title[:50]}")
            for r in refs:
                code_path = r["code_node"].removeprefix("CODE:")
                print(f"    {code_path}:{r['line_number']}")
            print()
    else:
        groups = {}
        for r in rows:
            groups.setdefault(r["code_node"], []).append(r)

        print(f"Code references by file ({len(groups)} files):\n")
        for code_node in sorted(groups):
            code_path = code_node.removeprefix("CODE:")
            refs = groups[code_node]
            print(f"  {code_path}")
            for r in refs:
                print(f"    L{r['line_number']:>4d}  → [{r['art_type'] or '?'}] "
                      f"{r['artifact_id']} — {(r['title'] or '')[:40]}")
            print()


# ── NetworkX loader (for path/impact commands) ────────────────────
@cli.command()
@click.argument("node_id_or_file")
@click.option("--lines", default=0, type=int, help="Max lines per artifact in --semantic mode (0 = no limit)")
@click.option("--nums", is_flag=True, help="Enable numeric conflict detection")
@click.option("--semantic", is_flag=True, help="Full text of each linked artifact for semantic validation")
@click.option("--types", default=None, help="Comma-separated artifact types to include in --semantic (e.g. ST,BR_REQ,BR_RULE,BP)")
@click.pass_context
def review(ctx, node_id_or_file, lines, nums, semantic, types):
    """Full review: validate + context in one call."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        config = load_config(root)
    except FileNotFoundError:
        config = None
    data = run_review(db, root, config, node_id_or_file,
                      semantic=semantic, lines=lines, nums=nums, types=types)
    db.close()

    if _json_out(ctx, data):
        return
    if "error" in data:
        print(data["error"])
        return

    artifact = data["artifact"]
    print(f"{'═' * 70}")
    print(f"  REVIEW: {artifact['id']} — {artifact['title']}")
    print(f"  Type: {artifact['type']}  |  File: {artifact['source_file']}:{artifact['line_number']}")
    print(f"{'═' * 70}")

    issues = data["issues"]
    if issues:
        print(f"\n┌─ Issues ({len(issues)}) ─────────────────────────────────")
        for issue in issues:
            print(f"│ [{issue['severity']:6s}] {issue['message']}")
        print(f"└{'─' * 55}")
    else:
        print("\n✓ No issues found")

    if data["clusters"]:
        print(f"\nClusters: {', '.join(data['clusters'])}")

    if semantic:
        linked = data["linked_artifacts"]
        print(f"\n{'═' * 70}")
        print(f"  LINKED ARTIFACTS ({len(linked)})")
        print(f"{'═' * 70}")
        for art in linked:
            if not art.get("defined", False):
                print(f"\n  ▸ {art['id']} — definition not found in DB")
                continue
            section = art.get("section")
            if section:
                print(f"\n{'─' * 70}")
                print(f"  {art['id']}: {art.get('title') or ''}")
                print(f"  File: {art.get('source_file')}:{art.get('line_number')}")
                print(f"{'─' * 70}")
                print(section)
        code_in = [r for r in data["incoming"] if r["ref_id"].startswith("CODE:")]
        if code_in:
            print(f"\n── Code References ({len(code_in)}) ──")
            for r in code_in:
                code_path = r["ref_id"].removeprefix("CODE:")
                print(f"  {code_path}:{r['line_number']}")
                if r.get("context"):
                    print(f"    {r['context'][:70]}")
        return

    print(f"\n── Outgoing references ({len(data['outgoing'])}) ──")
    for r in data["outgoing"]:
        print(f"  → [{r.get('type') or '?'}] {r['ref_id']} — {(r.get('title') or '')[:55]}")
        if r.get("source_file"):
            print(f"    Ref in: {r['source_file']}:{r.get('line_number') or 0}")
        if r.get("context"):
            print(f"    Context: {r['context'][:70]}")

    ba_in = [r for r in data["incoming"] if not r["ref_id"].startswith("CODE:")]
    code_in = [r for r in data["incoming"] if r["ref_id"].startswith("CODE:")]
    if ba_in:
        print(f"\n── Incoming references ({len(ba_in)}) ──")
        for r in ba_in:
            print(f"  ← [{r.get('type') or '?'}] {r['ref_id']} — {(r.get('title') or '')[:55]}")
            if r.get("source_file"):
                print(f"    Ref in: {r['source_file']}:{r.get('line_number') or 0}")
            if r.get("context"):
                print(f"    Context: {r['context'][:70]}")
    if code_in:
        print(f"\n── Code References ({len(code_in)}) ──")
        for r in code_in:
            code_path = r["ref_id"].removeprefix("CODE:")
            print(f"  {code_path}:{r['line_number']}")
            if r.get("context"):
                print(f"    {r['context'][:70]}")


def _emit_validate(ctx, artifact_id: str, checks: list):
    """Print validate result (human or JSON) and exit 1 on FAIL."""
    verdict = "FAIL" if any(c["status"] == "fail" for c in checks) else "PASS"
    if not _json_out(ctx, {"id": artifact_id, "verdict": verdict, "checks": checks}):
        symbols = {"pass": "✓", "fail": "✗", "warn": "⚠"}
        print(f"Validate: {artifact_id}")
        for c in checks:
            print(f"  {symbols[c['status']]} {c['name']} — {c['detail']}")
        print(f"\nVERDICT: {verdict}")
    if verdict == "FAIL":
        ctx.exit(1)


@cli.command()
@click.argument("artifact_id")
@click.pass_context
def validate(ctx, artifact_id):
    """Deterministic quality gate for one artifact (exit 0=PASS, 1=FAIL)."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        config = load_config(root)
    except FileNotFoundError:
        config = None
    data = run_validate(db, root, config, artifact_id)
    db.close()

    if not _json_out(ctx, data):
        symbols = {"pass": "✓", "fail": "✗", "warn": "⚠"}
        print(f"Validate: {artifact_id}")
        for c in data["checks"]:
            print(f"  {symbols[c['status']]} {c['name']} — {c['detail']}")
        print(f"\nVERDICT: {data['verdict']}")
    if data["verdict"] == "FAIL":
        ctx.exit(1)


# ── Anomaly detection ─────────────────────────────────────────────

@cli.command()
@click.option("--min-component", default=2, help="Min size of islands to report")
@click.pass_context
def anomalies(ctx, min_component):
    """Detect graph anomalies: islands, cycles, weak nodes, broken chains."""
    db = _conn(ctx)
    _require_graph(ctx, db)
    data = run_anomalies(db, min_component)
    db.close()

    if _json_out(ctx, data):
        return

    issues = [(i["type"], i["message"]) for i in data["issues"]]
    if not issues:
        print("No anomalies found.")
        return

    print(f"Graph anomalies ({data['nodes']} nodes, {data['edges']} edges):")
    print()
    current_cat = None
    for cat, msg in issues:
        if cat != current_cat:
            current_cat = cat
            print(f"── {cat} ──")
        print(f"  {msg}")
    print()
    total = sum(1 for _, msg in issues if not msg.startswith("  "))
    print(f"Total: {total} anomaly types detected")


# ── Global audit ──────────────────────────────────────────────────
@cli.command()
@click.option("--top", default=30, help="Max review candidates to return")
@click.option("--baseline", "baseline_path",
              type=click.Path(exists=True, path_type=Path), default=None,
              help="Compare issues against a baseline; exit 1 only on NEW issues")
@click.option("--write-baseline", "write_baseline_path",
              type=click.Path(path_type=Path), default=None,
              help="Write current issue fingerprints to a baseline file")
@click.pass_context
def audit(ctx, top, baseline_path, write_baseline_path):
    """Global audit: anomalies + coverage gaps + prioritized review list."""
    from graph_ba.config import load_config

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()
    try:
        config = load_config(root)
    except Exception:
        config = None
    result = run_audit(db, root, config, top)
    db.close()
    issues = result["issues"]

    if write_baseline_path:
        fps = sorted({fp for iss in issues for fp in _issue_fingerprints(iss)})
        write_baseline_path.parent.mkdir(parents=True, exist_ok=True)
        write_baseline_path.write_text(
            json.dumps({"version": 1, "fingerprints": fps},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print(f"Baseline written: {write_baseline_path} ({len(fps)} fingerprints)")
        return

    baseline_cmp = None
    if baseline_path:
        try:
            baseline_data = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
            baseline_set = set(baseline_data.get("fingerprints", []))
        except (OSError, json.JSONDecodeError) as e:
            raise click.ClickException(f"Cannot read baseline {baseline_path}: {e}")
        current = {fp for iss in issues for fp in _issue_fingerprints(iss)}
        baseline_cmp = {
            "new": sorted(current - baseline_set),
            "known": sorted(current & baseline_set),
            "resolved": sorted(baseline_set - current),
        }
        result["new"] = baseline_cmp["new"]
        result["known"] = baseline_cmp["known"]
        result["resolved"] = baseline_cmp["resolved"]

    if _json_out(ctx, result):
        if baseline_cmp and baseline_cmp["new"]:
            ctx.exit(1)
        return

    if baseline_cmp:
        new, known, resolved = (baseline_cmp["new"], baseline_cmp["known"],
                                baseline_cmp["resolved"])
        print(f"Global Audit ({result['summary']['artifacts']} nodes, "
              f"{result['summary']['edges']} edges) vs baseline {baseline_path}")
        print(f"Baseline: {len(new)} new / {len(known)} known / "
              f"{len(resolved)} resolved")
        if new:
            print(f"\n── New issues ({len(new)}) ──")
            for fp in new:
                print(f"  {fp}")
            ctx.exit(1)
        return

    print(f"Global Audit ({result['summary']['artifacts']} nodes, {result['summary']['edges']} edges)")
    print()
    if not issues:
        print("No issues found.")
        return

    by_type = {}
    for iss in issues:
        by_type.setdefault(iss["type"], []).append(iss)
    print(f"── Issues ({len(issues)}) ──")
    for cat in ["CYCLE", "DANGLING", "COVERAGE_GAP", "MISSING_CROSS_LAYER",
                "MISSING_BIDIR", "BRIDGE", "BOTTLENECK"]:
        items = by_type.get(cat, [])
        if not items:
            continue
        print(f"\n  {cat} ({len(items)}):")
        for iss in items[:10]:
            if cat == "CYCLE":
                print(f"    {' → '.join(iss['ids'])}")
            elif cat == "DANGLING":
                print(f"    {iss['id']} ← {', '.join(iss['referenced_by'][:5])}")
            elif cat == "COVERAGE_GAP":
                print(f"    {iss['source']}→{iss['target']}: {iss['pct']}% "
                      f"(missing: {', '.join(iss['missing'][:10])})")
            elif cat == "MISSING_CROSS_LAYER":
                print(f"    {iss['id']} → needs {iss['expected']} ({iss['label']})")
            elif cat == "MISSING_BIDIR":
                print(f"    {iss['id']} → {iss['target']} (one-way)")
            elif cat == "BRIDGE":
                print(f"    {iss['ids'][0]} — {iss['ids'][1]}")
            elif cat == "BOTTLENECK":
                print(f"    {iss['id']} degree={iss['degree']}")

    print()
    print(f"── Review Candidates ({len(result['candidates'])}) ──")
    for c in result["candidates"]:
        prio = "HIGH" if c["priority"] == "high" else "    "
        reasons = ", ".join(c["reasons"])
        print(f"  {prio}  {c['id']:12s} [{c['type']:4s}]  {reasons}")


# ── Lint command ──────────────────────────────────────────────────

_TODO_DEFAULT = re.compile(
    r'(?:TODO|TBD|FIXME|\?\?\?)', re.IGNORECASE
)
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)')
_GLOSSARY_ROW_RE = re.compile(
    r'^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|'
)
_CODE_FENCE_RE = re.compile(r'^```')

@cli.command()
@click.argument("node_id", required=False, default=None)
@click.option("--quick", is_flag=True, help="Skip git-based checks (stale)")
@click.pass_context
def lint(ctx, node_id, quick):
    """Content quality lint: TODO markers, empty sections, terminology, staleness, code coverage."""
    from graph_ba.config import load_config, LintConfig

    db = _conn(ctx)
    _require_graph(ctx, db)
    root = Path(ctx.obj.get("root", ".")).resolve()

    try:
        config = load_config(root)
    except FileNotFoundError:
        config = None

    findings = do_lint(db, root, config, node_id, quick)
    db.close()

    # Count by severity
    counts = {"ERR": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    # JSON output
    if _json_out(ctx, {
        "summary": {
            "total": len(findings),
            "errors": counts["ERR"],
            "warnings": counts["WARN"],
            "info": counts["INFO"],
        },
        "findings": findings,
    }):
        if counts["ERR"] > 0:
            sys.exit(1)
        return

    # Human-readable output
    scope = f" ({node_id})" if node_id else ""
    print(f"BA Lint{scope}")
    print(f"{'═' * 50}")

    if not findings:
        print("\n✓ No issues found")
        return

    # Group by category
    by_cat: dict = {}
    for f in findings:
        by_cat.setdefault(f["category"], []).append(f)

    cat_order = ["TODO_TBD", "EMPTY_SECTION", "TERMINOLOGY", "STALE", "CODE_COVERAGE"]
    cat_labels = {
        "TODO_TBD": "Incompleteness markers",
        "EMPTY_SECTION": "Empty sections",
        "TERMINOLOGY": "Terminology vs glossary",
        "STALE": "Stale artifacts",
        "CODE_COVERAGE": "Code coverage",
    }

    for cat in cat_order:
        items = by_cat.get(cat, [])
        if not items:
            continue
        label = cat_labels.get(cat, cat)
        print(f"\n── {label} ({len(items)}) ──")
        for f in items:
            loc = f["file"]
            if f["line"]:
                loc += f":{f['line']}"
            sev = f["severity"]
            print(f"  [{sev:4s}]  {f['artifact_id']:12s}  {loc}")
            print(f"          {f['message']}")

    # Summary
    print(f"\n{'─' * 50}")
    parts = []
    if counts["ERR"]:
        parts.append(f"{counts['ERR']} ERR")
    if counts["WARN"]:
        parts.append(f"{counts['WARN']} WARN")
    if counts["INFO"]:
        parts.append(f"{counts['INFO']} INFO")
    print(f"Lint: {', '.join(parts)}  (total {len(findings)})")

    if counts["ERR"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    cli()

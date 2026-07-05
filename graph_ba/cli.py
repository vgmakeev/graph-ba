"""Click command line interface for Graph BA."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

import click

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
@click.option("--db", type=click.Path(path_type=Path), default=None,
              help=f"Path to SQLite DB (default: reports/graph.db)")
@click.option("--root", type=click.Path(exists=True, path_type=Path),
              default=".", help="Project root directory")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-auto-import", is_flag=True,
              help="Do not rebuild the graph automatically when it is empty or stale")
@click.pass_context
def cli(ctx, db, root, json_output, no_auto_import):
    """Graph BA — query the artifact traceability graph."""
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
# graph-ba.toml — project configuration for Graph BA
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

# ── Artifact types ──
# Each type needs:
#   ref = regex to find references in text (group 1 = full ID)
#   classify = regex to classify an ID string (used with fullmatch)
#   label = human-readable name
#   restrict_to = optional list of files/dirs where this pattern is allowed

[types.REQ]
label = "Requirements"
ref = '(?<![A-Za-z])(REQ-\\d{2,4})(?!\\d)'
classify = 'REQ-\\d{2,4}'

[types.FEAT]
label = "Features"
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
        "SELECT a.id, a.type, a.title, a.source_file "
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
        "SELECT source_id, target_id, context FROM edges_fts "
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
            [(r["id"], r["type"], r["title"][:60], r["source_file"]) for r in rows],
            ["ID", "Type", "Title", "File"]
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
            [(r["source_id"], r["target_id"], r["context"][:60]) for r in e_rows],
            ["From", "To", "Context"]
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
        "SELECT e.target_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? ORDER BY a.type, e.target_id",
        (node_id,)
    ).fetchall()

    # In-edges
    inc = db.execute(
        "SELECT e.source_id, a.type, a.title, e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? ORDER BY a.type, e.source_id",
        (node_id,)
    ).fetchall()

    if _json_out(ctx, {
        "id": row["id"], "type": row["type"], "title": row["title"],
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
    print(f"File:    {row['source_file']}:{row['line_number']}")
    print(f"Defined: {'yes' if row['defined'] else 'NO (dangling)'}")
    print(f"Title:   {row['title']}")

    if clusters:
        print(f"Clusters: {', '.join(r['cluster_name'] for r in clusters)}")

    print(f"\n→ Outgoing ({len(out)}):")
    if out:
        print(fmt_table(
            [(r["target_id"], r["type"] or "?",
              f"{r['source_file']}:{r['line_number']}" if r["line_number"] else "",
              r["title"][:40] if r["title"] else "") for r in out],
            ["ID", "Type", "Ref location", "Title"]
        ))

    print(f"\n← Incoming ({len(inc)}):")
    if inc:
        print(fmt_table(
            [(r["source_id"], r["type"] or "?",
              f"{r['source_file']}:{r['line_number']}" if r["line_number"] else "",
              r["title"][:40] if r["title"] else "") for r in inc],
            ["ID", "Type", "Ref location", "Title"]
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
        "a.type as art_type, a.title, e.source_file, e.line_number, e.context "
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

"""Click command line interface for graph-ba."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import click

from graph_ba import __version__
from graph_ba.change_workflow import (
    ChangeWorkflowError,
    ChangeWorkflowService,
    approval_status,
    create_change_branch,
    init_change,
    proposal_check,
)
from graph_ba.db import _fts_query, _load_nx, do_import, get_db, graph_is_stale

from .gate_analysis import _change_payload, _pack_payload
from .gates import (
    _gate_payload,
    _graph_slice_payload,
    delivery_gate_payload,
)
from .rendering import (
    _render_change_state_yaml,
    _render_evidence_plan_markdown,
    _render_gaps_markdown,
    _render_pack_markdown,
    _render_projection_markdown,
    _render_worklist_markdown,
)
from .rendering import fmt_table


def _is_meta_node(node_id: str) -> bool:
    """Check if node is a meta-node (FILE:, CODE:, TEST: or UI:) rather than a BA artifact."""
    return (
        node_id.startswith("FILE:")
        or node_id.startswith("CODE:")
        or node_id.startswith("TEST:")
        or node_id.startswith("UI:")
    )


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


@click.group()
@click.version_option(__version__, prog_name="graph-ba")
@click.option(
    "--db",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to SQLite DB (default: reports/graph.db)",
)
@click.option(
    "--root",
    type=click.Path(exists=True, path_type=Path),
    default=".",
    help="Project root directory",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--no-auto-import",
    is_flag=True,
    help="Do not rebuild the graph automatically when it is empty or stale",
)
@click.pass_context
def cli(ctx, db, root, json_output, no_auto_import):
    """graph-ba — query the artifact traceability graph."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db
    ctx.obj["root"] = str(Path(root).resolve())
    ctx.obj["json"] = json_output
    ctx.obj["no_auto_import"] = no_auto_import


def _conn(ctx) -> sqlite3.Connection:
    db_path = ctx.obj.get("db_path")
    if db_path is None:
        root = Path(ctx.obj.get("root", ".")).resolve()
        db_path = root / "reports" / "graph.db"
    return get_db(db_path)


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
                f"Graph is empty (db: {db_path}). Run `graph-ba import` first."
            )
        return

    row = db.execute("SELECT value FROM meta WHERE key = 'import_time'").fetchone()
    if not row:
        if auto and _auto_import(ctx, db, "predates staleness tracking"):
            return
        click.echo(
            "warning: DB predates staleness tracking — re-run `graph-ba import` to enable it",
            err=True,
        )
        return
    import_time = float(row["value"])

    try:
        from graph_ba.config import load_config

        root = Path(ctx.obj.get("root", ".")).resolve()
        config = load_config(root)
        if graph_is_stale(db, root, config):
            if auto and _auto_import(ctx, db, "is stale"):
                return
            click.echo(
                "warning: graph is stale — sources modified "
                "after last import; run `graph-ba import`",
                err=True,
            )
            return
    except Exception:
        pass


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
        (fq, limit),
    ).fetchall()

    # Search clusters
    cl_rows = db.execute(
        "SELECT DISTINCT cluster_name FROM clusters_fts WHERE clusters_fts MATCH ? LIMIT ?",
        (fq, limit),
    ).fetchall()

    # Search edge contexts
    e_rows = db.execute(
        "SELECT source_id, target_id, relation_type, context FROM edges_fts "
        "WHERE edges_fts MATCH ? LIMIT ?",
        (fq, limit),
    ).fetchall()
    db.close()

    if _json_out(
        ctx,
        {
            "artifacts": [dict(r) for r in rows],
            "clusters": [dict(r) for r in cl_rows],
            "edges": [dict(r) for r in e_rows],
        },
    ):
        return

    if rows:
        print(f"── Artifacts ({len(rows)}) ──")
        print(
            fmt_table(
                [
                    (r["id"], r["type"], r["origin"], r["title"][:60], r["source_file"])
                    for r in rows
                ],
                ["ID", "Type", "Origin", "Title", "File"],
            )
        )
    else:
        print("Artifacts: not found")

    if cl_rows:
        print(f"\n── Clusters ({len(cl_rows)}) ──")
        for r in cl_rows:
            print(f"  • {r['cluster_name']}")

    if e_rows:
        print(f"\n── Edges ({len(e_rows)}) ──")
        print(
            fmt_table(
                [
                    (
                        r["source_id"],
                        r["target_id"],
                        r["relation_type"],
                        r["context"][:60],
                    )
                    for r in e_rows
                ],
                ["From", "To", "Relation", "Context"],
            )
        )


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
            "SELECT * FROM artifacts WHERE id LIKE ? LIMIT 5", (f"%{node_id}%",)
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
        "SELECT cluster_name FROM semantic_clusters WHERE artifact_id = ?", (node_id,)
    ).fetchall()

    # Out-edges
    out = db.execute(
        "SELECT e.target_id, a.type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.target_id = a.id "
        "WHERE e.source_id = ? ORDER BY a.type, e.target_id",
        (node_id,),
    ).fetchall()

    # In-edges
    inc = db.execute(
        "SELECT e.source_id, a.type, a.title, e.relation_type, "
        "e.source_file, e.line_number, e.context "
        "FROM edges e LEFT JOIN artifacts a ON e.source_id = a.id "
        "WHERE e.target_id = ? ORDER BY a.type, e.source_id",
        (node_id,),
    ).fetchall()

    if _json_out(
        ctx,
        {
            "id": row["id"],
            "type": row["type"],
            "origin": row["origin"],
            "title": row["title"],
            "source_file": row["source_file"],
            "line_number": row["line_number"],
            "defined": bool(row["defined"]),
            "clusters": [r["cluster_name"] for r in clusters],
            "outgoing": [dict(r) for r in out],
            "incoming": [dict(r) for r in inc],
        },
    ):
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
        print(
            fmt_table(
                [
                    (
                        r["target_id"],
                        r["type"] or "?",
                        r["relation_type"],
                        f"{r['source_file']}:{r['line_number']}"
                        if r["line_number"]
                        else "",
                        r["title"][:40] if r["title"] else "",
                    )
                    for r in out
                ],
                ["ID", "Type", "Relation", "Ref location", "Title"],
            )
        )

    print(f"\n← Incoming ({len(inc)}):")
    if inc:
        print(
            fmt_table(
                [
                    (
                        r["source_id"],
                        r["type"] or "?",
                        r["relation_type"],
                        f"{r['source_file']}:{r['line_number']}"
                        if r["line_number"]
                        else "",
                        r["title"][:40] if r["title"] else "",
                    )
                    for r in inc
                ],
                ["ID", "Type", "Relation", "Ref location", "Title"],
            )
        )
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
            print(f"Shortest path ({label}, {len(p) - 1} steps):")
            for i, nid in enumerate(p):
                data = G.nodes.get(nid, {})
                arrow = "  →  " if i < len(p) - 1 else ""
                print(
                    f"  [{data.get('type', '?')}] {nid} — {data.get('title', '')[:50]}{arrow}"
                )
            return
        except nx.NetworkXNoPath:
            continue

    print(f"No path between {from_id} and {to_id}")


@cli.command("matrix")
@click.option(
    "--source-type",
    default=None,
    help="Comma-separated source artifact types, e.g. TEST,CODE,UIC",
)
@click.option(
    "--target-type",
    default=None,
    help="Comma-separated target artifact types, e.g. AC,RULE",
)
@click.option(
    "--relation",
    "relation_filter",
    default=None,
    help="Comma-separated relation types, e.g. TEST_EVIDENCE,IMPLEMENTS",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Write sparse matrix JSON to this file",
)
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


@cli.group("change")
@click.pass_context
def change_group(ctx):
    """Manage graph-native change requests."""


@change_group.command("create")
@click.argument("change_id")
@click.option("--title", default="", help="Human-readable change title")
@click.option(
    "--state",
    default="draft",
    type=click.Choice(["draft", "planned", "accepted", "archived"]),
)
@click.option(
    "--mode", default="dev", type=click.Choice(["explore", "dev", "review", "release"])
)
@click.option(
    "--scope", "scope_items", multiple=True, help="Scoped artifact ID; can be repeated"
)
@click.pass_context
def change_create(ctx, change_id, title, state, mode, scope_items):
    """Create `.graphba/changes/<change-id>/`."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    path = _create_change_dir(root, change_id, title, state, mode, scope_items)
    print(f"Created graph-ba change: {path}")


@change_group.command("init")
@click.argument("change_id")
@click.option("--title", default="", help="Human-readable change title")
@click.option("--intent", default="", help="Why this contract change is needed")
@click.option(
    "--source",
    "source_items",
    multiple=True,
    help="Source artifact ID; can be repeated",
)
@click.option(
    "--base-ref", default=None, help="Git ref used as the accepted contract base"
)
@click.option(
    "--scope", "scope_items", multiple=True, help="Scoped artifact ID; can be repeated"
)
@click.option(
    "--branch/--no-branch",
    "create_branch",
    default=True,
    help="Create change/<change-id> from the current clean branch",
)
@click.pass_context
def change_init(
    ctx, change_id, title, intent, source_items, base_ref, scope_items, create_branch
):
    """Create one Git-native change manifest."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    if create_branch:
        try:
            binding = create_change_branch(root, change_id, base_ref=base_ref)
        except ChangeWorkflowError as exc:
            raise click.ClickException(str(exc)) from exc
        base_ref = binding["base_ref"]
    path = _create_change_manifest(
        root,
        change_id,
        title,
        intent,
        source_items,
        base_ref,
        scope_items,
    )
    print(f"Initialized graph-ba change: {path}")


@change_group.command("compile")
@click.argument("change_id")
@click.option(
    "--mode",
    default=None,
    type=click.Choice(["explore", "dev", "review", "release"]),
    help="Gate strictness for generated findings; defaults to change.yaml mode or dev",
)
@click.option(
    "--snapshot",
    "snapshot_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Accepted fingerprint snapshot for stale checks",
)
@click.pass_context
def change_compile(ctx, change_id, mode, snapshot_path):
    """Write generated pack/gaps/state/projection files for a change."""
    root = Path(ctx.obj.get("root", ".")).resolve()
    db = _conn(ctx)
    _require_graph(ctx, db)
    manifest_path = _change_manifest_path(root, change_id)
    if not manifest_path:
        raise click.ClickException(f"Change not found: {change_id}")
    compiled_dir = _change_output_path(root, change_id, manifest_path)
    compiled_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_change_manifest(manifest_path)
    service = ChangeWorkflowService(root, db)
    compiled_change = None
    try:
        compiled_change = service.compile(change_id)
    except ChangeWorkflowError as exc:
        if manifest_path.parent == root / ".graphba" / "changes":
            db.close()
            raise click.ClickException(str(exc)) from exc
    semantic_payload = compiled_change["semantic"] if compiled_change else None
    context_payload = compiled_change["impact"] if compiled_change else None
    proposal_payload = (
        proposal_check(semantic_payload, manifest) if semantic_payload else None
    )
    approval_payload = (
        approval_status(root, change_id, semantic_payload) if semantic_payload else None
    )
    delivery_targets = (
        _delivery_target_ids(db, semantic_payload, manifest) if semantic_payload else []
    )
    delivery_payload = (
        delivery_gate_payload(
            db,
            root,
            delivery_targets,
            proposal_fingerprint=semantic_payload["proposal_fingerprint"],
            mode=mode,
            snapshot_path=snapshot_path,
            approval=approval_payload,
            require_approval=True,
        )
        if semantic_payload
        else None
    )
    graph_native = manifest_path.parent == root / ".graphba" / "changes"
    if graph_native and delivery_targets:
        primary_target = delivery_targets[0]
        primary_gate = next(
            check for check in delivery_payload["checks"]
            if check["target"] == primary_target
        )
        graph_payload = _graph_slice_payload(
            db,
            root,
            primary_target,
            mode,
            snapshot_path,
            "excerpt",
            1200,
            False,
            gate_data=primary_gate,
        )
        gate_payload = primary_gate
        state_payload = _state_payload_from_graph(change_id, manifest, graph_payload)
        pack_payload = _pack_payload_from_graph(graph_payload)
    else:
        graph_payload = _graph_slice_payload(
            db, root, change_id, mode, snapshot_path, "excerpt", 1200, False
        )
        gate_payload = _gate_payload(db, root, change_id, mode, snapshot_path)
        state_payload = _change_payload(db, root, change_id)
        pack_payload = _pack_payload(db, root, change_id)
    db.close()
    if semantic_payload:
        (compiled_dir / "semantic-diff.json").write_text(
            json.dumps(
                semantic_payload, ensure_ascii=False, indent=2, default=str
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        (compiled_dir / "context.json").write_text(
            json.dumps(
                context_payload, ensure_ascii=False, indent=2, default=str
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        (compiled_dir / "graph-delta.json").write_text(
            json.dumps(
                compiled_change["graph_delta"], ensure_ascii=False, indent=2, default=str
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        (compiled_dir / "impact.json").write_text(
            json.dumps(
                compiled_change["impact"], ensure_ascii=False, indent=2, default=str
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        (compiled_dir / "proposal-check.json").write_text(
            json.dumps(
                proposal_payload, ensure_ascii=False, indent=2, default=str
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
        (compiled_dir / "delivery-check.json").write_text(
            json.dumps(
                delivery_payload, ensure_ascii=False, indent=2, default=str
            ).rstrip()
            + "\n",
            encoding="utf-8",
        )
    (compiled_dir / "graph.json").write_text(
        json.dumps(graph_payload, ensure_ascii=False, indent=2, default=str).rstrip()
        + "\n",
        encoding="utf-8",
    )
    (compiled_dir / "worklist.json").write_text(
        json.dumps(
            graph_payload["agent_worklist"], ensure_ascii=False, indent=2, default=str
        ).rstrip()
        + "\n",
        encoding="utf-8",
    )
    (compiled_dir / "evidence-plan.json").write_text(
        json.dumps(
            graph_payload["evidence_plan"], ensure_ascii=False, indent=2, default=str
        ).rstrip()
        + "\n",
        encoding="utf-8",
    )
    (compiled_dir / "state.yaml").write_text(
        _render_change_state_yaml(state_payload), encoding="utf-8"
    )
    (compiled_dir / "gaps.md").write_text(
        _render_gaps_markdown(gate_payload), encoding="utf-8"
    )
    (compiled_dir / "evidence-plan.md").write_text(
        _render_evidence_plan_markdown(graph_payload["evidence_plan"]),
        encoding="utf-8",
    )
    (compiled_dir / "worklist.md").write_text(
        _render_worklist_markdown(graph_payload), encoding="utf-8"
    )
    (compiled_dir / "projection.md").write_text(
        _render_projection_markdown(graph_payload), encoding="utf-8"
    )
    (compiled_dir / "pack.md").write_text(
        _render_pack_markdown(pack_payload).rstrip() + "\n", encoding="utf-8"
    )
    print(f"Compiled graph-ba change: {compiled_dir}")


def _change_path(root: Path, change_id: str) -> Path:
    return root / ".graphba" / "changes" / change_id


def _change_manifest_path(root: Path, change_id: str) -> Path | None:
    single_file = root / ".graphba" / "changes" / f"{change_id}.yaml"
    if single_file.is_file():
        return single_file
    legacy = _change_path(root, change_id) / "change.yaml"
    return legacy if legacy.is_file() else None


def _change_output_path(root: Path, change_id: str, manifest_path: Path) -> Path:
    legacy_dir = _change_path(root, change_id)
    if manifest_path.parent == legacy_dir:
        return legacy_dir / "compiled"
    return root / "reports" / "graphba" / "changes" / change_id


def _read_change_manifest(path: Path) -> dict[str, Any]:
    from graph_ba.traceability import _read_graph_native_change

    return _read_graph_native_change(path)


def _delivery_target_ids(
    db: sqlite3.Connection,
    semantic_payload: dict[str, Any],
    manifest: dict[str, Any],
) -> list[str]:
    changed = {
        item["id"]
        for item in semantic_payload.get("contract", [])
        if item.get("operation") != "remove"
    }
    candidates = changed | set(manifest.get("scope", []))
    if not candidates:
        return []
    placeholders = ",".join("?" for _ in candidates)
    contained = {
        row["target_id"]
        for row in db.execute(
            "SELECT target_id FROM edges WHERE relation_type = 'CONTAINS' "
            f"AND source_id IN ({placeholders}) AND target_id IN ({placeholders})",
            (*sorted(candidates), *sorted(candidates)),
        ).fetchall()
    }
    return sorted(candidates - contained)


def _state_payload_from_graph(
    change_id: str,
    manifest: dict[str, Any],
    graph_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "change": {
            "id": change_id,
            "title": manifest.get("title") or change_id,
            "state": manifest.get("state") or "draft",
            "mode": manifest.get("mode") or "",
        },
        "scope": [
            {
                "id": node["id"],
                "type": node["type"],
                "lifecycle": node.get("lifecycle") or "draft",
                "computed": node.get("computed", {}),
            }
            for node in graph_payload.get("nodes", [])
        ],
    }


def _pack_payload_from_graph(graph_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": graph_payload["target"],
        "artifacts": [
            {
                "id": node["id"],
                "type": node["type"],
                "title": node.get("title", ""),
                "source_file": node.get("source", {}).get("file", ""),
                "line_number": node.get("source", {}).get("line", 0),
                "content": node.get("content", {}).get("text", ""),
            }
            for node in graph_payload.get("nodes", [])
        ],
        "edges": [
            {
                "source_id": edge["from"],
                "relation_type": edge["relation"],
                "target_id": edge["to"],
            }
            for edge in graph_payload.get("edges", [])
        ],
    }


def _create_change_manifest(
    root: Path,
    change_id: str,
    title: str,
    intent: str,
    source_items: tuple[str, ...],
    base_ref: str | None,
    scope_items: tuple[str, ...],
) -> Path:
    try:
        return init_change(
            root,
            change_id,
            title=title,
            intent=intent,
            sources=source_items,
            scope=scope_items,
            base_ref=base_ref,
        )
    except ChangeWorkflowError as exc:
        raise click.ClickException(str(exc)) from exc


def _create_change_dir(
    root: Path,
    change_id: str,
    title: str,
    state: str,
    mode: str,
    scope_items: tuple[str, ...],
) -> Path:
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
        "# Change source\n\n<!-- Add graph-native :::artifact blocks here. -->\n",
        encoding="utf-8",
    )
    return path
